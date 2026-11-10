# Market Forge | 13:40 MT Power Hour Overnight Swing WATCH

**Purpose:** One separate Telegram research bulletin per **US trading day**, 13:40 MT (15:40 ET), approximately 20 minutes before NYSE regular close. Does not replace existing 13:00 MT Power Hour briefing, 13:00 Intraday V3 Top20 or Swing 2–5/5–10 day engine. No orders, position change or DAY/MC/SCMP rules.

## Schedule

- `vercel.json` adds an authenticated Vercel Cron route `GET /scanner/overnight-1340/worker` at **19:40 UTC and 20:40 UTC**, Monday–Friday. Application checks `America/Denver` is **13:40–13:49** and verifies Alpaca's exchange calendar and current regular-session bounds. The wrong UTC slot is harmlessly ignored across DST.
- Provider cron delivery is **best effort**, not a guarantee of an exact second. An idempotent record and outbox key `overnight-watch-v1:YYYY-MM-DD:1340` prevent duplicate notification if the request retries.
- The existing 5-minute V3 scanner still collects data independently; this bulletin takes the latest already-persisted scanner snapshot, making **no extra Alpaca price queries or Bigdata billable calls**. As of 13:40 MT, the normal 13:37 scan ordinarily represents completed candles through 13:35 MT.

## Screening and output

- Requires the latest V3 full 150-symbol native **IEX** 5m WATCH snapshot within 10 minutes, completed-bar cutoff no older than 15 minutes, and **each candidate's last completed bar within 10 minutes**. Wrong session / malformed clock / no ready data => explicit `DATA_UNAVAILABLE` warning and **no stale candidates**.
- From currently selected Bullish and Bearish Top 10, keep only `eligible=true`, `score>=65`, `chase in (LOW,MED)`, positive recorded price and recent symbol-level completed candle. Max **10 bullish and 10 bearish**, not artificially filled. These numerical thresholds are **unvalidated research heuristics**, not predictive win probabilities.
- Labels `NEW`, `CONTINUE`, `FLIP` compare against prior 13:00 MT hourly WATCH shortlist. The direction itself is the V3 scanner direction, not the separate Swing engine's 1h/4h/daily eligibility.
- No verified earnings calendar, overnight news catalyst, macro release, options open interest/volume, liquidity or real consolidated market-wide price. These checks are clearly marked **unverified** and overnight gap risk is explicit. **Do not treat this as an instruction to carry positions overnight.**

## Troubleshooting

- Sanitized Vercel log line `OVERNIGHT_1340_CRON` contains status, day, quality, counts and Telegram outbox send status. `sent` indicates Telegram API acceptance only.
- An authenticated operator can inspect outbox id and `overnight-watch-v1:<date>:1340` persisted report in the existing store. Do not post Telegram/CRON/Webhook keys to chat.
- Existing Top20 MC/SCMP entry gate and all X/SX/HX exits are unchanged; this WATCH message cannot itself trigger chart signals.
- Unit verification: `python3 -m unittest discover -s tests -p test_overnight_watch.py -v` plus all established Preopen/Intraday/Top20 entry regressions.

Deployment is separate from test; verify GitHub CI success, Vercel production READY on the merged SHA, then inspect first 13:40 cron invocation and Telegram `sent` state.