# Intraday V3: CHOPPY and LATE_CHASE

Research-only change. Applies to native-IEX 5-minute Intraday V3 candidate selection *before* the two directional Top 10 lists are saved. Pine / TradingView signal generation and X/SX/HX protection are unchanged.

## Quality gates
- **CHOPPY:** Require a continuous observed 5-minute window of 6 to 14 bars (14 after warmup). Calculate rolling Choppiness Index = 100 * log10(sum(True Range) / (highest high - lowest low)) / log10(N). Exclude when CHOP >= 61.8, or close-to-close directional efficiency < 0.30 with >=3 reversals. Missing bars inside the full lookback return structure_gap. No interpolation, fabricated bars or current-day hindsight.
- **LATE_CHASE:** Exclude bullish and bearish names that moved too far from EMA9 and VWAP, an extended 15-minute impulse, an excessive breakout beyond the previous 20-minute range, or late-session >=8 ATR excursion without a sufficient reset. Symmetric for bullish and bearish. The reason is late_chase.
- **No mandatory top 10:** Maximum 10 each, can be 0. The Telegram report includes counts for CHOPPY and LATE_CHASE. Past candidates are marked CHOPPY or CHASE when removed.
- **Downstream fail-safe:** MC/SCMP outbound gate and 13:40 Overnight WATCH both reject explicit choppy/late_chase flags, even if eligible=true was stored incorrectly.

## Limits
- Thresholds are conservative, **uncalibrated** research heuristics. CHOP from 6-13 early bars is not equivalent to full 14-bar CHOP. Compare multi-day opportunity recall and false exclusions; no improved hit-rate claim.
- IEX is not SIP/BATS, and Bigdata catalyst/news and options liquidity remain unverified. X/SX/HX alert routes are unaffected.
- **07:20 MT Pre-Open V2 prior-RTH research watch is unchanged.** Its stored names are rechecked using the new filters by Intraday V3 after 08:00 MT. 13:40 Overnight WATCH inherits the filtered intraday selected list.

## Validation
CI: preopen, intraday, Top20 entry Telegram and overnight tests. Inspect /intraday-v3 authenticated admin panel latest rows and quality_counts, ensure live selected entries do not have choppy_chart or late_chase, and check next hourly Telegram send statuses. Production rollout must follow passing PR CI and Vercel READY.
