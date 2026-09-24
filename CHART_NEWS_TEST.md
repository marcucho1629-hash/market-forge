# Chart 80 + Bigdata 20 — experimental shadow test

This endpoint never sends Telegram, never calls Alpaca, and does not use an Alpaca score. It does not automatically replace existing TradingView alerts. Scores are experimental ranking inputs, not probabilities.

POST `/scanner/chart-news/shadow` with CRON_SECRET bearer authorization and `observations` containing up to 70 distinct tickers from the existing seven TradingView banks. Ranking applies to the submitted batch, not unobserved tickers. Supply `observed_at`, `sampling` (`intrabar`, `closed_1m`, `closed_5m`), `direction`, and the features below. All direction-sensitive calculations are mirrored for SHORT.

* Trend/relative strength, 25: EMA9 versus EMA21 aligned (8); price versus VWAP aligned (6); `ema9_slope_atr` aligned (5); `relative_return_pct` aligned (6). Relative return must be the symbol return minus benchmark return over the same interval, in percentage points. Missing values must not be replaced with zero.
* Volume/momentum, 25: 10 × min(relative_volume / 3, 1); 10 × min(directional_momentum_atr, 1); 5 × clamp(direction × (RSI − 50) / 20, 0, 1).
* Structure, 20: `breakout_valid` or `pullback_valid` from the chart producer. No double points. These must be evaluated at observation time, without future candles.
* Location, 10: 10 × max(0, 1 − extension_atr / 2). Extension is absolute EMA9 distance divided by ATR.
* Bigdata, 20: strongest reviewed direction-matched news/catalyst score × 0.2; full weight within one hour of publication, half through three hours, then unavailable. A 0–100 evidence review must explain materiality and direction with a source-body excerpt. Never sum overlapping news and catalyst events. These age cutoffs are test parameters, not validated trading advantages.

Candidate condition: chart >= 60 and total >= 80; choppy, wide_whipsaw and extension >= 2 block entries and momentum information. A verified server session is required. Shared budget is three candidate/information selections per five minutes, with a 20-minute ticker cooldown. Missing or unverified Bigdata means null total and no entry; a separately labeled momentum observation may still be selected. Evidence is read only from server storage, not an incoming alert's news fields. No bid/ask validation is claimed; shadow candidates must not be promoted to executable trades.

Authenticated `/scanner/chart-news/documents` stores fetched Bigdata bodies with source URLs and timestamps. `/scanner/chart-news/review` records explicit evidence reviews and quoted supporting text. These are ingestion interfaces, not an automatic Bigdata client. Review scores are human/upstream inputs, not automatically generated news sentiment. No-relevant-news and retrieval failures currently both leave total unavailable; neither receives invented points.

Current verification: synthetic unit/integration tests only, including all 70 symbols, LONG/SHORT symmetry, exact threshold, missing evidence, point-in-time evidence, duplicate events, quotas and no Telegram writes.

Remaining before a live test: wire actual chart features and benchmark-relative returns, source and review real Bigdata records, provide session calendar, deploy and verify these routes. Existing Pine payloads do not yet supply all required features. Live Telegram remains disabled for this experiment. The legacy unfiltered 70-symbol alerts have not been reactivated.

## September 24 connection work

`pine/Market_Forge_V13_29_Chart_News_SHADOW.pine` now emits the required chart features to `/webhook/chart-news` using a body secret. Outgoing alerts default off. Daily ET session gating replaces the expired one-day gate. Relative strength compares matching completed 5m symbol/SPY returns, including matching previous-bar timestamps; unmatched intervals are skipped. Structure uses five-bar range breakout with 0.05 ATR buffer, or trend-aligned EMA9 touch/reclaim above/below VWAP. This is still the confirmed five-minute engine, not intrabar entries.

The receiver validates producer/schema, completed-bar age, benchmark interval, direction and authentication, removes secrets from stored payloads, and evaluates shadow only. No Telegram is queued. Per-alert arrival ordering is not global ranking of 70 symbols.

Vercel project and shared environment-variable search both found no BIGDATA variable. A server API key is required for automatic API calls (official reference: https://docs.bigdata.com/api-rest/authentication). API authentication does not itself implement the news polling/review worker. That worker, session provisioning, live deployment and Telegram receipt remain outstanding.


## One-session runtime test (2026-09-24)
Authenticated control enables delivery only until an explicit expiry within 24 hours. An independently verified market session is also required. Entries require chart>=60, total>=80 and no risk blocks. Separate high-momentum information may be sent without news; it is labeled as not an 80-point entry. A shared maximum of three selections per five minutes and 20-minute ticker cooldown apply.

Pine remains confirmed 5-minute sampling on a 1-minute host. The webhook performs only stored-evidence evaluation and durable queueing, then attempts background Telegram delivery; the worker also drains pending messages. Messages expire after 60 seconds. The worker refreshes at most one recently observed ticker each run, at most once per ten minutes per ticker and 70 refresh attempts per ET day. Newly fetched news applies only to subsequent observations, never retroactively. This is candidate-driven, not continuous polling of 70 news feeds.

Automated news review uses an exact supporting excerpt and a conservative impact/novelty/certainty/directness rubric (0..5 each, multiplied by five to produce 0..100 before the 20% weight). Neutral or unverified evidence does not create news points. These are experimental heuristic scores, not calibrated probabilities. Quote/spread and sector-money-flow verification are not implemented.

Bigdata API offset-free timestamps are normalized to UTC only within the official API adapter, matching bigdata-client 2.21.0 document.py model_post_init. Imported arbitrary timestamps remain unverified. First-seen times are preserved during normalization repair.
