# MC / SCMP: Scanner Top 10-only Telegram Gate

Scope: only newly generated MC for current Bullish Top 10, and newly generated SCMP for current Bearish Top 10.

- Applies immediately before Telegram transport, using the latest *current-day* Intraday V3 snapshot refreshed on the 5-minute cadence.
- Gate requires snapshot freshness <=11 minutes, bar-cutoff freshness <=16 minutes, and current candidate eligible=true, chase not HIGH, correct ticker and direction.
- During 07:30–07:59 MT only, use the current day's 07:20 list: the V2 prior-RTH WATCH picks (or V1 verified picks). Those are **prior-day research context, not same-day verified trade entries**. From 08:00 MT onward, use only the refreshed Intraday V3 5-minute snapshot.
- Fail closed from 08:00 MT if snapshot is stale, missing or inconsistent. This avoids outside-Top10 alerts but may yield zero MC/SCMP during an outage. Previous-day V2 candidates are never substituted after 08:00.
- X, SX, HX, DATA_CONFLICT, existing confirmation/cancel/status messages, and all unrelated Swing or Pre-Open messages remain unaffected.
- Outbox entries blocked by this gate are marked filtered_top20, not sent; legacy direct webhook reports filtered_top20 as status.
- Enabled by default. Authenticated /intraday-v3 panel, with the existing Webhook key, provides emergency filter OFF/ON toggles. Never paste keys into chat.
- This cannot create MC/SCMP for tickers outside actual installed TradingView bank coverage. The V3 150-symbol scanner covers more names than some Pine engines.
- Does not alter DAY or Swing entry generation or X/SX logic, charge Bigdata credits, or verify options spreads.
- Current producer first-line MC/SCMP entries are classified; future message formats must be added to the classifier so no bypass occurs. Future hardening: use explicit typed outbound event metadata.

Verify in PR CI: python3 -m unittest discover -s tests -p test_top20_entry_gate.py -v

Before trusting a live run, check /intraday-v3 gate status and current Top 10; inspect API send/filtered states and verify X/SX unaffected. A READY deployment is not proof of an end-to-end Telegram signal.
