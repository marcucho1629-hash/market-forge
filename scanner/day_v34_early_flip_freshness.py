"""Mon 2026-11-16 early flip vs chase/freshness, FORWARD-OBSERVED SHADOW.

All inputs are actual completed Alpaca IEX RTH 5m bars/snapshots observed
at original scanner time. No new intraday Telegram, order or existing MC,
SCMP, X, SX, HX changes. The EOD comparison is research, not executions.
"""
import hmac,html,json,os
from collections import Counter,defaultdict
from datetime import datetime,timedelta
from urllib.parse import parse_qs
from fastapi import HTTPException,Request
from fastapi.responses import HTMLResponse
from .calendar import schedule
from .core import ET
from .free_iex import DENVER,auth,utc
from .preopen.core import validate
from .store import drain

DAY="2026-11-16"
PREFIX="day-v34-early-flip-freshness-shadow:"
SOURCES={"OPEN_RAW","FIVE_MIN"}
MAX_SAVED_EXAMPLES=60
EXTENDED_SESSION_ATR=6.0
EARLY_MIN_MOVE=.45
EARLY_STRONG_BODY=.55
EARLY_RELATIVE_SPY=.30

def stamp(s):
    return datetime.fromisoformat(s.replace("Z","+00:00"))

def row_time(r):
    return stamp(r["t"])

def opening_rows(market,opened,end):
    """Keep ALL usable first-wave snapshots, including REJECTED radar candidates."""
    if end not in [opened+timedelta(minutes=n) for n in (5,10,15,20,25)]:
        raise ValueError("bad_opening_cutoff")
    raw={}
    for ticker,rows in market.items():
        try:
            b=validate(rows,5,opened,end)
            if len(b)!=int((end-opened).total_seconds()/300):continue
            if [row_time(q) for q in b]!=[opened+timedelta(minutes=5*n) for n in range(len(b))]:continue
            raw[ticker]=b
        except (KeyError,TypeError,ValueError,OverflowError):
            continue
    spy=raw.get("SPY")
    if not spy:return []
    spy_move=100*(spy[-1]["c"]/spy[0]["o"]-1)
    events=[]
    for ticker,b in raw.items():
        if ticker=="SPY":continue
        first=b[0];last=b[-1]
        if first["o"]<=0:continue
        move=100*(last["c"]/first["o"]-1)
        side="BULLISH" if move>0 else "BEARISH" if move<0 else "NEUTRAL"
        sign=1 if move>0 else -1 if move<0 else 0
        width=last["h"]-last["l"]
        body=abs(last["c"]-last["o"])/width if width>0 else 0
        loc=(last["c"]-last["l"])/width if width>0 else .5
        relative=sign*(move-spy_move)
        extended=abs(move)>2.5
        flip=False
        if len(b)==2:
            b0,b1=b
            first_dir=b0["c"]-b0["o"];second_dir=b1["c"]-b1["o"]
            first_size=abs(100*first_dir/b0["o"]) if b0["o"] else 0
            second_size=abs(100*second_dir/b1["o"]) if b1["o"] else 0
            flip=(first_dir*second_dir<0 and first_size>=.25
                  and second_size>=.45 and body>=.55
                  and (loc>=.70 if second_dir>0 else loc<=.30))
        strong=(abs(move)>=EARLY_MIN_MOVE and relative>=EARLY_RELATIVE_SPY
                and body>=EARLY_STRONG_BODY
                and (loc>=.75 if sign>0 else loc<=.25))
        state=("EARLY_FLIP_WATCH" if flip else
               "EXTENDED_NO_ENTRY" if extended else
               "EARLY_MOMENTUM_WATCH" if strong else "EARLY_UNCONFIRMED")
        events.append(dict(ticker=ticker,side=side,price=round(last["c"],4),
                      session_move_pct=round(move,3),relative_spy_pct=round(relative,3),
                      state=state,first_two_flip=flip,extended=extended,
                      count=len(b),latest_body=round(body,3),latest_close_location=round(loc,3)))
    return events

def five_rows(rows):
    """Read-only compact copy of scanner results; no change to selection."""
    out=[]
    for r in rows:
        if not isinstance(r,dict) or not isinstance(r.get("ticker"),str):continue
        s=r.get("score");m=(r.get("points") or {}).get("momentum")
        out.append(dict(ticker=r["ticker"],side=r.get("direction","NEUTRAL"),
            quality=r.get("quality"),selected=r.get("selected") is True,
            strong_low=(r.get("eligible") is True and r.get("chase")=="LOW"
                        and r.get("choppy") is not True and r.get("late_chase") is not True
                        and isinstance(s,(int,float)) and s>=70
                        and isinstance(m,(int,float)) and m>=8),
            score=s, momentum=m,chase=r.get("chase"),choppy=r.get("choppy") is True,
            session_extension_atr=r.get("session_extension_atr"),
            ema_extension_atr=r.get("extension_ema_atr"),
            price=r.get("price"),reason=r.get("reason")))
    return out

def capture(store,day,now,cutoff,source,rows,opened=None,hourly=False):
    if day!=DAY or source not in SOURCES:return {"status":"outside_study"}
    if now.tzinfo is None or cutoff.tzinfo is None or not cutoff<=now:
        raise ValueError("bad_capture_time")
    if (now.astimezone(ET).date().isoformat()!=DAY or
        cutoff.astimezone(ET).date().isoformat()!=DAY or
        now-cutoff>timedelta(minutes=12) or
        cutoff.minute%5 or cutoff.second or cutoff.microsecond):
        raise ValueError("wrong_date_or_unclosed_bar")
    if source=="OPEN_RAW":
        if opened is None:raise ValueError("missing_open")
        picks=opening_rows(rows,opened,cutoff)
    else:
        picks=five_rows(rows)
    key=PREFIX+day+":timeline:"+source+":"+str(int(cutoff.timestamp()))
    record=dict(at=now.isoformat(),asof=cutoff.isoformat(),source=source,
                rows=picks,hourly=hourly if source=="FIVE_MIN" else False,
                feed="IEX_5M",orders=0,live_alerts=0)
    with store.transaction() as tx:
        if tx.get(key) is not None:return {"status":"duplicate","observed":len(picks)}
        tx.put(key,record)
    return {"status":"captured","observed":len(picks)}

def timeline(store):
    with store.transaction() as tx:
        data=tx.execute("SELECT value FROM mf_kv WHERE key LIKE ?",
                        (PREFIX+DAY+":timeline:%",)).fetchall()
    valid=[]
    for item, in data:
        try:
            record=json.loads(item)
            at=stamp(record["at"]);asof=stamp(record["asof"])
            if (record.get("source") in SOURCES and record.get("feed")=="IEX_5M"
                 and at.astimezone(ET).date().isoformat()==DAY and
                 at>=asof and isinstance(record.get("rows"),list)):
                valid.append(record)
        except (KeyError,TypeError,ValueError,AttributeError):
            continue
    return sorted(valid,key=lambda r:stamp(r["at"]))

def summary(events,now):
    if not events:return dict(status="no_actual_observations")
    early=defaultdict(list);low_first={};hour_first={}
    all_five=[];qualified_count=Counter()
    for record in events:
        at=stamp(record["at"])
        for r in record["rows"]:
            ticker=r.get("ticker")
            if not ticker:continue
            if record["source"]=="OPEN_RAW":
                if r.get("state") in ("EARLY_FLIP_WATCH","EXTENDED_NO_ENTRY","EARLY_MOMENTUM_WATCH"):
                    early[ticker].append(dict(at=at.isoformat(),state=r["state"],
                                       side=r.get("side"),price=r.get("price"),
                                       move_pct=r.get("session_move_pct"),
                                       relative=r.get("relative_spy_pct")))
            else:
                all_five.append((at.isoformat(),r))
                side=r.get("side")
                if r.get("strong_low") and side in ("BULLISH","BEARISH"):
                    qualified_count[ticker]+=1
                    low_first.setdefault((ticker,side),dict(at=at.isoformat(),price=r.get("price")))
                if record.get("hourly") and r.get("selected") and side in ("BULLISH","BEARISH"):
                    hour_first.setdefault((ticker,side),dict(at=at.isoformat(),price=r.get("price"),
                        age_first_low_minutes=round((at-stamp(low_first[(ticker,side)]["at"])).total_seconds()/60,1)
                        if (ticker,side) in low_first else None,
                        session_extension_atr=r.get("session_extension_atr"),
                        chase=r.get("chase")))
    early_flip=[dict(ticker=t,**x) for t,arr in early.items() for x in arr if x["state"]=="EARLY_FLIP_WATCH"]
    extended=[dict(ticker=t,**x) for t,arr in early.items() for x in arr if x["state"]=="EXTENDED_NO_ENTRY"]
    preceding=[]
    for (ticker,side),h in hour_first.items():
        prior=[x for x in early.get(ticker,[]) if x["side"]==side and stamp(x["at"])<stamp(h["at"])]
        if prior:
            a=min(prior,key=lambda v:stamp(v["at"]))
            preceding.append(dict(ticker=ticker,side=side,early=a["at"],
                              first_hour=h["at"],
                              lead_min=round((stamp(h["at"])-stamp(a["at"])).total_seconds()/60,1),
                              early_state=a["state"],hour_chase=h["chase"]))
    stale_hours=[dict(ticker=t,side=s,**h) for (t,s),h in hour_first.items()
                 if (h["age_first_low_minutes"] is not None and h["age_first_low_minutes"]>15)
                 or (isinstance(h["session_extension_atr"],(int,float))
                     and h["session_extension_atr"]>=EXTENDED_SESSION_ATR)]
    # Forward observation, but no fill / no backdated first signal. Compare
    # only to an actually RECEIVED scanner state >= 15min after the first.
    continuation=[]
    by_ticker=defaultdict(list)
    for at,r in all_five:by_ticker[r["ticker"]].append((at,r))
    for (ticker,side),first in low_first.items():
        later=next(((at,r) for at,r in by_ticker[ticker]
                    if stamp(at)>=stamp(first["at"])+timedelta(minutes=15)
                    and r.get("price") is not None),None)
        if later and first["price"] and later[1]["price"]:
            sign=1 if side=="BULLISH" else -1
            continuation.append(dict(ticker=ticker,side=side,
                asof=first["at"],next_observed=later[0],
                next_signed_pct=round(100*sign*(later[1]["price"]/first["price"]-1),3)))
    direction_favorable=sum(x["next_signed_pct"]>0 for x in continuation)
    source_counts=Counter(r["source"] for r in events)
    result=dict(status="ready",day=DAY,generated_at=now.isoformat(),
        mode="AFTER_CLOSE_RESEARCH_ONLY",
        scans=source_counts,observed_symbols=len(by_ticker),
        early_symbols=len(early),early_flip_count=len(early_flip),
        early_extended_count=len(extended),first_strong_low_count=len(low_first),
        first_hourly_count=len(hour_first),early_before_hourly_count=len(preceding),
        late_hourly_count=len(stale_hours),
        forward_15m_favorable=direction_favorable,forward_15m_covered=len(continuation),
        early_flip_examples=early_flip[:MAX_SAVED_EXAMPLES],
        extended_examples=extended[:MAX_SAVED_EXAMPLES],
        earlier_than_hourly=preceding[:MAX_SAVED_EXAMPLES],
        late_hourly=stale_hours[:MAX_SAVED_EXAMPLES],
        continuation_examples=continuation[:MAX_SAVED_EXAMPLES],
        orders=0,new_intraday_alerts=0,bigdata_calls=0,
        caveats=["7:35/7:40 flags are as-of observations, NOT safe entry prices.",
                 "A strong first candle >2.5% is watched as EXTENDED_NO_ENTRY, never promoted to a trade.",
                 "First-hour NEW merely means not on prior hourly list; not a newly born trend.",
                 "FRESH timing only measures recorded 5m strong LOW first receipt, not actual trend initiation.",
                 "Direction change is not an automatic option trade, execution, or flip notification.",
                 "Forward 15m movement is measured at actually observed later 5m receipts, not at simulated order fills.",
                 "IEX differs from TradingView BATS; options spreads, liquidity, fills, catalysts unverified.",
                 "No change to DAY/MC/SCMP/X/SX/HX or risk gates."])
    return result

def text_report(r):
    return "\n".join([
        "MARKET FORGE AI | 11/16 EARLY FLIP + FRESHNESS SHADOW",
        "실제 관측 IEX 5분봉 연구 · 기존 DAY 매매/알림/청산 변경 없음",
        f'장중 RADAR/EARLY 관측 {r["scans"].get("OPEN_RAW",0)}회 · 8시 이후 {r["scans"].get("FIVE_MIN",0)}회',
        f'초반 FLIP WATCH {r["early_flip_count"]}건 · 과도하게 움직인 EXTENDED WATCH {r["early_extended_count"]}건',
        f'8시 이후 처음 강한 LOW 후보 {r["first_strong_low_count"]}개 · 시간별 첫 표시 {r["first_hourly_count"]}개',
        f'시간별 이전에 감지된 종목 {r["early_before_hourly_count"]}개 · 시간별 늦은 목록 {r["late_hourly_count"]}개',
        f'15분 후 다음 관측 방향 일치 {r["forward_15m_favorable"]}/{r["forward_15m_covered"]} (정확한 15분봉 아님)',
        "실제 주문/진입/청산 없음 · 옵션 유동성/체결비용 미검증",
        "이 결과는 Shadow 성능 관찰이며 매매 성공률이 아닙니다.",
        "상세: /day-v34-shadow (기존 웹훅 키 필요)"
    ])[:3900]

def window(now):
    local=now.astimezone(DENVER)
    return local.date().isoformat()==DAY and local.weekday()==0 and local.hour==14 and 52<=local.minute<=59

def worker(store,send,now):
    if not window(now):return dict(status="outside_monday_1452")
    market=schedule(DAY)
    if not market.get("is_open"):return dict(status="exchange_closed")
    if now<stamp(market["close"])+timedelta(minutes=35):
        return dict(status="waiting_for_post_close")
    key=PREFIX+DAY+":report"
    with store.transaction() as tx:prior=tx.get(key)
    if prior is None:
        events=timeline(store)
        if not events:return dict(status="no_live_observations_no_report")
        report=summary(events,now)
        if report["status"]!="ready":return dict(status="research_not_ready")
        with store.transaction() as tx:
            if tx.get(key) is None:
                tx.put(key,report)
                tx.enqueue(PREFIX+DAY+":telegram",dict(text=text_report(report),
                    expires_at=(now+timedelta(minutes=8)).isoformat()))
        prior=report;created=True
    else:created=False
    delivered=drain(store,send,prefix=PREFIX+DAY+":telegram",limit=1,now=now)
    return dict(status="recorded",created=created,
        opening=prior["scans"].get("OPEN_RAW",0),
        five=prior["scans"].get("FIVE_MIN",0),
        flip=prior["early_flip_count"],extended=prior["early_extended_count"],
        late=prior["late_hourly_count"],
        delivery=[r["status"] for r in delivered])

def install(app,storage,send):
    @app.get("/day-v34-shadow",response_class=HTMLResponse)
    def panel():
        return ('<meta charset="utf-8"><h2>DAY V3.4 11/16 Early Flip + Freshness SHADOW</h2>'
                '<p>장중 실제 5분 관측, 장 마감 후 연구 보고서 · 기존 신호 미변경</p>'
                '<form method="post" action="/day-v34-shadow/control">'
                '<input type="password" name="secret" required placeholder="기존 웹훅 키">'
                '<button name="action" value="status">저장 보고서</button></form>')
    @app.post("/day-v34-shadow/control",response_class=HTMLResponse)
    async def control(request:Request):
        raw=await request.body()
        if len(raw)>2048:raise HTTPException(400,"Too large")
        form=parse_qs(raw.decode());auth(form.get("secret",[""])[0])
        if form.get("action",["status"])[0]!="status":raise HTTPException(400,"Unsupported")
        with storage().transaction() as tx:
            report=tx.get(PREFIX+DAY+":report",{"status":"not_recorded","day":DAY})
        return '<meta charset="utf-8"><h2>DAY V3.4 SHADOW</h2><pre>'+html.escape(json.dumps(report,ensure_ascii=False,indent=2))+'</pre>'
    @app.get("/scanner/day-v34-shadow/worker")
    def cron(request:Request):
        secret=os.getenv("CRON_SECRET","")
        if not secret or not hmac.compare_digest(request.headers.get("authorization",""),"Bearer "+secret):
            raise HTTPException(401,"Unauthorized")
        result=worker(storage(),send,utc())
        print("DAY_V34_SHADOW "+json.dumps(result,sort_keys=True),flush=True)
        return result
