# Market Forge V3 Shadow: opening shock research engine

Status: research-only code, not deployed or connected to alerts.

This PR leaves main, DAY V13.39, IEX5, Swing, TradingView, cron, database and Telegram untouched.

## Why
Recent November 5–6 diagnostics show that current DAY entries may be suppressed by the original 1m/2m/3m full entry gates, gaps in indicator warmup, and 3-minute cycle alignment. This small experiment separates early candidate discovery from entry confirmation rather than changing the old gate thresholds.

## Input and events
Accepts finalized 1m OHLCV and an explicit prior-completed-bar reference: previous close, ATR, average minute volume, and complete warmup flag. It is intentionally independent of HTTP, credentials, database, orders, and Telegram.

Emits WATCH, PROVISIONAL, MISSED, INVALIDATED, or DATA_HALT. PROVISIONAL is research only; it must not be forwarded as MC or SCMP. Opening gap is tracked separately from extension after candidate detection. Research parameters are initial hypotheses, not proven trading thresholds.

Only one opening setup per ticker/day is evaluated, in the 9:31–9:35 ET window. This is NOT a 150/200 ticker all-day scanner, not a profit-protection engine and not a proven strategy.

## Tests
Run: python -m unittest discover -s tests -p test_v3_shadow.py -v

Seven synthetic tests passed in isolation on the local draft. Repository-wide tests and realistic replay remain pending.

## Requirements before any integration
1. Replay against same-source completed minute bars and complete previous session seed; distinguish historical backfill from actual first-received time.
2. Include strong up/down openings, gap reversals, and quiet sessions over multiple dates. Compare original entry, new WATCH/provisional timing, MFE/MAE, false breakouts, chase misses, and exits.
3. Validate persistence of candidate state across serverless requests, source freshness, duplicate IDs, bar revisions, incomplete candles and market calendars.
4. Build incremental storage; do not re-export all 20 tickers and full session on every webhook. Budget Neon egress and measure runtime and costs.
5. Keep data-adapter/Shadow/Telegram integration as separate PRs; do not enable production signals without fresh forward evidence and the user's release approval.
6. Before touching cron or Telegram, verify Vercel deployment scope, worker URL/method/auth, delivery idempotency, retries, suppress-old-entry policy, monitoring and rollback.

This PR is not an authorization to place any order or change live risk filters.
