"""V2.2 three-minute EARLY WEAKNESS shadow. Research only; never Telegram or orders.

This detector is independent of the five-minute confirmed Flip delivery gate.
Only tracks directions with a verified Telegram-SENT outbox entry. It consumes
three completely finished, contiguous Alpaca IEX 1Min bars, never premarket
bars, never unobserved prices and never future data.
"""
import hmac
import json
import os
from datetime import datetime, timedelta, timezone
from fastapi import HTTPException, Request

from .free_iex import DENVER, bounds, calendar, utc
from .preopen.core import stamp, validate
from .preopen.runtime import fetch
from .v21_direction import sent_directions

PREFIX = "v22-weakness-3m:"
BEGIN = "2026-11-24"
MIN_OPPOSITE_PCT = 0.40
MAX_TRACKED = 60
OPEN_OFFSET_MINUTES = 18  # first 07:48 MT cutoff after 07:45 Early signal


def slot(now):
    """3-minute MT grid, only 07:48-13:57 on exchange weekdays."""
    local = now.astimezone(DENVER)
    if local.weekday() >= 5 or local.date().isoformat() < BEGIN:
        return None
    minute = local.hour * 60 + local.minute
    if not 7*60 + 48 <= minute <= 13*60 + 57:
        return None
    if (minute - (7*60 + 30)) % 3:
        return None
    return local.replace(second=0, microsecond=0).astimezone(timezone.utc)


def analyze_bar(symbol, side, raw, opened, cutoff):
    start = cutoff - timedelta(minutes=3)
    try:
        bars = validate(raw, 1, opened, cutoff)
    except (ValueError, KeyError, TypeError, OverflowError):
        return dict(ticker=symbol, side=side, quality="INVALID_1M", weakness=False)
    recent = [x for x in bars if start <= stamp(x["t"]) < cutoff]
    target = [start + timedelta(minutes=i) for i in range(3)]
    if len(recent) != 3 or [stamp(x["t"]) for x in recent] != target:
        return dict(ticker=symbol, side=side, quality="MISSING_1M", weakness=False)
    signed = 1 if side == "BULLISH" else -1
    first, last = recent[0]["o"], recent[-1]["c"]
    vol = sum(x["v"] for x in recent)
    vwap = sum(x["vw"]*x["v"] for x in recent)/vol
    changes = [recent[i]["c"]-recent[i-1]["c"] for i in (1,2)]
    against = all(signed*d < 0 for d in changes)
    moved = signed*100*(last/first-1)
    across_vwap = signed*(last-vwap) < 0
    weak = against and moved <= -MIN_OPPOSITE_PCT and across_vwap
    return dict(ticker=symbol, side=side, quality="READY",
                weakness=weak, price=round(last,4), vwap=round(vwap,4),
                original_side_move_pct=round(moved,3),
                opposite_consecutive_closes=against,
                vwap_opposite=across_vwap, bar_cutoff=cutoff.isoformat())


def worker(store, now):
    cutoff = slot(now)
    if not cutoff:
        return dict(status="outside_3m_shadow_window", orders=0)
    day = now.astimezone(DENVER).date().isoformat()
    market, _ = calendar(now)
    if not market or market.get("date") != day:
        return dict(status="market_closed", orders=0)
    opened, closed = bounds(market)
    if not opened + timedelta(minutes=OPEN_OFFSET_MINUTES) <= cutoff <= closed:
        return dict(status="outside_regular_session", orders=0)
    key = PREFIX + day + ":" + str(int(cutoff.timestamp()))
    with store.transaction() as tx:
        if tx.get(key):
            return dict(status="already_captured", orders=0)
        delivered = sent_directions(tx, day)
        latest = {}
        for e in delivered:
            if datetime.fromisoformat(e["at"]) <= cutoff:
                latest[e["ticker"]] = e
        monitored = sorted(latest)[:MAX_TRACKED]
    if not monitored:
        record = dict(at=now.isoformat(),cutoff=cutoff.isoformat(),
                      status="no_verified_sent_signals",rows=[],
                      telegram=False,orders=0)
        with store.transaction() as tx:
            if not tx.get(key): tx.put(key,record)
        return dict(status=record["status"], tracked=0, flagged=0, orders=0)
    try:
        bars = fetch(monitored, "1Min", cutoff-timedelta(minutes=3), cutoff)
    except Exception:
        # Market-data/network failures must not generate a synthetic weakness.
        return dict(status="iex_1m_unavailable", tracked=len(monitored),orders=0)
    results = []
    for ticker in monitored:
        event = latest[ticker]
        # Start of the three-minute window must not precede the original live
        # Telegram observation; this prevents hindsight labels.
        first = cutoff-timedelta(minutes=3)
        if datetime.fromisoformat(event["at"]) > first:
            results.append(dict(ticker=ticker,side=event["side"],
                                quality="PRE_SIGNAL_WINDOW",weakness=False))
            continue
        result = analyze_bar(ticker,event["side"],bars.get(ticker,[]),opened,cutoff)
        result["source"] = event["source"]
        result["first_sent_observed_at"] = event["at"]
        results.append(result)
    record = dict(day=day, observed_at=now.isoformat(), cutoff=cutoff.isoformat(),
                  feed="IEX_1MIN", frame="3x1Min", mode="SHADOW_ONLY",
                  rows=results, telegram=False, orders=0,
                  options_liquidity="NOT_VERIFIED", catalyst="UNKNOWN")
    with store.transaction() as tx:
        if tx.get(key):
            return dict(status="already_captured",orders=0)
        tx.put(key,record)
        tx.put(PREFIX + day + ":latest",record)
    return dict(status="captured", tracked=len(monitored),
                flagged=sum(x.get("weakness") is True for x in results),
                missing=sum(x["quality"]!="READY" for x in results),
                cutoff=cutoff.isoformat(), telegram=False, orders=0)


def install(app, storage):
    @app.get("/scanner/v22/weakness-3m-shadow/worker")
    def scheduled(request: Request):
        secret = os.getenv("CRON_SECRET","")
        if not secret or not hmac.compare_digest(
                request.headers.get("authorization",""),"Bearer "+secret):
            raise HTTPException(401,"Unauthorized")
        result=worker(storage(),utc())
        print("V22_3M_WEAKNESS_SHADOW "+json.dumps(result,sort_keys=True),flush=True)
        return result

    @app.get("/scanner/v22/weakness-3m-shadow/status")
    def status(request: Request):
        secret = os.getenv("CRON_SECRET","")
        if not secret or not hmac.compare_digest(
                request.headers.get("authorization",""),"Bearer "+secret):
            raise HTTPException(401,"Unauthorized")
        day=utc().astimezone(DENVER).date().isoformat()
        with storage().transaction() as tx:
            latest=tx.get(PREFIX+day+":latest")
        return dict(day=day,latest=latest if latest else {"status":"not_captured"})
