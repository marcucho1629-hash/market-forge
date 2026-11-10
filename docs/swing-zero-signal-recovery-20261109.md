# Market Forge Swing 0-signal recovery | 2026-11-09

**Review branch only. Not deployed; not a claim that TradingView alarms or Telegram are live.**
No changes to DAY, overnight, or long-swing entry thresholds. All swing actions remain paper-comparison information, not orders.

## What changed

1. **Bank isolation.** The authenticated `mf.swing.bank.v1` 1-hour envelope remains strictly validated. An individual malformed event is quarantined and counted as `partial_accepted`, while other valid `PLAN`, `MODEL_ENTRY`, `MODEL_EXIT`, `REJECTED` events from the same bank continue to the outbox. Duplicate event IDs remain idempotent. The original 2026-10-29 XLK internal-calculation clock is still overridden only with the validated outer envelope emit time; source timestamp is retained separately.
2. **Bounded diagnostics.** Admin `/swing-compare` → `100종목 진단 이력` reports the current ET date, 13-bank reporting coverage, missing bank IDs, hourly observations, candidate count, valid queued count, per-event and per-envelope rejections, latest ready symbol counts, first failed-gate counts when Pine supplied `diagnostic`, and same-day delivery status counts. Day-scoped indexed key ranges avoid fetching full lifetime storage. A `ready=true` ticker can still fail another gate.
3. **Repeatable safe Telegram test.** Admin `Telegram 연결 테스트` may enqueue one test in each five-minute window instead of silently reusing one ID all day. It displays the test's outbox state, not just unrelated drain results. `sent` means the Telegram API accepted a send, **not** that it appeared on the recipient's phone.
4. **Pine display.** The uninstalled `diagnostics/swing-bank-observability-DRAFT.pine` dashboard now distinguishes script OFF, missing secret, and script enabled but alert rule unverified. It remains uncompiled and must not be treated as active production.

## No-signal triage for Tuesday 2026-11-10

Regular US stock session 09:30 ET / 07:30 MT. The first standard 60-minute RTH close is 10:30 ET / 08:30 MT.

- **Missing 13 hourly bank heartbeats:** Check actual TradingView alert rules, each bank number 1–13, *standard* `SPY` 60-minute RTH host chart, `sendCompare=true`, configured secret, active nonexpired alert rule, Webhook enabled, `Any alert() function call`, and exact production route `/webhook/swing-bank-v1`. A script showing "ON" does not prove an active TradingView alert.
- **HTTP 401:** Secret mismatch. Don't paste the private key into chat.
- **HTTP 422:** Envelope stale/bad health/identity. Distinguish from `partial_accepted` when one model event is invalid. Read auth-protected rejection counters.
- **13 heartbeats received; 0 candidate events:** Inspect `first_block_counts` and `ready_tickers_latest_by_bank`. Wait for at least 1 full hourly evaluation; do not lower gates blindly. Currently 2–5-day and 5–10-day 1.5R variants are the only emitted comparison signals. A 1.0R/0.5R simulation event is not an alert.
- **Events > 0, `queued` = 0:** Check event quarantine, duplicate IDs, event clock validation.
- **`queued` > 0, `sent` = 0:** Check expiring outbox, Telegram credentials/API errors, Vercel and Neon service availability. Historical Vercel database connection errors were seen through 2026-11-05, but direct causation for swing is unproven.
- **`sent` > 0, user says no message:** Check correct Telegram chat ID, bot permissions, device notifications, and actual message history.

### Product / risk notes
- V1.8 research Pine currently uses `SPY` as both market and sector baseline. That is not an actual per-sector confirmation and should be changed only in a separate calibrated candidate experiment.
- Require same-source multi-session, point-in-time swing replay and forward validation before allowing any threshold relaxation or live trading language.
- Existing production and private settings are untouched. GitHub merge alone will not create the 13 TradingView alert rules.

## Verification

`python3 -m unittest discover -s tests -p test_swing_clock.py -v`

CI file in this PR attempts the same tests. Runner availability is not proof of a passed test. Manually test the authenticated admin panel and observe the first 1-hour heartbeat before approving deployment.
