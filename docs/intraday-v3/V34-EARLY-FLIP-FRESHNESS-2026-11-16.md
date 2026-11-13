# Market Forge AI DAY V3.4: Nov16 Early FLIP + Freshness SHADOW

**Effective session:** Monday, 2026-11-16, 7:30–14:00 Mountain Time. **Research only**.

## Hypothesis
Nov13 SNDK reversed from an upward first five-minute candle to a strong bearish second candle; existing OPEN RADAR rejected the mixed first-two-bar sequence. NVDA already fell >2.5% in the first candle; it correctly failed the entry chase rule but its important downside momentum was not recorded as a watch. AMD had early directional weakness but insufficient 0.55 candle-body strength. At 8:00 the existing six-completed-bar DAY V3 scanner enters its standard cadence. Hourly `NEW` means absent from previous hourly list, not a freshly begun trend. The live WATCH send quota can be exhausted before mid-session.

## Monday research change
- Add **read-only raw-bar observation tap** to the pre-existing 7:35/7:40 RADAR and 7:45/7:50/7:55 EARLY workers, **only** on Nov16. It records all valid, consecutive, completed IEX 5m OHLC snapshots for the full 150-symbol configured pool, including rejected candidates. A thrown research observer is caught and **must not affect existing Telegram delivery**. No new API calls inside this tap.
- Independently date-gate and persist the every-5m full 150-symbol V3 evaluated states from 8:02 onward, plus whether the current hour actually caused a Top10 message. Research snapshots only.
- Research classifications: `EARLY_FLIP_WATCH` when first two completed 5m candles change direction and the second is strong, `EXTENDED_NO_ENTRY` when session move exceeds absolute 2.5%, `EARLY_MOMENTUM_WATCH` for directional but not extended motion, otherwise `EARLY_UNCONFIRMED`. These do **not** grant CHASE LOW or imply safe short/long entry.
- Compare when the initial setup was *first observed*, how many minutes before hourly Top10, its first 5m strong LOW receipt, and whether hourly entries are older than 15min or already extended by >=6 session ATR. Same ticker can appear on several hourly lists; count these as **hourly appearances**, not unique stocks.
- A rough signed later movement compares prices only at **observed** five-minute scans at least 15min after the first observation; **not exact +15min returns** and not an executable order/fill/slippage model.
- One guarded report and Telegram summary at **14:52 MT Monday only**; `/day-v34-shadow` read-only authenticated page with existing webhook key. No intraday Shadow alerts or orders. No paid Bigdata calls. The Nov11/12/13 research freezes and all existing MC/SCMP/X/SX/HX/day/Swing outputs unchanged.

## Safety and acceptance
- Existing DAY `70+`, momentum `8/15+`, `CHASE LOW`, `CHOPPY`, provider data-quality and option/catalyst caveats retained. Any data gap => do not invent bars or mark as fresh.
- Vercel cron `52 20,21 * * 1-5` UTC with strict local Monday Nov16 14:52 guard. Cron can execute twice on UTC offsets, but only one fits the local date and minute.
- CI validates SNDK mixed-bar early flip, NVDA >2.5% extended no-entry, all candidate capture, single report/Telegram dedup, date guard and optional observer failure isolation. Production READY cannot prove Monday observability until real logs and delivered summary are checked.
- To promote into real intraday Early FLIP alert would require forward session tests, constrained alert quotas, confirmation/anti-chop, MFE/MAE and option liquidity and actual latency validation. **Do not promote automatically**.
