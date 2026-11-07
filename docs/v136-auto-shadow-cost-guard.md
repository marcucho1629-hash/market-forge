# Research V13.39 per-webhook Shadow replay cost guard

**DRAFT ONLY, production has not changed.**

The existing production webhook currently performs (1) synchronous ingest and (2) background Telegram delivery plus (3) background full-session Shadow replay on every webhook with accepted new 1m bars. The shadow task reads every symbol's cumulative 1m bars and rewrites a research report, potentially producing high Neon DB network egress.

This change:
- Preserves ingest, Telegram delivery and existing authenticated manual `shadow_run` endpoint exactly.
- Disables the expensive automatic full-session replay by default, requiring the literal environment value `MF_V136_AUTO_SHADOW=true` to opt in.
- Does not delete saved data. Manual replay and status remain available.
- No new cron/Telegram messages; no credentials are read.

Important tradeoff: with the flag unset, automatic per-webhook Shadow telemetry/report refresh is paused. There is no guarantee that this change alone resolves the Neon network quota issue, and connection/transfer volumes must be measured before and after any deployment. Production promotion requires approval and a targeted smoke test.

Recommended later improvement is incremental per-bar Shadow state updates plus bounded retention and a separately scheduled full-session comparison, after performance validation.

Local test command: `python -m unittest discover -s tests -p test_v136_auto_shadow_optin.py -v`
