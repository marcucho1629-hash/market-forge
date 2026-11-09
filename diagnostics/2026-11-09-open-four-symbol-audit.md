# Market Forge V3 Shadow | 2026-11-09 opening audit (SPY, QQQ, AVGO, MSFT)

**Status:** read-only market-data investigation, not a signal backtest or live delivery audit. This file documents research evidence to guide 2026-11-10 forward observation; production code, TradingView alerts, CRON, DB and Telegram are unchanged.

## Evidence and scope

- Price source: Alpaca 1-minute **SIP** OHLCV for Monday, 2026-11-09, 09:30–09:44 **America/New_York**. SIP is **not** the same feed as Pine's `BATS:` source. Reconcile using same-feed bars before assigning exact signal eligibility.
- Metric: 09:30 bar **open** to the 09:44 bar **close**. Return = 100 × (close / open − 1). Does not imply executable trade profit.
- TradingView screenshot from owner shows 5-minute AVGO/MSFT chart and MC/SCMP/X markers. Marker presence is **not** proof that a particular Telegram message was generated, accepted, or delivered. Chart table `WEBHOOK ON` means Pine setting enabled, not verified end-to-end connection.
- No authenticated production database/outbox or TradingView Alert Log was available for this audit. **Do not label individual symbol events "blocked by GitHub/server" without those records.**

## Opening statistics

| Ticker | 09:30 open | 09:30 close | 09:32 close | 09:34 close | 09:44 close | Open → 09:44 | Pattern |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| SPY | 741.95 | 741.34 | 742.60 | 743.37 | 746.66 | +0.635% | First minute down; sustained rise begins thereafter |
| QQQ | 662.63 | 662.445 | 664.23 | 665.46 | 670.5474 | +1.195% | First minute slightly down; multi-minute acceleration |
| AVGO | 305.00 | 305.92 | 306.41 | 310.34 | 313.455 | +2.772% | Large impulse at 09:33 and follow-through |
| MSFT | 544.08 | 546.795 | 544.29 | 544.89 | 543.84 | −0.044% | Opening spike rapidly faded; avoid indiscriminate 1-bar MC |

Key close-to-close examples from same data: AVGO 09:33 bar opened 306.19 and closed 309.65 (+1.13% intrabar body); QQQ 09:33 opened 664.3111 and closed 665.62 (+0.197%); SPY 09:32 opened 741.62 and closed 742.60 (+0.132%). These are **completed one-minute candles**, not intraminute timestamps.

## Candidate failure modes, not yet verified causes

1. Pine scanner V13.30/31/32/33 reads per-ticker `request.security(..., "5", ...)`; a 1m host chart does **not** make the ticker signal engine 1m.
2. Research V13.36/39 requires aligned 1m+2m+3m entry predicates in a 3m cycle. `Window` needs prior bars to fill ATR / volume / prior-high references. Missing seed can suppress early-session eligibility even with a strong candle.
3. V13.30 server has separate `bigdata_missing_or_unverified`, cooldown and 5-minute batch quota blocks; active production routing version must first be identified.
4. V13.36 research path can label events `stale` when final 1m bar arrives too late; data gaps/halt are separate from notification-only `DATA_GAP` suppression in unmerged PR #8.
5. V13.31 relay has `shadow` (default), `live`, pending-expiry and delivery states. A chart plot is not proof of a sent message.
6. In the photographed Pine charts, Bank 7's `WEBHOOK ON` does not establish AVGO/MSFT coverage. V13.30/31 bank 1 includes SPY, QQQ, AVGO and MSFT.

## Forward comparison on Tuesday 2026-11-10

Use same-source, pre-existing completed bars; record *first observed* timestamps separately from bar end (never derive latency from historical backfill). Capture:

| Stage | Required records |
| --- | --- |
| Source | ticker, feed, source 1m bar start/end, actual first receipt, freshness, missing candles |
| Pine | bank, source version, `WEBHOOK ON`, actual TradingView Alert Log event, `chart_mc` / `chart_scmp`, `early_*_raw`, `f_ready` / false reason |
| Transport | webhook HTTP status, server receipt timestamp, schema validation, source/version, first/last heartbeat |
| Gates | mode (live/shadow), warmup/seed count, bars missing, premarket vs RTH reset, ATR/body/RVOL, first gate failed and *all* failed gates |
| Outbox | event ID, queued/attempting/expired/sent, expiry timestamp, Telegram API acceptance; don't equate API acceptance with user receipt |
| Outcome | WATCH/provisional/confirmed timing, no-chase threshold, 1/3/5/10-min post-event MFE/MAE and false-breakout rate; use no future information for candidate generation |

Test both continuation paths (SPY, QQQ, AVGO-like) and reversal paths (MSFT-like), bullish and bearish. Maintain **no fabricated bars, no bypass of DATA_CONFLICT, no forced retroactive entry**. Compare against existing V13.30/31 and V13.36/39 from same feeds. New V3 WATCH/PROVISIONAL events are research labels, not MC/SCMP trading alerts. Changes to alert routing require a separate reviewed deployment.

## Decision

Before any live V3 activation, show multi-day same-feed baseline and counterfactual gate-by-gate replay, separate alert-generation gaps from delivery gaps, and report chase rate, latency, MFE/MAE and false positives without cherry-picking four symbols. Preserve the existing safe production engine until reviewed.
