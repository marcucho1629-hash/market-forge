# Market Forge DAY V3.3 Early + FLIP + Profit Protection | 2026-11-13

**Intent:** user values fewer signals in CHOPPY conditions, but wants early momentum awareness and faster warnings for reversals/surrendered favorable price movement. **Keep all existing production thresholds, CHOPPY suppression and MC/SCMP/X/SX/HX signals unchanged.**

## Safe design
- Separate one-day research ledger, `day-v33-early-protection-shadow:`, active on **Friday Nov 13, 2026 only**. Existing five-minute Scanner cron observes 150-ticker IEX data and can write independent, as-of compact state for each observed completed-bar scan (not every attempted/requested scan).
- Store early actual 7:35/7:40 `OPEN_RADAR` *sent* candidates, 7:45–7:55 `OPEN_EARLY` *sent* candidates, and 8:00+ **all 150-symbol 5m per-ticker evaluation states**; distinguish eligible `70+/momentum 8+/CHASE LOW` from CHOPPY/neutral/score below limits. Qualified early RADAR is still `CHASE UNKNOWN`, not LOW. Received timestamps are authoritative, not candle endpoints or fabricated arrival times. A source fetch failure is recorded but must never interfere with live watch / exit paths.
- From the first as-of qualifying strong LOW per ticker, research-only first `WEAKNESS` on later completed 5m states (direction loss, VWAP crossing, low score/momentum or CHOPPY), and first `OPPOSITE_STRONG_5M` when the opposite direction becomes another strong 70+/8+/LOW candidate. Deduplicate. They are **not** intrabar price-flip detection, broker exits or production Telegram alerts, and do not imply that warnings will lead to improved fillable P&L.
- At **14:52 MT** (DST-safe two cron slots), after close, fetch native Alpaca IEX 1-minute bars for only the symbols actually identified as early/strong candidates. Build completed 3m candle proxies and test confirmation AFTER 5m observation (code reused from Nov12, but still **not** the 20-symbol V13.36 MC/SCMP engine).
- Compare 7:35 early RADAR / EARLY sent-watch timestamps vs first same-direction 5m strong discovery within 45 minutes. Keep potential opposite-direction flips separately instead of calling them early profitable predictions.
- For 30-minute hypothetical price paths, use the **first full 1m candle opening AFTER actual scan observation** as a conservative next-minute illustrative reference, not the already-past 5m closing price. Report max favorable excursion (MFE), adverse excursion (MAE), and potential giveback only when all 30 subsequent native 1m candles exist. Missing bars and stale coverage cannot be made up; no actual executions, spreads or commissions are modeled.
- Send one **after-close-only** informational research Telegram, using the existing at-most-once durable outbox. Private keyed report at `/day-v33-shadow`. No new intraday messages and no Bigdata credit costs.
- Vercel cron `/scanner/day-v33-shadow/worker` schedule `52 20,21 * * 1-5` with strict local Friday 14:52–14:59 and completed NYSE close checks; all other dates no-op.
- **Nov12 V3.2 snapshot is frozen/untouched.** No change to existing TradingView, Top10 selection, 5m alerts, swing, Telegram routes or Pine protective orders.

## Evaluation
To consider a later version, compare **actual 5m as-of visibility**, early detection lead and false flip warnings, number of CHOPPY suppressed entries, actual provider timestamps, completed-3m proxy confirmation, missing-source rates, and hypothetical 30m giveback separately. A single-day result cannot establish success; multi-day forward observation of live latency, actual signals and exits is necessary before any live cutover.

No active trade should be entered or exited solely on this research report. 30m giveback is not a realized return or a profit protection guarantee.
