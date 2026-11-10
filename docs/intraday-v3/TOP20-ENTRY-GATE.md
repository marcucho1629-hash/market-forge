# MC / SCMP: Top 10 + CHASE LOW-only Telegram entry gate

**Only new MC for Bullish Top 10 + CHASE LOW, and new SCMP for Bearish Top 10 + CHASE LOW.** MED is still on the Top 10 research WATCH list but is not eligible for MC/SCMP Telegram entry. HIGH and CHOPPY remain disqualified by the Scanner.

- Runs immediately before Telegram transport from a current-day V3 snapshot refreshed every 5 minutes. Requires valid candidate, corresponding direction, eligible=true, choppy=false, late_chase=false, chase exactly LOW, snapshot age <=11 minutes and completed-bar cutoff age <=16 minutes.
- During **07:30–07:59 MT** the previous-day 07:20 WATCH has no verified same-day CHASE LOW. Thus all classified new MC/SCMP entry notifications are withheld during this window. Starting with a valid Intraday V3 scan from 08:00 MT onward, new MC/SCMP can be delivered if LOW and all other gates pass. We do not infer LOW from prior-day data.
- Chasing reflects the *distance/impulse relative to ATR, EMA9 and VWAP*, not momentum strength alone. Stronger acceleration can result in MED/HIGH. A consolidation or pullback can reduce chase to LOW, but the candidate must still be eligible and in the correct directional Top 10.
- **No retroactive send:** an MC/SCMP event blocked when MED is not queued for later delivery when MED turns LOW. Only a *freshly generated* MC/SCMP at the time of LOW can be sent. Scanner WATCH updates do not manufacture MC/SCMP.
- X, SX, HX, DATA_CONFLICT and other protection/exit, status, confirmation/cancellation, Swing, Pre-Open and overnight messages are unchanged.
- Filtered entries have auditable outbox status filtered_top20, not sent. Scanner itself and trading/chart calculations are unmodified.
- The authenticated /intraday-v3 panel shows the required chase=LOW and matching entry-ready subsets. The existing emergency OFF switch bypasses **both** Top 10 and LOW restrictions; re-enable when safe. Do not share webhook/API credentials.
- A missing or stale scanner snapshot fails closed for new entry Telegram alerts. Installed TradingView coverage remains independent of the Scanner's 150-symbol universe.
- No additional Bigdata calls or verified options spread; LOW is **not** a win-rate guarantee or evidence that a setup is safe.

Validation: run test_top20_entry_gate.py, test_intraday_v3.py and existing preopen/overnight tests in CI, verify production READY on exact merge SHA. After deployment, inspect authenticated gate status and new real Telegram logs. A READY deployment is not an end-to-end delivery proof.
