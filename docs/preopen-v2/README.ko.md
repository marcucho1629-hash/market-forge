# Market Forge AI Pre-Open V2, 2026-11-09

**Draft PR, not deployed.** V1 07:20 MT premarket criteria, trading signals, DAY/Swing alerts, Bigdata and cron are unchanged.

## Why

Nov 9 V1 Telegram: 150 inspected, 149 INSUFFICIENT, 1 SPARSE_RECONCILED, 0 premarket-validated watch entries.
An independent **post-session** Alpaca IEX 5-minute-bar read for 08:00–09:15 ET found only 1 of the 150 tickers had >=12 native 5m bars through 09:15, so the V1 quality gate is structurally very difficult to satisfy on this single-exchange free feed. The retrospective API response is not a reconstruction of exactly which data the live app had at 09:20.

## What V2 changes

- New pure module `scanner/preopen/v2.py` reads *already-captured* previous completed regular-session 5-minute **IEX** bars from V1 seed storage. **Zero additional API calls, no fabricated candles**, no new provider entitlement, no client trading.
- Valid seed for research context requires >=60 true RTH 5-minute bars on one previous exchange day, a bar no older than 15:40 ET, contiguous final 12 observed bars, finite OHLCV and >$5M **IEX-only** RTH notional. No future-bar / as-of leak.
- Prior session context ranks short-term technical direction and prior-session strength. Score and thresholds are **unvalidated research hypotheses**. A valid SPY seed is required as a same-session reference. Nothing is called a complete, same-day premarket signal.
- An added field `prior_rth_context` in the V1 report holds its own rows, quality counters, and up to 5 bullish + 5 bearish research-only context items. It does not alter `rows[].technical_pass`, `quality_counts`, or `selected`.
- Existing 07:20 Telegram report retains its V1 verified candidate count, and also labels the prior-session context explicitly **전일 정규장 관찰 후보 · 장전 미검증**. Users can see both why V1 is empty and what may warrant **fresh** observation at the 09:30 ET open.

## Important limitations

- Prior RTH trend is NOT a premarket price signal, trade recommendation, option liquidity check, Bigdata catalyst, or forward-looking proof of a good setup.
- If prior-session IEX seed is sparse or SPY benchmark missing, the fallback appropriately remains empty.
- V1 itself still requires 12 native current-day premarket 5m bars, $1M IEX premarket notional, fresh data and 1m/5m reconciliation. Those protections are unchanged.
- New V2 context currently does **NOT** connect to the live 1m/2m/3m DAY engine. That requires a separate data-contract/forward-comparison project and explicit review.
- Since daily report and outbox IDs are idempotent, deploying code **after** the Nov 9 07:20 MT report cannot retroactively replace that already-sent message. The first live candidate would be a subsequent scheduled session after merge+deployment, provided seeding/cron/Telegram services work.

## Required checks before rollout

`python3 -m unittest discover -s tests -p 'test_preopen*.py' -v`

Confirm V1 `selected` and technical pass counts remain byte-for-byte logically unchanged for shared fixtures. Verify no leaking current-day bars, mixed prior-session dates, fabricated empty intervals or unjustified `entry_signal` fields. On staging inspect 13-bank DAY/Swing isolation, 07:20 report length (<4000 characters), sample SPY and NVDA seed values, outbox idempotency, and data provider response.

**Do not merge/deploy this draft without passing tests and review.**
