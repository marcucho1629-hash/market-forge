"""Market Forge V13.36 RESEARCH ONLY.

Standalone, close-only 1-minute OHLCV simulator. No network, order execution,
TradingView alert calls, Bigdata queries, or Telegram delivery.
Not a replacement for the existing Pine/server production engine.
"""
from __future__ import annotations
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from statistics import mean
from zoneinfo import ZoneInfo
from typing import Optional
import math

VERSION = "MF_V13_38_SIGNAL_SEQUENCE"
ET = ZoneInfo("America/New_York")
SYMBOLS = tuple("SPY QQQ NVDA TSLA AVGO AMD META AAPL MSFT AMZN PLTR PANW SNDK GOOG RDDT HOOD COIN JPM AXP V".split())


@dataclass(frozen=True)
class Bar:
    ticker: str
    end: datetime  # Time of close; all prices and volume are final for the bar
    open: float
    high: float
    low: float
    close: float
    volume: float
    closed: bool = True

    def validate(self) -> None:
        if self.closed is not True:
            raise ValueError("unconfirmed bar rejected")
        if self.ticker not in SYMBOLS:
            raise ValueError("ticker outside 20-symbol research universe")
        if self.end.tzinfo is None or self.end.second or self.end.microsecond:
            raise ValueError("closed-bar end must have timezone and minute alignment")
        if any(not math.isfinite(v) for v in (self.open, self.high, self.low, self.close, self.volume)):
            raise ValueError("non-finite OHLCV")
        if not (0 < self.low <= self.open <= self.high and self.low <= self.close <= self.high and self.volume > 0):
            raise ValueError("invalid OHLCV")
        local = self.end.astimezone(ET)
        if not (time(9, 30) < local.time() <= time(16, 0)):
            raise ValueError("outside regular cash-market session")


@dataclass(frozen=True)
class Config:
    # Research-only initial hypotheses: tune using 1m historical replay, not anecdote.
    entry_body_atr: dict[int, float] = field(default_factory=lambda: {1: 1.25, 2: 1.05, 3: 0.90})
    entry_rel_volume: dict[int, float] = field(default_factory=lambda: {1: 1.50, 2: 1.35, 3: 1.25})
    breakout_lookback: int = 4
    min_prior_bars: int = 4
    chase_ema_atr: float = 2.25
    emergency_flip_body_atr: float = 1.55
    two_bar_flip_body_atr: float = 0.65
    profit_activate_atr: float = 1.75
    trim_retrace_atr: float = 0.85
    trim_mfe_fraction: float = 0.35
    protect_retrace_atr: float = 1.45
    protect_mfe_fraction: float = 0.60
    exit_cooldown_minutes: int = 3
    entry_cutoff_et: time = time(15, 30)
    flat_alert_et: time = time(15, 45)


@dataclass(frozen=True)
class Event:
    ticker: str
    time: datetime
    action: str
    price: float
    side: int
    timeframe: int
    reason: str
    episode: int
    catalyst: str = "UNKNOWN"


@dataclass
class Window:
    tf: int
    history: deque[Bar] = field(default_factory=lambda: deque(maxlen=100))
    tr_history: deque[float] = field(default_factory=lambda: deque(maxlen=14))
    ema9: Optional[float] = None
    ema21: Optional[float] = None
    previous_ema9: Optional[float] = None
    previous_ema21: Optional[float] = None
    previous_atr: Optional[float] = None
    previous_volume_avg: Optional[float] = None
    previous_high: Optional[float] = None
    previous_low: Optional[float] = None
    latest: Optional[Bar] = None
    pending: list[Bar] = field(default_factory=list)

    def push(self, bar: Bar, minute_in_session: int) -> bool:
        self.pending.append(bar)
        if minute_in_session % self.tf:
            return False
        bars = self.pending
        self.pending = []
        b = Bar(bar.ticker, bar.end, bars[0].open, max(v.high for v in bars),
                min(v.low for v in bars), bars[-1].close, sum(v.volume for v in bars))
        old = self.latest
        self.previous_atr = mean(self.tr_history) if len(self.tr_history) >= 4 else None
        self.previous_volume_avg = mean(v.volume for v in list(self.history)[-8:]) if len(self.history) >= 4 else None
        recent = list(self.history)[-4:]
        self.previous_high = max(v.high for v in recent) if len(recent) >= 4 else None
        self.previous_low = min(v.low for v in recent) if len(recent) >= 4 else None
        self.previous_ema9, self.previous_ema21 = self.ema9, self.ema21
        self.ema9 = b.close if self.ema9 is None else self.ema9 + (2 / 10) * (b.close - self.ema9)
        self.ema21 = b.close if self.ema21 is None else self.ema21 + (2 / 22) * (b.close - self.ema21)
        tr = max(b.high - b.low, abs(b.high - old.close) if old else 0, abs(b.low - old.close) if old else 0)
        self.tr_history.append(tr)
        self.history.append(b)
        self.latest = b
        return True


@dataclass
class Position:
    side: int
    entry: float
    entered: datetime
    episode: int
    best: float
    trimmed: bool = False
    continued: bool = False


@dataclass
class TickerState:
    windows: dict[int, Window] = field(default_factory=lambda: {t: Window(t) for t in (1, 2, 3, 5, 10)})
    date: Optional[object] = None
    last_end: Optional[datetime] = None
    position: Optional[Position] = None
    episode: int = 0
    cooldown_end: Optional[datetime] = None
    total_vwap_notional: float = 0.0
    total_volume: float = 0.0
    opposites: deque[tuple[int, float]] = field(default_factory=lambda: deque(maxlen=2))
    flat_sent: bool = False
    signal_cycle: int = -1
    entry_signals: dict[int, tuple[int, datetime]] = field(default_factory=dict)


class Engine:
    def __init__(self, cfg: Optional[Config] = None):
        self.cfg = cfg or Config()
        self.states: dict[str, TickerState] = {}

    def seed(self, bars: list[Bar]) -> None:
        """Warm indicators only from one prior contiguous RTH session. No events/positions."""
        if not bars:
            return
        symbol = bars[0].ticker
        if symbol in self.states:
            raise ValueError("seed must precede session")
        state = TickerState()
        previous = None
        day = bars[0].end.astimezone(ET).date()
        for b in bars:
            b.validate()
            local = b.end.astimezone(ET)
            if b.ticker != symbol or local.date() != day:
                raise ValueError("mixed seed session")
            if previous is not None and b.end - previous != timedelta(minutes=1):
                raise ValueError("seed gap")
            minute = local.hour * 60 + local.minute - 570
            if previous is None and minute != 1:
                raise ValueError("seed must begin at session open")
            for w in state.windows.values():
                w.push(b, minute)
            previous = b.end
        for w in state.windows.values():
            w.pending.clear()
        state.date = day
        self.states[symbol] = state

    def on_bar(self, bar: Bar, catalyst: Optional[str] = None) -> list[Event]:
        """Accept ONLY final one-minute bars in ascending contiguous order.
        Catalyst is a precomputed optional label; NEVER blocks exits or entries.
        """
        bar.validate()
        state = self.states.setdefault(bar.ticker, TickerState())
        local = bar.end.astimezone(ET)
        if state.date != local.date():
            if state.date is not None and state.position is not None:
                raise ValueError("previous session position not closed: fail safely")
            windows = state.windows
            for w in windows.values():
                w.pending.clear()
            self.states[bar.ticker] = state = TickerState(date=local.date(), windows=windows)
        minute_in_session = (local.hour * 60 + local.minute) - (9 * 60 + 30)
        if state.last_end is None and minute_in_session != 1:
            raise ValueError("session must start at 09:31 ET bar close for correct aggregation")
        if state.last_end is not None and bar.end.astimezone(ET) - state.last_end.astimezone(ET) != timedelta(minutes=1):
            raise ValueError("missing or duplicated minute: replay required to preserve state")
        state.last_end = bar.end
        state.total_vwap_notional += ((bar.high + bar.low + bar.close) / 3) * bar.volume
        state.total_volume += bar.volume
        vwap = state.total_vwap_notional / state.total_volume
        windows_closed = [tf for tf, w in state.windows.items() if w.push(bar, minute_in_session)]
        events: list[Event] = []
        if state.position:
            events.extend(self._risk(state, bar, vwap))
        if state.position and local.time() >= self.cfg.flat_alert_et:
            events.append(self._event(bar, state, "EOD_X", 1, "day-trade close reminder; no order sent", catalyst))
            state.position = None
            state.cooldown_end = bar.end + timedelta(minutes=self.cfg.exit_cooldown_minutes)
            state.flat_sent = True
        cycle = (minute_in_session - 1) // 3
        if state.signal_cycle != cycle:
            state.signal_cycle = cycle
            state.entry_signals.clear()
        if not state.position:
            for tf in (1, 2, 3):
                if tf not in windows_closed:
                    continue
                # Do not carry a 2m candle beginning in the previous 3m cycle.
                if minute_in_session - tf < cycle * 3:
                    continue
                direction = self._entry_side(state.windows[tf], vwap)
                if direction:
                    if any(side != direction for side, _ in state.entry_signals.values()):
                        state.entry_signals.clear()
                    state.entry_signals[tf] = (direction, bar.end)
            signals = state.entry_signals
            aligned = (all(tf in signals for tf in (1, 2, 3)) and
                       len({signals[tf][0] for tf in (1, 2, 3)}) == 1)
            if (3 in windows_closed and aligned and local.time() < self.cfg.entry_cutoff_et
                    and (state.cooldown_end is None or bar.end >= state.cooldown_end)):
                direction = signals[3][0]
                state.episode += 1
                state.position = Position(direction, bar.close, bar.end, state.episode, bar.close)
                state.opposites.clear()
                events.append(self._event(bar, state, "MC" if direction == 1 else "SCMP", 3,
                                          "1m+2m+3m entry signals matched within same 3m cycle", catalyst))
                state.entry_signals.clear()
        if state.position and 10 in windows_closed and not state.position.continued:
            if self._trend(state.windows[5], vwap) == state.position.side and self._trend(state.windows[10], vwap) == state.position.side:
                events.append(self._event(bar, state, "CTN", 10, "5m+10m trend aligned", catalyst))
                state.position.continued = True
        return events

    def _event(self, bar: Bar, state: TickerState, action: str, tf: int, reason: str, catalyst: Optional[str] = None) -> Event:
        p = state.position
        return Event(bar.ticker, bar.end, action, bar.close, p.side if p else 0, tf, reason,
                     p.episode if p else state.episode, catalyst or "UNKNOWN")

    def _entry_side(self, w: Window, vwap: float) -> int:
        b = w.latest
        if b is None or w.previous_atr is None or w.previous_volume_avg is None or w.previous_high is None or w.previous_low is None:
            return 0
        atr = w.previous_atr
        if atr <= 0:
            return 0
        body = b.close - b.open
        side = 1 if body > 0 else -1
        if abs(body) < self.cfg.entry_body_atr[w.tf] * atr:
            return 0
        if b.volume < self.cfg.entry_rel_volume[w.tf] * w.previous_volume_avg:
            return 0
        candle_location = (b.close - b.low) / (b.high - b.low) if b.high > b.low else 0.5
        if (side == 1 and candle_location < 0.72) or (side == -1 and candle_location > 0.28):
            return 0
        if side * (b.close - vwap) <= 0 or side * (b.close - w.ema9) < 0:
            return 0
        if side * (w.ema9 - w.previous_ema9) <= 0:
            return 0
        if abs(b.close - w.ema9) > self.cfg.chase_ema_atr * atr:
            return 0
        if side == 1 and b.close <= w.previous_high + 0.05 * atr:
            return 0
        if side == -1 and b.close >= w.previous_low - 0.05 * atr:
            return 0
        # Strong counter-trend reversal may override slow EMA21 alignment.
        ema_align = side * (w.ema9 - w.ema21) > 0
        very_strong = (abs(body) >= 1.60 * atr and b.volume >= 1.75 * w.previous_volume_avg)
        if not ema_align and not very_strong:
            return 0
        return side

    def _trend(self, w: Window, vwap: float) -> int:
        if len(w.history) < 3 or w.previous_ema9 is None or w.ema9 is None or w.ema21 is None:
            return 0
        b = w.latest
        recent = list(w.history)[-3:]
        gain = b.close - recent[0].close
        if gain > 0 and w.ema9 > w.ema21 and w.ema9 > w.previous_ema9 and b.close > vwap:
            return 1
        if gain < 0 and w.ema9 < w.ema21 and w.ema9 < w.previous_ema9 and b.close < vwap:
            return -1
        return 0

    def _risk(self, state: TickerState, bar: Bar, vwap: float) -> list[Event]:
        p = state.position
        w = state.windows[1]
        atr = w.previous_atr
        if atr is None or atr <= 0:
            p.best = max(p.best, bar.high) if p.side == 1 else min(p.best, bar.low)
            return []
        side = p.side
        body_opp = -side * (bar.close - bar.open)
        prev_bars = list(w.history)[-4:-1]  # Do not include current bar in structure reference.
        structure_break = bool(prev_bars) and (bar.close < min(v.low for v in prev_bars) if side == 1 else bar.close > max(v.high for v in prev_bars))
        weak_ema9 = side * (bar.close - w.ema9) < 0
        state.opposites.append((1 if body_opp > 0 else 0, max(0.0, body_opp / atr)))
        two_counter = (len(state.opposites) == 2 and all(x[0] for x in state.opposites)
                       and all(x[1] >= self.cfg.two_bar_flip_body_atr for x in state.opposites))
        strong_flip = body_opp >= self.cfg.emergency_flip_body_atr * atr and structure_break and weak_ema9
        # Prior completed bars' high-water mark only. No intrabar high/low order assumption.
        mfe = side * (p.best - p.entry)
        retrace = side * (p.best - bar.close)
        events = []
        confirm_weakness = (two_counter or (body_opp >= 0.8 * atr and weak_ema9))
        if mfe >= self.cfg.profit_activate_atr * atr and confirm_weakness and retrace >= max(self.cfg.protect_retrace_atr * atr, self.cfg.protect_mfe_fraction * mfe):
            events.append(self._event(bar, state, "PROTECT_X", 1, "profit trailing protection triggered"))
            state.position = None
            state.cooldown_end = bar.end + timedelta(minutes=self.cfg.exit_cooldown_minutes)
            state.opposites.clear()
            return events
        # Two ordinary opposite candles can signal TRIM during profitable trend.
        # For full FAST_X require either a shock candle OR damage to entry / EMA21.
        serious_two_bar_flip = (two_counter and structure_break and weak_ema9 and
                                (side * (bar.close - p.entry) <= -0.50 * atr or
                                 side * (bar.close - w.ema21) < 0))
        if strong_flip or serious_two_bar_flip:
            event = self._event(bar, state, "FAST_X", 1, "strong structural flip" if strong_flip else "2-bar confirmed structural flip")
            state.position = None
            state.cooldown_end = bar.end + timedelta(minutes=self.cfg.exit_cooldown_minutes)
            state.opposites.clear()
            return [event]
        if mfe >= self.cfg.profit_activate_atr * atr and not p.trimmed and confirm_weakness and retrace >= max(self.cfg.trim_retrace_atr * atr, self.cfg.trim_mfe_fraction * mfe):
            events.append(self._event(bar, state, "TRIM", 1, "partial-exit warning; trend not invalidated"))
            p.trimmed = True
        p.best = max(p.best, bar.high) if side == 1 else min(p.best, bar.low)
        return events
