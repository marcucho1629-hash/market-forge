# Five-Minute Early WATCH | 2026-11-11

## The missing connection

The existing intraday V3 scanner runs every five minutes but only **enqueues a Telegram Top 10 once per hour**, and its confirmed-bar evaluator requires **six 5-minute bars** beginning at 07:30 MT. Therefore it could not notify about the opening 07:45–07:55 move, and already-extended hourly Top 10 candidates could appear too late. These were design limitations, not proof that no valid market candidates existed.

## Isolated fix

**First-wave opening EARLY WATCH** (07:47, 07:52, 07:57 America/Denver):
- Native IEX 5-minute regular-session candles only, first three or more completed and continuous. Never include a future or premarket bar.
- At least three consistent closes, 0.35% directional price movement, 0.20% SPY-relative strength, IEX notional, non-excessive extension from session VWAP and a strong latest-bar close. **Only CHASE LOW**. This is a *heuristic*, not a validated trade signal.
- Up to 4 new names per scan / 8 names per day. Duplicate suppression per ticker for the day. If there are zero valid candidates, send nothing and record reasons.
- Store a separate \`early:open:latest\`, do not replace the 08+ official Top 10 or modify the LIVE MC/SCMP filter, which continues to require current intraday V3 Top 10 and CHASE LOW only after 08:00.
- The cron is extended to 13:00 UTC in summer; existing local-hour/NYSE session guards prevent out-of-window scans or unwanted alerts.

**08:00 onward NEW LOW WATCH** (every five minutes):
- Existing V3 selected Top10 is unchanged. Compare the current qualifying LOW names with previous completed 5-minute LOW selection, and notify *only genuinely new or changed-direction* \`CHASE LOW\` candidates scoring at least 60.
- Hourly first scan initializes its own previous-LOW baseline and sends the **existing hourly report**, not a duplicate low alert. Subsequent 5-minute scans can notify up to 3 new LOW names per scan / 12 names per day. Existing sent names are never repeated during the day, even after bouncing out of the list.
- Experiment in a **separate store transaction**: any LOW alert failure must not block the hourly V3 Top10, V3.1 timeline, MC/SCMP or exit alerts.
- Every Telegram alert is labeled \`EARLY WATCH\` or \`5MIN NEW LOW WATCH\`, states source bar-end time and says **not an entry signal**. No paid Bigdata calls or trading orders.

## Guardrails

- The first 15-minute sample is inherently noisy, 3 bars alone are not a sufficiently validated signal. There is **no forced quota** and no claim that this will capture every winning move.
- IEX volume is not consolidated volume; options spread, earnings risk, company catalyst and Bigdata context remain unverified.
- Do not use EARLY WATCH alerts as substitute for TradingView MC/SCMP or interpreted 3-minute confirmed LIVE signals.
- Compare today/tomorrow alert counts, post-alert +5/15/30-minute movement, false breaks and chop. Tune only after measuring, never loosen filters simply to increase messages.
- 7:20 preopen, 8:00+ hourly Top10, DAY V3.1 Shadow, 1:40 overnight Swing, and X/SX/HX remain intact.

## Testing

Run \`python3 -m unittest discover -s tests -p test_intraday_early_watch.py -v\` plus the existing intraday V3, Top20 and V3.1 regression. Verify Vercel deployment READY and **actual cron \`INTRADAY_V3_CRON\` evidence** (for 07:47/52/57 MT and 08:07+ new LOW). README/READY alone do not prove Telegram has sent.

**Deployment note:** The UTC cron is not local-time scoped; local-hour and NYSE schedule checks in the worker own safety gating and DST behavior.
