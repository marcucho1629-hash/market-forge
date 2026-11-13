"""After-close Swing connection health check; independent of TradingView alarms.

Does not fabricate Swing PLAN/ENTRY/EXIT, relax trading filters, or modify
Pine/DAY/Swing signal logic. Sends actionable diagnostics only if the legacy
13-bank connection is missing or incomplete, or the separate EOD Swing WATCH
has not reported. Never sends secrets, bank payloads or private settings.
"""
import hmac
import json
import os
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
from fastapi import HTTPException,Request
from .calendar import schedule
from .free_iex import DENVER,utc
from .store import drain
from .swing_compare import BANKS,PREFIX as SWING_BANK_PREFIX
from .swing_scanner_shadow import PREFIX as SWING_SCAN_PREFIX

PREFIX="swing-health-v1:"
ET=ZoneInfo("America/New_York")
MAX_AGE_SECS=86400

def assess(store,now):
    day=now.astimezone(ET).date().isoformat()
    with store.transaction() as tx:
        existing={i:tx.get(SWING_BANK_PREFIX+"bank:"+str(i)) for i in BANKS}
        scanner=tx.get(SWING_SCAN_PREFIX+day+":report")
        scanner_error=tx.get(SWING_SCAN_PREFIX+day+":last_error",{})
        record=tx.execute("SELECT status FROM mf_outbox WHERE id=?",
                          (SWING_SCAN_PREFIX+day+":telegram",)).fetchone()
        rejected={i:tx.get("swing-diagnostic:reject:"+day+":"+str(i)) for i in BANKS}
    received={}
    for i,bank in existing.items():
        try:
            at=datetime.fromisoformat(bank["received_at"])
            recent=at.tzinfo is not None and at.astimezone(ET).date().isoformat()==day
        except (KeyError,TypeError,ValueError,AttributeError):
            recent=False
        if recent:
            received[i]={"at":bank["received_at"],"events":bank.get("events",0),
                         "valid_events":bank.get("valid_events",bank.get("events",0)),
                         "queued":bank.get("queued",0),"rejected_events":bank.get("rejected_events",0)}
    missing=sorted(set(BANKS)-set(received))
    total_events=sum(x["events"] for x in received.values())
    total_queued=sum(x["queued"] for x in received.values())
    return dict(day=day,bank_count=len(received),expected=len(BANKS),
                missing_banks=missing,received=received,
                event_count_latest=sum(x["events"] for x in received.values()),
                # Latest bank payloads are not a full-day history: never call
                # event_count_latest the number of events seen all day.
                queued_latest=total_queued,
                bank_envelope_rejections=sum(x.get("count",0) for x in rejected.values() if x),
                shadow_status="recorded" if scanner else "not_recorded",
                shadow_error_status=scanner_error.get("status"),
                shadow_error_causes=scanner_error.get("causes",{}),
                shadow_picks=len(scanner["picks"]) if scanner else None,
                shadow_delivery=record[0] if record else "not_queued",
                limitations=[
                    "Latest bank snapshots are not full-day cumulative event counts.",
                    "Zero bank receipts cannot distinguish an uncreated TradingView alert from disconnected internet or secret/422 rejected requests.",
                    "Telegram sent means API accepted, not user read confirmation.",
                    "Shadow WATCH is not TradingView Swing PLAN or execution."])

def message(report):
    day=report["day"]
    lines=[f"MARKET FORGE AI | SWING CONNECTION CHECK {day}",
           f'기존 TradingView Bank 수신 {report["bank_count"]}/{report["expected"]}']
    if report["missing_banks"]:
        names=", ".join(str(x) for x in report["missing_banks"])
        lines.append("⚠️ 미수신 Bank: "+names)
        lines.extend(["TradingView의 각 Bank마다 확인:",
                     "① Send SWING TEST alerts = ON",
                     "② Webhook key 입력 (채팅 공유 금지)",
                     "③ 알림 생성/활성화: Any alert() function call",
                     "④ URL: /webhook/swing-bank-v1"])
        lines.append("상세 원인 진단: /swing-compare")
    elif report["bank_envelope_rejections"] or report["event_count_latest"]==0:
        lines.append("13개 Bank 연결 관측. 개별 기술적 필터/거절 원인 확인 필요")
        lines.append(f'최근 상태: events {report["event_count_latest"]}, rejected envelopes {report["bank_envelope_rejections"]}')
    else:
        lines.append("13개 Bank 수신 확인 · 후보/전송 상태 추가 확인")
    status=report["shadow_status"]
    if status!="recorded" and report.get("shadow_error_status"):
        lines.append("Swing Shadow 오류: "+str(report["shadow_error_status"]))
        causes=report.get("shadow_error_causes",{})
        if causes:
            lines.append("데이터 실패 유형: "+", ".join(
                k+" "+str(v) for k,v in sorted(causes.items())[:4]))
    lines.extend(["",f'Server Swing Shadow: {status} · 후보 {report["shadow_picks"] if report["shadow_picks"] is not None else "UNKNOWN"}',
                  f'Shadow Telegram outbox: {report["shadow_delivery"]}',
                  "독립 Swing WATCH는 매매 진입 신호가 아닙니다.",
                  "기존 DAY/MC/SCMP/X/SX/HX 신호 변경 없음"])
    msg="\n".join(lines)
    if len(msg)>3900:raise ValueError("swing_health_message_limit")
    return msg

def scheduled(now):
    local=now.astimezone(DENVER)
    return local.weekday()<5 and local.hour==14 and 40<=local.minute<=54

def worker(store,send,now):
    if not scheduled(now):return {"status":"outside_1440_mountain"}
    day=now.astimezone(ET).date().isoformat()
    if not schedule(day).get("is_open"):return {"status":"exchange_closed"}
    key=PREFIX+day+":report"
    with store.transaction() as tx:
        current=tx.get(key)
    if current is None:
        try:report=assess(store,now)
        except Exception:return {"status":"health_database_unavailable"}
        with store.transaction() as tx:
            if tx.get(key) is None:
                tx.put(key,report)
                if report["missing_banks"] or report["shadow_status"]!="recorded":
                    tx.enqueue(PREFIX+day+":telegram",{
                        "text":message(report),
                        "expires_at":(now+timedelta(minutes=9)).isoformat()})
        current=report
    # The outbox is at-most-once and must not repeatedly resubmit a failed send.
    deliver=drain(store,send,prefix=PREFIX+day+":telegram",limit=1,now=now)
    return {"status":"assessed","day":day,"banks":current["bank_count"],
            "missing":current["missing_banks"],
            "shadow":current["shadow_status"],
            "delivery":[d["status"] for d in deliver]}

def install(app,storage,send):
    @app.get("/scanner/swing-health/worker")
    def cron(request:Request):
        secret=os.getenv("CRON_SECRET","")
        if not secret or not hmac.compare_digest(request.headers.get("authorization",""),"Bearer "+secret):
            raise HTTPException(401,"Unauthorized")
        result=worker(storage(),send,utc())
        print("SWING_CONNECTION_HEALTH "+json.dumps(result,sort_keys=True),flush=True)
        return result
