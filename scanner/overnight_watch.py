"""1:40 PM Mountain: research-only power-hour overnight watch, distinct from Swing and DAY.

Revalidates the most recent authenticated 5m Intraday V3 snapshot. Never
fabricates bars, queries Bigdata, sends orders, or creates MC/SCMP.
"""
import hmac
import json
import os
from datetime import datetime, timedelta
from fastapi import HTTPException, Request
from .free_iex import DENVER, bounds, calendar, utc
from .store import drain

PREFIX="overnight-watch-v1:"
SCANNER_PREFIX="intraday-v3:"
MIN_SCORE=65
MAX_PER_SIDE=10
MAX_REFRESH_AGE_SECONDS=600
MAX_BAR_AGE_SECONDS=900

def _stamp(value):
    return datetime.fromisoformat(value.replace("Z","+00:00"))

def is_window(now):
    local=now.astimezone(DENVER)
    return local.weekday()<5 and local.hour==13 and 40<=local.minute<=49

def candidates(snapshot,now):
    """Return no opportunities, not stale symbols, when the scanner is unhealthy."""
    if not isinstance(snapshot,dict) or snapshot.get("mode")!="WATCH_ONLY":
        return [],"MISSING_SCANNER_SNAPSHOT"
    if snapshot.get("day")!=now.astimezone(DENVER).date().isoformat():
        return [],"WRONG_SESSION"
    try:
        updated=_stamp(snapshot["at"])
        candle=_stamp(snapshot["bar_cutoff"])
        refresh_age=(now-updated).total_seconds()
        bar_age=(now-candle).total_seconds()
    except (ValueError,TypeError,KeyError,AttributeError):
        return [],"INVALID_SCANNER_TIMESTAMP"
    if not (0<=refresh_age<=MAX_REFRESH_AGE_SECONDS
            and 0<=bar_age<=MAX_BAR_AGE_SECONDS and candle<=updated):
        return [],"STALE_SCANNER_SNAPSHOT"
    if snapshot.get("ready_count",0)<1 or not isinstance(snapshot.get("selected"),list):
        return [],"INVALID_SCANNER_QUALITY"
    chosen=[]
    seen=set()
    for row in snapshot["selected"]:
        if not isinstance(row,dict):continue
        ticker=row.get("ticker")
        direction=row.get("direction")
        score=row.get("score")
        if (not isinstance(ticker,str) or ticker in seen or
                direction not in ("BULLISH","BEARISH") or
                type(score) not in (float,int) or not MIN_SCORE<=score<=100 or
                type(row.get("price")) not in (float,int) or row["price"]<=0 or
                row.get("eligible") is not True or
                row.get("chase") not in ("LOW","MED")):
            continue
        try:
            bar_end=_stamp(row["bar_end"])
            age=(now-bar_end).total_seconds()
        except (ValueError,TypeError,KeyError,AttributeError):
            continue
        if not 0<=age<=MAX_REFRESH_AGE_SECONDS:continue
        seen.add(ticker)
        chosen.append({key:row.get(key) for key in ("ticker","direction","score","price","chase","bar_end")})
    final=[]
    for direction in ("BULLISH","BEARISH"):
        ranked=sorted((r for r in chosen if r["direction"]==direction),
                      key=lambda x:(-x["score"],x["ticker"]))
        final.extend(ranked[:MAX_PER_SIDE])
    return final,"READY"

def changes(picks,previous):
    prior={r.get("ticker"):r.get("direction") for r in previous if isinstance(r,dict)}
    return {r["ticker"]:("CONTINUE" if prior.get(r["ticker"])==r["direction"]
                         else "FLIP" if r["ticker"] in prior else "NEW") for r in picks}

def message(record):
    asof=_stamp(record["at"]).astimezone(DENVER)
    lines=[f"MARKET FORGE AI | POWER HOUR OVERNIGHT SWING WATCH {asof:%H:%M} MT",
           "익일(1~2거래일) 후보 연구용 · 실제 진입 신호 아님",
           f"데이터: IEX 5분 완료봉 · 마지막 {record.get('bar_cutoff','확인 불가')}",
           "뉴스/실적/옵션 거래량: 미검증 · Bigdata 추가 호출 없음",""]
    if record["quality"]!="READY":
        lines += ["⚠ 데이터 검증 실패: "+record["quality"],
                  "신규 Overnight Swing 추천 보류. 오래된 후보를 재사용하지 않습니다."]
    else:
        for direction,header in (("BULLISH","🟢 OVERNIGHT BULLISH WATCH"),
                                 ("BEARISH","🔴 OVERNIGHT BEARISH WATCH")):
            rows=[r for r in record["picks"] if r["direction"]==direction]
            lines.append(f"{header} · {len(rows)}/10")
            if rows:
                for r in rows:
                    lines.append(f'{r["ticker"]} {r["score"]:.1f}/100 · {record["statuses"][r["ticker"]]} · USD {r["price"]:.2f} · CHASE {r["chase"]}')
            else:
                lines.append("기준 통과 후보 없음")
            lines.append("")
    lines += ["⚠ 밤사이 갭·뉴스·실적 위험. 다음 거래일 개장 확인 필요.",
              "MC·SCMP 및 기존 Swing 엔진과 별도의 WATCH 알림.",
              "ID: overnight-watch-v1:"+record["day"]]
    text="\n".join(lines)
    if len(text)>3900:raise ValueError("overnight_message_too_long")
    return text

def worker(store,send,now):
    if not is_window(now):return {"status":"outside_1340_mountain_window"}
    today,_=calendar(now)
    if not today:return {"status":"market_closed"}
    opened,closed=bounds(today)
    if not opened<now<closed:return {"status":"outside_regular_session"}
    day=today["date"]
    identity=PREFIX+day+":1340"
    with store.transaction() as tx:
        old=tx.get(identity)
        if old is None:
            snapshot=tx.get(SCANNER_PREFIX+day+":latest",{})
            picks,quality=candidates(snapshot,now)
            prior=tx.get(SCANNER_PREFIX+day+":hourly_last",{}).get("selected",[])
            report={"day":day,"at":now.isoformat(),"bar_cutoff":snapshot.get("bar_cutoff","UNKNOWN"),
                    "quality":quality,"picks":picks,"statuses":changes(picks,prior),
                    "feed":"IEX","mode":"OVERNIGHT_RESEARCH_WATCH_ONLY",
                    "positions":0,"orders":0,"bigdata_calls":0,
                    "option_liquidity_verified":False,"catalyst_verified":False}
            tx.put(identity,report)
            tx.enqueue(identity+":telegram",{"text":message(report),
                       "expires_at":(now+timedelta(minutes=12)).isoformat()})
            created=True
        else:
            report=old
            created=False
    # On retry, drain a still-pending same-day message, never re-enqueue one
    # that was sent / attempted. This is at-most-once by the shared outbox.
    delivery=drain(store,send,prefix=identity+":telegram",limit=1,now=now)
    return {"status":"sent_or_recorded","day":day,"created":created,
            "quality":report["quality"],
            "bullish":sum(x["direction"]=="BULLISH" for x in report["picks"]),
            "bearish":sum(x["direction"]=="BEARISH" for x in report["picks"]),
            "delivery":delivery,"bigdata_calls":0}

def install(app,storage,send):
    @app.get("/scanner/overnight-1340/worker")
    def cron(request:Request):
        secret=os.getenv("CRON_SECRET","")
        if not secret or not hmac.compare_digest(
                request.headers.get("authorization",""),"Bearer "+secret):
            raise HTTPException(401,"Unauthorized")
        result=worker(storage(),send,utc())
        safe={key:result.get(key) for key in
              ("status","day","created","quality","bullish","bearish","bigdata_calls")}
        safe["delivery_states"]=[x.get("status") for x in result.get("delivery",[])]
        print("OVERNIGHT_1340_CRON "+json.dumps(safe,sort_keys=True),flush=True)
        return safe
