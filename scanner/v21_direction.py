"""Market Forge V2.1: sent-only premarket recheck and 5-minute flip warnings.

Reads only completed IEX scanner records. WATCH notifications only: no
entries, no orders, no changes to legacy DAY/Pine/MC/SCMP/X/SX/HX.
"""
import hmac
import json
import os
from datetime import datetime, timedelta
from fastapi import HTTPException, Request
from .free_iex import DENVER, utc
from .store import drain

START = "2026-11-23"
STAGES = ("MOMENTUM_0733", "POSSIBLE_FLIP_0735", "CONFIRM_0740", "EARLY_150_0745")
SIDES = {"BULLISH", "BEARISH"}
QUALIFIED_0740 = {"EARLY_MOMENTUM_WATCH", "EARLY_FLIP_WATCH"}
MAX_WEAKNESS_DAILY = 12   # Noncritical one-bar warnings cannot exhaust Flip capacity.
MAX_CONFIRMED_FLIP_DAILY = 40  # Independent of WEAKNESS; each sent ticker flips at most once.
MAX_SCAN = 4


def active(now):
    local = now.astimezone(DENVER)
    return local.weekday() < 5 and local.date().isoformat() >= START


def sent(tx, event_id):
    row = tx.execute("SELECT status FROM mf_outbox WHERE id=?", (event_id,)).fetchone()
    return bool(row and row[0] == "sent")


def classify(preselected, early_rows):
    observed = {x.get("ticker"): x for x in early_rows if isinstance(x, dict)}
    result = []
    for p in preselected:
        side = p.get("direction")
        ticker = p.get("ticker")
        if not ticker or side not in SIDES:
            continue
        row = observed.get(ticker, {})
        current = row.get("side")
        state = row.get("state", "MISSING")
        if current in SIDES and state in QUALIFIED_0740:
            outcome = "CONFIRMED" if side == current else "FLIPPED"
        elif current in SIDES and current != side:
            outcome = "WEAKNESS"
        elif state in ("EARLY_UNCONFIRMED", "EXTENDED_NO_ENTRY"):
            outcome = "WEAKNESS"
        else:
            outcome = "NO_CONFIRM"
        result.append(dict(ticker=ticker, premkt_side=side,
                           observed_side=current if current in SIDES else "NEUTRAL",
                           state=state, status=outcome, price=row.get("price")))
    return result


def confirmation_text(day, rows):
    lines = [f"MARKET FORGE AI | 07:40 PREMARKET RECHECK | {day}",
             "07:20 WATCH → 07:40 완료 5분봉 구조 검증",
             "관측 기준 IEX · 매매 진입/청산 신호 아님",
             "Bigdata 촉매/옵션 유동성 미검증", ""]
    for label in ("CONFIRMED", "FLIPPED", "WEAKNESS", "NO_CONFIRM"):
        group = [r for r in rows if r["status"] == label]
        lines.append(f"{label} {len(group)}")
        for p in group:
            before = "🟢" if p["premkt_side"] == "BULLISH" else "🔴"
            after = "🟢" if p["observed_side"] == "BULLISH" else "🔴" if p["observed_side"] == "BEARISH" else "⚪"
            lines.append(f'{p["ticker"]} {before}→{after} · {p["state"]}'
                         + (f' · USD {p["price"]:.2f}' if isinstance(p["price"], (int, float)) else ""))
        lines.append("")
    lines.append("NO_CONFIRM = 실제 방향 확정 불가, 추격 금지")
    output = "\n".join(lines)
    if len(output) > 3900:
        raise ValueError("v21_confirm_message_length")
    return output


def confirm_worker(store, send, now):
    local = now.astimezone(DENVER)
    if not active(now) or local.hour != 7 or local.minute not in (41, 42, 43):
        return {"status": "outside_0740_recheck"}
    day = local.date().isoformat()
    root = "v21-direction:" + day + ":"
    with store.transaction() as tx:
        if tx.get(root + "confirm:report"):
            return {"status": "already_recorded"}
        pre = tx.get("preopen-watch-v1:" + day + ":report")
        early = tx.get("early-schedule-shadow:" + day + ":CONFIRM_0740")
        if not pre or not sent(tx, "preopen-watch-v1:" + day + ":watchlist"):
            return {"status": "preopen_report_or_delivery_missing"}
        if not early or early.get("input_frame") != "5Min" or not early.get("rows"):
            return {"status": "early_0740_missing"}
        if not pre.get("selected"):
            return {"status": "preopen_no_selected"}
        rows = classify(pre["selected"], early["rows"])
        report = dict(day=day, observed_at=now.isoformat(),
                      cutoff=early.get("cutoff"), rows=rows,
                      entry_signal=False, orders=0, feed="IEX")
        tx.put(root + "confirm:report", report)
        tx.enqueue(root + "confirm:msg", dict(
            text=confirmation_text(day, rows),
            expires_at=(now + timedelta(minutes=4)).isoformat()))
    delivered = drain(store, send, prefix=root + "confirm:msg", limit=1, now=now)
    with store.transaction() as tx:
        report = tx.get(root + "confirm:report")
        report["delivery"] = [x.get("status") for x in delivered]
        tx.put(root + "confirm:report", report)
    return {"status": "recorded", "total": len(rows),
            "confirmed": sum(p["status"] == "CONFIRMED" for p in rows),
            "flipped": sum(p["status"] == "FLIPPED" for p in rows),
            "delivery_states": [x.get("status") for x in delivered]}


def sent_directions(tx, day):
    """Only verified Telegram SENT events are eligible for follow-up alerts."""
    found = []
    for stage in STAGES:
        record = tx.get("early-schedule-shadow:" + day + ":" + stage)
        if not record or not sent(tx, "early-direction-live:" + day + ":" + stage):
            continue
        for p in record.get("live_candidates", []):
            if p.get("side") in SIDES:
                found.append(dict(ticker=p["ticker"], side=p["side"],
                                  first_price=p.get("price"),
                                  at=record["observed_at"], source=stage))
    root = "intraday-v3:" + day + ":"
    opening = tx.get(root + "early:open:latest")
    if opening and opening.get("telegram_queued") and opening.get("bar_cutoff"):
        bucket = int(datetime.fromisoformat(opening["bar_cutoff"]).timestamp()) // 300
        if sent(tx, root + "early:open:msg:" + str(bucket)):
            for p in opening.get("selected", []):
                if p.get("direction") in SIDES:
                    found.append(dict(ticker=p["ticker"], side=p["direction"],
                                      first_price=p.get("price"), at=opening["at"],
                                      source="OPEN_EARLY"))
    for record in tx.get(root + "early:live:history", []):
        if not record.get("asof") or not record.get("candidates"):
            continue
        bucket = int(datetime.fromisoformat(record["asof"]).timestamp()) // 300
        if sent(tx, root + "early:live:msg:" + str(bucket)):
            for p in record["candidates"]:
                if p.get("direction") in SIDES:
                    found.append(dict(ticker=p["ticker"], side=p["direction"],
                                      first_price=None, at=record["at"],
                                      source="FIVE_MIN_LOW"))
    confirmed = tx.get("v21-direction:" + day + ":confirm:report")
    if confirmed and sent(tx, "v21-direction:" + day + ":confirm:msg"):
        for p in confirmed["rows"]:
            if p["status"] in ("CONFIRMED", "FLIPPED") and p["observed_side"] in SIDES:
                found.append(dict(ticker=p["ticker"], side=p["observed_side"],
                                  first_price=p.get("price"), at=confirmed["observed_at"],
                                  source="PREMARKET_RECHECK"))
    return sorted(found, key=lambda x: (x["at"], x["ticker"]))


def step_direction(entry, row, cutoff):
    last = entry.get("last_bar")
    if last and cutoff <= datetime.fromisoformat(last):
        return entry, None
    gap = cutoff - datetime.fromisoformat(last) if last else None
    previous = entry.get("opposite_streak", 0)
    entry["last_bar"] = cutoff.isoformat()
    if not row or row.get("quality") != "READY" or row.get("choppy"):
        entry["opposite_streak"] = 0
        return entry, None
    if row.get("bar_end") != cutoff.isoformat():
        entry["opposite_streak"] = 0
        return entry, None
    current = row.get("direction")
    if current == entry["side"] or current not in SIDES:
        entry["opposite_streak"] = 0
        return entry, None
    price, vwap = row.get("price"), row.get("vwap")
    if not isinstance(price, (int, float)) or not isinstance(vwap, (int, float)):
        entry["opposite_streak"] = 0
        return entry, None
    if (current == "BULLISH" and price <= vwap) or (current == "BEARISH" and price >= vwap):
        entry["opposite_streak"] = 0
        return entry, None
    entry["opposite_streak"] = previous + 1 if gap == timedelta(minutes=5) else 1
    if entry["opposite_streak"] >= 2 and not entry.get("flipped"):
        return entry, "CONFIRMED_FLIP"
    if entry["opposite_streak"] == 1 and not entry.get("warned"):
        return entry, "WEAKNESS"
    return entry, None


def flip_text(now, cutoff, events):
    local, bar = now.astimezone(DENVER), cutoff.astimezone(DENVER)
    lines = [f"MARKET FORGE AI | FLIP WATCH {local:%H:%M} MT",
             f"IEX 완료 5분봉 {bar:%H:%M} MT · 실제 발송 방향만 추적",
             "WATCH 경고만, 매매 청산·반대 진입 명령 아님", ""]
    for e in events:
        tag = "🚨 CONFIRMED FLIP" if e["kind"] == "CONFIRMED_FLIP" else "⚠️ WEAKNESS"
        lines.append(f'{tag} | {e["ticker"]} | {e["original"]} → {e["current"]}'
                     + (f' | USD {e["price"]:.2f}' if isinstance(e.get("price"), (int, float)) else ""))
    lines.extend(["", "CHOP 제외, 5분 완료봉·VWAP/EMA 방향 확인",
                  "Bigdata 촉매·옵션 유동성 미검증 · 주문 없음"])
    msg = "\n".join(lines)
    if len(msg) > 3900:
        raise ValueError("v21_flip_message_length")
    return msg


def monitor_worker(store, send, now):
    local = now.astimezone(DENVER)
    if not active(now) or not 8 <= local.hour <= 13:
        return {"status": "outside_monitor_hours"}
    day = local.date().isoformat()
    prefix = "v21-direction:" + day + ":"
    with store.transaction() as tx:
        snap = tx.get("intraday-v3:" + day + ":latest")
        if not snap or not snap.get("at") or not snap.get("bar_cutoff"):
            return {"status": "missing_intraday"}
        observed = datetime.fromisoformat(snap["at"])
        if not timedelta(0) <= now - observed <= timedelta(minutes=3):
            return {"status": "stale_intraday"}
        cutoff = datetime.fromisoformat(snap["bar_cutoff"])
        bucket = str(int(cutoff.timestamp()))
        if tx.get(prefix + "monitor:bucket:" + bucket):
            return {"status": "already_monitored"}
        state = tx.get(prefix + "monitor:state", {})
        for e in sent_directions(tx, day):
            old = state.get(e["ticker"])
            if not old or old["side"] != e["side"]:
                state[e["ticker"]] = dict(side=e["side"], at=e["at"], source=e["source"],
                    first_price=e.get("first_price"), last_bar=None,
                    warned=False, flipped=False, opposite_streak=0)
        rows = {p["ticker"]: p for p in snap.get("rows", []) if p.get("ticker")}
        notices = tx.get(prefix + "monitor:notices", [])
        # Separate daily budgets: a flood of early WEAKNESS must never
        # suppress later CONFIRMED FLIP notices.  Logged counts are by
        # candidate, not by Telegram message.
        used_weak = sum(sum(item["kind"] == "WEAKNESS" for item in record["items"])
                        for record in notices)
        used_flip = sum(sum(item["kind"] == "CONFIRMED_FLIP" for item in record["items"])
                        for record in notices)
        budgets = {"WEAKNESS": max(0, MAX_WEAKNESS_DAILY - used_weak),
                   "CONFIRMED_FLIP": max(0, MAX_CONFIRMED_FLIP_DAILY - used_flip)}
        proposals = []
        for ticker in sorted(state):
            entry, kind = step_direction(state[ticker], rows.get(ticker), cutoff)
            if kind:
                row = rows.get(ticker, {})
                proposals.append(dict(ticker=ticker, kind=kind,
                    original=entry["side"], current=row["direction"],
                    price=row.get("price")))
            state[ticker] = entry
        # High-severity confirmed reversals have first claim on the per-scan
        # throughput, even if the ticker sorts after many one-bar weaknesses.
        proposals.sort(key=lambda event:(event["kind"] != "CONFIRMED_FLIP", event["ticker"]))
        outgoing = []
        rejected = {"weakness_budget": 0, "flip_budget": 0, "scan_limit": 0}
        for event in proposals:
            kind = event["kind"]
            if budgets[kind] <= 0:
                rejected["flip_budget" if kind == "CONFIRMED_FLIP" else "weakness_budget"] += 1
                continue
            if len(outgoing) >= MAX_SCAN:
                rejected["scan_limit"] += 1
                continue
            budgets[kind] -= 1
            outgoing.append(event)
            entry = state[event["ticker"]]
            entry["warned"] = True
            if kind == "CONFIRMED_FLIP":
                entry["flipped"] = True
            state[event["ticker"]] = entry
        tx.put(prefix + "monitor:state", state)
        tx.put(prefix + "monitor:bucket:" + bucket,
               dict(at=now.isoformat(), tracked=len(state), warnings=len(outgoing),
                    weakness_sent=sum(e["kind"] == "WEAKNESS" for e in outgoing),
                    flips_sent=sum(e["kind"] == "CONFIRMED_FLIP" for e in outgoing),
                    suppressed=rejected, budgets_remaining=budgets))
        if outgoing:
            key = prefix + "monitor:msg:" + bucket
            tx.enqueue(key, dict(text=flip_text(now, cutoff, outgoing),
                                 expires_at=(now + timedelta(minutes=4)).isoformat()))
            notices.append(dict(at=now.isoformat(), cutoff=cutoff.isoformat(),
                                id=key, items=outgoing))
            tx.put(prefix + "monitor:notices", notices)
    delivery = drain(store, send, prefix=prefix + "monitor:msg:", now=now, limit=1) if outgoing else []
    return dict(status="monitored", tracked=len(state), warnings=len(outgoing),
                weakness=sum(e["kind"] == "WEAKNESS" for e in outgoing),
                confirmed_flip=sum(e["kind"] == "CONFIRMED_FLIP" for e in outgoing),
                suppressed=rejected, budgets_remaining=budgets,
                delivery_states=[x.get("status") for x in delivery])


def install(app, storage, send):
    def check(request):
        secret = os.getenv("CRON_SECRET", "")
        if not secret or not hmac.compare_digest(
                request.headers.get("authorization", ""), "Bearer " + secret):
            raise HTTPException(401, "Unauthorized")

    @app.get("/scanner/v21/confirm-worker")
    def recheck(request: Request):
        check(request)
        result = confirm_worker(storage(), send, utc())
        print("V21_PREMARKET_RECHECK " + json.dumps(result, sort_keys=True), flush=True)
        return result

    @app.get("/scanner/v21/monitor-worker")
    def monitor(request: Request):
        check(request)
        result = monitor_worker(storage(), send, utc())
        print("V21_FLIP_MONITOR " + json.dumps(result, sort_keys=True), flush=True)
        return result

    @app.get("/scanner/v21/status")
    def status(request: Request):
        check(request)
        day = utc().astimezone(DENVER).date().isoformat()
        prefix = "v21-direction:" + day + ":"
        with storage().transaction() as tx:
            return dict(day=day, confirmation=tx.get(prefix + "confirm:report"),
                        monitor_state=tx.get(prefix + "monitor:state", {}),
                        warnings=tx.get(prefix + "monitor:notices", []))
