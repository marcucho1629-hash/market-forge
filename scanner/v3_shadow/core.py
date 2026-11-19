"""Isolated, deterministic RTH opening-shock research engine.

No IO, network calls, order routing, database, or Telegram side effects.
Only finalized one-minute bars may be submitted. Results are hypotheses, not trades.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class Bar:
    ticker: str
    end: datetime  # final 1m close, timezone-aware
    open: float
    high: float
    low: float
    close: float
    volume: float

    def check(self):
        if not self.ticker or self.end.tzinfo is None or self.end.second or self.end.microsecond:
            raise ValueError("invalid bar identity or close timestamp")
        if not (0 < self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high and self.volume > 0):
            raise ValueError("invalid finalized OHLCV")


@dataclass(frozen=True)
class Reference:
    # Frozen from prior completed bars; never fitted to future RTH data.
    prior_close: float
    atr: float
    avg_minute_volume: float
    complete: bool = True

    def check(self):
        if min(self.prior_close, self.atr, self.avg_minute_volume) <= 0:
            raise ValueError("invalid reference")


@dataclass(frozen=True)
class Rules:
    shock_body_atr: float = 0.9
    volume_multiple: float = 1.2
    close_location: float = 0.25
    chase_max_atr: float = 0.6
    confirm_volume_multiple: float = 0.5
    candidate_ttl_min: int = 2


@dataclass
class Candidate:
    origin: datetime
    side: int
    anchor: float
    high: float
    low: float
    atr0: float
    prior_gap_atr: float
    episode: str


class Engine:
    def __init__(self, rules=None):
        self.rules = rules or Rules()
        self.current = {}

    def on_bar(self, bar: Bar, reference: Reference):
        """Returns WATCH / PROVISIONAL / MISSED / INVALIDATED / DATA_HALT.

        A PROVISIONAL event is research only; it MUST NOT be sent as MC/SCMP.
        """
        bar.check()
        reference.check()
        t = bar.end.astimezone(ET)
        day = t.date().isoformat()
        s = self.current.get(bar.ticker)
        events = []
        if not s or s['day'] != day:
            s = {'day': day, 'last': None, 'candidate': None, 'done': False}
            self.current[bar.ticker] = s
        if not (time(9, 31) <= t.time() <= time(16, 0)):
            return events
        if s['last'] and bar.end <= s['last']:
            raise ValueError("duplicate or out-of-order bar")
        if s['last'] and bar.end - s['last'] != timedelta(minutes=1):
            s['candidate'] = None
            s['last'] = bar.end
            events.append({'ticker': bar.ticker, 'time': bar.end.isoformat(), 'event': 'DATA_HALT', 'reason': 'missing_final_minute'})
            return events
        if not s['last'] and t.time() != time(9, 31):
            s['last'] = bar.end
            return [{'ticker': bar.ticker, 'time': bar.end.isoformat(), 'event': 'DATA_HALT', 'reason': 'missing_opening_minute'}]
        s['last'] = bar.end
        if not reference.complete:
            s['candidate'] = None
            return [{'ticker': bar.ticker, 'time': bar.end.isoformat(), 'event': 'DATA_HALT', 'reason': 'warmup_incomplete'}]

        cand = s['candidate']
        if cand:
            dist = cand.side * (bar.close - cand.anchor) / cand.atr0
            age = (bar.end - cand.origin).total_seconds() / 60
            # Excess extension is a missed opportunity, never a new entry.
            if dist > self.rules.chase_max_atr:
                result = ('MISSED', 'exceeded_frozen_chase_limit')
            elif age > self.rules.candidate_ttl_min:
                result = ('INVALIDATED', 'candidate_expired')
            elif cand.side * (bar.close - bar.open) >= 0 and dist >= 0 and bar.volume >= self.rules.confirm_volume_multiple * reference.avg_minute_volume and (bar.high <= cand.high + 0.25*cand.atr0 if cand.side < 0 else bar.low >= cand.low - 0.25*cand.atr0):
                result = ('PROVISIONAL', 'direction_and_opening_level_held')
            elif (bar.high > cand.high + 0.25*cand.atr0 if cand.side < 0 else bar.low < cand.low - 0.25*cand.atr0):
                result = ('INVALIDATED', 'opening_structure_failed')
            else:
                return events
            evt, why = result
            e = {'ticker': bar.ticker, 'time': bar.end.isoformat(), 'event': evt, 'reason': why,
                 'episode': cand.episode, 'side': 'BEAR' if cand.side < 0 else 'BULL',
                 'candidate_at': cand.origin.isoformat(), 'chase_atr': round(max(dist, 0), 4),
                 'gap_atr_context': round(cand.prior_gap_atr, 4), 'research_only': True}
            if evt == 'PROVISIONAL':
                e.update(entry_proxy=bar.close, stop_reference=cand.high + 0.15*cand.atr0 if cand.side < 0 else cand.low - 0.15*cand.atr0,
                         risk_per_share=abs(bar.close - (cand.high + 0.15*cand.atr0 if cand.side < 0 else cand.low - 0.15*cand.atr0)))
            s['candidate'] = None
            s['done'] = True
            return [e]

        # Research only one opening setup per ticker/day; avoid repeated alert churn.
        if s['done'] or t.time() > time(9, 35):
            return events
        body = bar.close - bar.open
        direction = 1 if body > 0 else -1
        location = (bar.close - bar.low)/(bar.high - bar.low) if bar.high > bar.low else 0.5
        if abs(body) < self.rules.shock_body_atr*reference.atr:
            return events
        if bar.volume < self.rules.volume_multiple*reference.avg_minute_volume:
            return events
        if (direction < 0 and location > self.rules.close_location) or (direction > 0 and location < 1-self.rules.close_location):
            return events
        gap = (bar.open-reference.prior_close)/reference.atr
        episode = f'{day}:{bar.ticker}:{int(bar.end.timestamp())}:{direction}'
        s['candidate'] = Candidate(bar.end,direction,bar.close,bar.high,bar.low,reference.atr,gap,episode)
        return [{'ticker': bar.ticker, 'time': bar.end.isoformat(), 'event': 'WATCH',
                 'reason': 'first_completed_opening_impulse', 'side': 'BEAR' if direction < 0 else 'BULL',
                 'episode': episode, 'gap_atr_context': round(gap,4), 'research_only': True}]
