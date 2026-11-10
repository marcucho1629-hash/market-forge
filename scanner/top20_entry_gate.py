"""Restrict newly sent MC / SCMP Telegram entries to current V3 Top 10 by side.

This is a notification gate, NOT a TradingView/Pine/order/exit engine change.
Always keep X, SX, HX and existing lifecycle/protection notifications.
Fail closed for classified new entries when V3 is missing/stale; preserve an
explicit authenticated operator emergency disable switch.
"""
import re
from datetime import datetime, timezone
from .core import ET, stamp

CONTROL_KEY="top20-entry-gate:control"
PREFIX="intraday-v3:"
MAX_SNAPSHOT_AGE_SECONDS=660

# Only new entry-message forms, never CONFIRM / CANCEL / X / SX / HX,
# status, DATA_GAP or other messages mentioning an old MC/SCMP.
FIRST_LINE_PATTERNS=[
    re.compile(r"^(?!TEST\b)[^\s]+\s+([A-Z][A-Z0-9.]{0,8})\s+(MC|SCMP)\s+·"),
    re.compile(r"^(?:🚀|🚨)\s+([A-Z][A-Z0-9.]{0,8})\s+\|\s+(MC|SCMP)\s+\|"),
    re.compile(r"^DAY TEST\s+([A-Z][A-Z0-9.]{0,8})\s+(MC|SCMP)\s*$"),
    re.compile(r"^TEST\s+⚡\s+([A-Z][A-Z0-9.]{0,8})\s+EARLY\s+(MC|SCMP)(?:\s|$)"),
    re.compile(r"^TEST\s+([A-Z][A-Z0-9.]{0,8})\s+⚡\s+EARLY\s+(MC|SCMP)(?:\s|$)"),
    re.compile(r"^TEST\s+·\s+([A-Z][A-Z0-9.]{0,8})\s+EARLY\s+(MC|SCMP)(?:\s|$)"),
]

def entry_from_text(message):
    if not isinstance(message,str) or not message:return None
    lines=message.splitlines()
    if not lines:return None
    first=lines[0].strip()
    # The free-IEX TEST paper stream puts the new signal on the second line.
    if first.startswith("TEST · IEX ·") and len(lines)>1:
        m=re.fullmatch(r"([A-Z][A-Z0-9.]{0,8})\s+(MC|SCMP)\s+·.*",lines[1].strip())
        if m:return m.group(1),m.group(2)
    for regex in FIRST_LINE_PATTERNS:
        m=regex.match(first)
        if m:return m.group(1),m.group(2)
    return None

def evaluate(tx,ticker,action,now,allow_off=True):
    """Read-only decision from the most recent, timestamped, validated Top20 WATCH.
    No use of prior-day V2 candidates or stale hourly summaries as a fallback.
    """
    if action not in ("MC","SCMP"):return dict(allow=True,reason="not_entry")
    cfg=tx.get(CONTROL_KEY,{})
    if cfg.get("enabled") is False and allow_off:
        return dict(allow=True,reason="operator_disabled")
    day=now.astimezone(ET).date().isoformat()
    # 07:30-07:59 MT: the 07:20 prior-session watch does not contain a
    # current-day CHASE LOW classification. Never infer LOW from old candles.
    # Once the first 08:00 V3 sample completes, rely solely on live snapshots.
    from zoneinfo import ZoneInfo
    local=now.astimezone(ZoneInfo("America/Denver"))
    if local.hour==7 and local.minute>=30:
        return dict(allow=False,reason="chase_low_unverified_before_0800")
    snap=tx.get(PREFIX+day+":latest",{})
    if not snap or snap.get("day")!=day or snap.get("mode")!="WATCH_ONLY":
        return dict(allow=False,reason="missing_current_day_snapshot")
    try:
        seen=stamp(snap["at"])
        cutoff=stamp(snap["bar_cutoff"])
        age=(now-seen).total_seconds()
        bar_age=(now-cutoff).total_seconds()
    except (KeyError,ValueError,TypeError):
        return dict(allow=False,reason="invalid_snapshot_clock")
    if not (0<=age<=MAX_SNAPSHOT_AGE_SECONDS and 0<=bar_age<=MAX_SNAPSHOT_AGE_SECONDS+300):
        return dict(allow=False,reason="stale_snapshot")
    if snap.get("ready_count",0)<1 or not isinstance(snap.get("selected"),list):
        return dict(allow=False,reason="invalid_snapshot_quality")
    expected="BULLISH" if action=="MC" else "BEARISH"
    side=[r for r in snap["selected"] if isinstance(r,dict) and r.get("direction")==expected]
    if len(side)>10 or len(side)!=len({r.get("ticker") for r in side}):
        return dict(allow=False,reason="invalid_snapshot_rank")
    for r in side:
        if r.get("ticker")!=ticker:
            continue
        if r.get("eligible") is not True or r.get("choppy") is True or r.get("late_chase") is True:
            return dict(allow=False,reason="candidate_not_eligible")
        if r.get("chase")!="LOW":
            return dict(allow=False,reason="chase_not_low",chase=r.get("chase"))
        return dict(allow=True,reason="top10_low_matched",score=r.get("score"),
                    chase="LOW",asof=snap["bar_cutoff"])
    return dict(allow=False,reason="not_in_directional_top10")

def telegram_decision(store,message,now):
    match=entry_from_text(message)
    if not match:return dict(entry=False,allow=True,reason="non_entry")
    ticker,code=match
    try:
        with store.transaction() as tx:
            result=evaluate(tx,ticker,code,now)
    except Exception:
        result=dict(allow=False,reason="gate_storage_unavailable")
    return dict(entry=True,ticker=ticker,code=code,**result)

def set_enabled(store,enabled,now):
    if type(enabled) is not bool:raise ValueError("boolean required")
    with store.transaction() as tx:
        control={"enabled":enabled,"updated_at":now.isoformat(),"description":"MC only Bullish10 LOW; SCMP only Bearish10 LOW"}
        tx.put(CONTROL_KEY,control)
        return control

def status(store,now):
    with store.transaction() as tx:
        control=tx.get(CONTROL_KEY,{})
        snap=tx.get(PREFIX+now.astimezone(ET).date().isoformat()+":latest",{})
        updated=stamp(snap["at"]) if snap.get("at") else None
        return dict(enabled=control.get("enabled",True),explicit_override="enabled" in control,
                    last_snapshot=snap.get("bar_cutoff"),last_refresh=snap.get("at"),
                    refresh_age_sec=(now-updated).total_seconds() if updated else None,
                    top10_bullish=[r["ticker"] for r in snap.get("selected",[]) if r.get("direction")=="BULLISH"],
                    top10_bearish=[r["ticker"] for r in snap.get("selected",[]) if r.get("direction")=="BEARISH"],
                    entry_filter_only=True,exits_untouched=True,required_chase="LOW",
                    note="New MC/SCMP requires current directional V3 Top10 AND CHASE LOW. MED stays WATCH-only. Before 08:00 MT LOW is unverified, so entry notifications are filtered. X/SX/HX always pass.")
