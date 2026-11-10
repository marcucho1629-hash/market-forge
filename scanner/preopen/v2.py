"""Pre-open V2: prior *regular-session* research context, never an entry signal.

Uses only the existing V1 prior-session 5m IEX seed. The premarket V1
technical gate remains unchanged. Never fills missing candles, projects
IEX market share into consolidated volume, or promotes RTH context into
a same-day premarket-confirmed candidate.
"""
from collections import Counter
from datetime import date, datetime, time
from math import isfinite
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
MIN_RTH_BARS = 60
MIN_IEX_RTH_NOTIONAL = 5_000_000
MIN_CONTEXT_SCORE = 65
MAX_PER_SIDE = 10


def _ema(closes, period):
    values = []
    alpha = 2 / (period + 1)
    for x in closes:
        values.append(x if not values else values[-1] + alpha * (x - values[-1]))
    return values


def prior_context(ticker, raw, day, benchmark_return=None):
    """Classify only complete, observed previous-session 5m IEX candles.

    The frozen as-of day is the upcoming session. Rows from as-of or
    later immediately invalidate the candidate; no future leakage.
    """
    out = dict(ticker=ticker, source="IEX", category="PRIOR_RTH_ONLY",
               premarket_verified=False, entry_signal=False,
               usable=False, selected=False, score=None, reason=None)
    if not isinstance(raw, list) or not raw:
        return out | dict(reason="no_seed")
    asof = date.fromisoformat(day)
    valid = {}
    try:
        for row in raw:
            t = datetime.fromisoformat(row["t"].replace("Z", "+00:00"))
            if t.tzinfo is None or t.second or t.microsecond or t.minute % 5:
                raise ValueError("timestamp")
            local = t.astimezone(ET)
            if local.date() >= asof:
                raise ValueError("future_seed")
            if not time(9, 30) <= local.time() < time(16, 0):
                continue
            vals = [row[k] for k in ("o", "h", "l", "c", "v", "n", "vw")]
            if not all(type(x) in (int, float) and isfinite(x) for x in vals):
                raise ValueError("numeric")
            if not (0 < row["l"] <= min(row["o"], row["c"])
                    <= max(row["o"], row["c"]) <= row["h"]
                    and row["v"] > 0 and row["n"] > 0 and row["vw"] > 0):
                raise ValueError("ohlcv")
            if t in valid and valid[t] != row:
                raise ValueError("conflict")
            valid[t] = row
    except (ValueError, TypeError, KeyError, OverflowError):
        return out | dict(reason="invalid_seed")
    if not valid:
        return out | dict(reason="no_rth_bars")
    bars = [valid[t] for t in sorted(valid)]
    times = sorted(valid)
    last_date = times[-1].astimezone(ET).date()
    if any(t.astimezone(ET).date() != last_date for t in times):
        return out | dict(reason="mixed_sessions")
    if (asof - last_date).days > 7:
        return out | dict(reason="old_session")
    out.update(prior_date=last_date.isoformat(), rth_bars=len(bars))
    if len(bars) < MIN_RTH_BARS:
        return out | dict(reason="rth_bars_insufficient")
    if times[-1].astimezone(ET).time() < time(15, 40):
        return out | dict(reason="rth_last_bar_stale")
    if any((b - a).total_seconds() != 300 for a, b in zip(times[-12:-1], times[-11:])):
        return out | dict(reason="rth_recent_gaps")
    notional = sum(r["vw"] * r["v"] for r in bars)
    out["iex_rth_notional"] = round(notional)
    if notional < MIN_IEX_RTH_NOTIONAL:
        return out | dict(reason="rth_iex_liquidity_low")

    closes = [r["c"] for r in bars]
    e9, e21 = _ema(closes, 9), _ema(closes, 21)
    close = closes[-1]
    prior_vwap = sum(r["vw"] * r["v"] for r in bars) / sum(r["v"] for r in bars)
    rising = e9[-1] > e21[-1] and e21[-1] > e21[-6] and close > closes[-13]
    falling = e9[-1] < e21[-1] and e21[-1] < e21[-6] and close < closes[-13]
    side = 1 if rising else -1 if falling else 0
    intraday_return = 100 * (close / bars[0]["o"] - 1)
    out.update(prior_close=round(close, 4), prior_rth_return_pct=round(intraday_return, 3),
               prior_iex_vwap=round(prior_vwap, 4), usable=True,
               direction="BULLISH" if side > 0 else "BEARISH" if side < 0 else "NEUTRAL")
    if side == 0:
        return out | dict(reason="no_confirmed_prior_trend")
    last_hour_volume = sum(r["v"] for r in bars[-12:])
    earlier_volume = sum(r["v"] for r in bars[-36:-12]) / 2
    high = max(r["h"] for r in bars)
    low = min(r["l"] for r in bars)
    range_position = (close - low) / (high - low) if high > low else .5
    close_near_edge = range_position >= .75 if side > 0 else range_position <= .25
    relative = None if benchmark_return is None else intraday_return - benchmark_return
    points = dict(
        trend=25,
        vwap=20 if side * (close - prior_vwap) > 0 else 0,
        recent_momentum=15 if side * (close - closes[-13]) > 0 else 0,
        relative=15 if relative is not None and side * relative > 0 else 0,
        volume=10 if last_hour_volume >= 1.2 * earlier_volume else 0,
        range_position=10 if close_near_edge else 0,
        liquidity=5,
    )
    score = sum(points.values())
    out.update(score=score, points=points,
               relative_to_spy_pct=round(relative, 3) if relative is not None else None,
               selected_eligible=score >= MIN_CONTEXT_SCORE,
               reason=None if score >= MIN_CONTEXT_SCORE else "context_score_below_minimum")
    return out


def research_report(seeds, day, universe):
    """Independent context shortlist; does not alter V1 technical_pass."""
    seeds = seeds if isinstance(seeds, dict) else {}
    raw_by_symbol = seeds.get("bars", {}) if isinstance(seeds.get("bars", {}), dict) else {}
    errors = seeds.get("errors", {}) if isinstance(seeds.get("errors", {}), dict) else {}
    # The market baseline can be used only when it came from a valid same-day
    # prior IEX session, not from a fabricated or missing SPY close.
    benchmark = prior_context("SPY", raw_by_symbol.get("SPY", []), day)
    benchmark_return = benchmark.get("prior_rth_return_pct") if benchmark.get("usable") else None
    benchmark_date = benchmark.get("prior_date")
    rows = []
    for ticker in universe:
        if ticker in errors:
            result = dict(ticker=ticker, category="PRIOR_RTH_ONLY",
                          premarket_verified=False, entry_signal=False,
                          usable=False, selected=False, reason="seed_fetch_failed")
        else:
            result = prior_context(ticker, raw_by_symbol.get(ticker, []), day, benchmark_return)
            if (benchmark_date is None or result.get("prior_date") != benchmark_date) and result.get("usable"):
                result["selected_eligible"] = False
                result["reason"] = "benchmark_session_unavailable"
        rows.append(result)
    picks = []
    for direction in ("BULLISH", "BEARISH"):
        choices = [r for r in rows if r.get("selected_eligible") and r.get("direction") == direction]
        chosen = sorted(choices, key=lambda r: (-r["score"], -abs(r["prior_rth_return_pct"]),
                                                  r["ticker"]))[:MAX_PER_SIDE]
        picks.extend(chosen)
    selected = {r["ticker"] for r in picks}
    for row in rows:
        row["selected"] = row["ticker"] in selected
    return dict(mode="PRIOR_RTH_CONTEXT_ONLY", asof_day=day, feed="iex",
                benchmark="SPY" if benchmark_return is not None else "UNAVAILABLE",
                quality_counts=dict(Counter(r.get("reason") or "context_eligible"
                                            for r in rows)),
                rows=rows, selected=picks,
                not_live=True, premarket_confirmed_count=0, trade_signals=0)
