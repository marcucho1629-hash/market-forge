"""Independent intraday WATCH scanner. No trading, no DAY/Swing writes."""
import html,hmac,json,os,uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta
from math import log10
from urllib.parse import parse_qs
from fastapi import HTTPException,Request
from fastapi.responses import HTMLResponse
from .free_iex import DENVER,bounds,calendar,utc,auth
from .preopen.core import config as pre_config,validate
from .preopen.runtime import fetch
from .store import drain
from .intraday_early_watch import opening_worker,live_low_alert

PREFIX="intraday-v3:"
MAX_PER_SIDE=10
MIN_SCORE=55
MIN_NOTIONAL=500_000

def clamp(x):
    return max(0.,min(1.,x))

def ema(xs,n):
    a=2/(n+1);out=[]
    for x in xs:out.append(x if not out else out[-1]+a*(x-out[-1]))
    return out

def change(xs,n=6):
    return 100*(xs[-1]/xs[-1-n]-1) if len(xs)>n and xs[-1-n] else None

def structure_metrics(bars,true_ranges):
    """Bounded observed-bar chop: no synthetic bars and no future information.

    For the first 30–65min, use the observed 6–13 bar window. From bar 14,
    use a standard rolling 14-bar Choppiness Index. Thresholds are research
    heuristics pending multi-session validation.
    """
    n=min(14,len(bars))
    recent=bars[-n:]
    overall=max(r["h"] for r in recent)-min(r["l"] for r in recent)
    total_tr=sum(true_ranges[-n:])
    ratio=max(1.0,total_tr/overall) if overall>0 else float("inf")
    chop=100*log10(ratio)/log10(n) if n>=2 and ratio!=float("inf") else 100.
    closes=[r["c"] for r in recent]
    deltas=[next_-prev for prev,next_ in zip(closes,closes[1:])]
    path=sum(abs(d) for d in deltas)
    efficiency=abs(closes[-1]-closes[0])/path if path else 0.
    signs=[1 if d>0 else -1 if d<0 else 0 for d in deltas]
    signs=[s for s in signs if s]
    reversals=sum(a!=b for a,b in zip(signs,signs[1:]))
    choppy=(chop>=61.8 or (efficiency<.30 and reversals>=3))
    return dict(choppiness=round(chop,1),efficiency_ratio=round(efficiency,3),
                trend_reversals=reversals,structure_bars=n,choppy=choppy)

def evaluate(sym,bars,opened,end,spy_move=None):
    """Only validated completed native IEX 5m bars; absent intervals stay absent."""
    r=dict(ticker=sym,direction="NEUTRAL",quality="INSUFFICIENT",reason="no_bars",
           eligible=False,catalyst="UNKNOWN",options_liquidity="NOT_VERIFIED")
    try:b=validate(bars,5,opened,end)
    except (ValueError,TypeError,KeyError):return r|dict(quality="INVALID",reason="invalid_data")
    if len(b)<6:return r|dict(bars=len(b),reason="under_6_bars")
    ends=[datetime.fromisoformat(x["t"].replace("Z","+00:00"))+timedelta(minutes=5) for x in b]
    if (end-ends[-1]).total_seconds()>600:return r|dict(reason="stale")
    if any((y-x).total_seconds()!=300 for x,y in zip(ends[-3:-1],ends[-2:])):
        return r|dict(reason="recent_gap")
    notional=sum(x["vw"]*x["v"] for x in b)
    r.update(bars=len(b),iex_notional=round(notional),bar_end=ends[-1].isoformat())
    if notional<MIN_NOTIONAL:return r|dict(reason="iex_notional_low")
    # CHOP and efficiency require a continuous observation window. No gap
    # interpolation or synthetic 5m candles, even for a popular ticker.
    window=b[-min(14,len(b)):]
    times=[datetime.fromisoformat(x["t"].replace("Z","+00:00")) for x in window]
    if any((n-p).total_seconds()!=300 for p,n in zip(times,times[1:])):
        return r|dict(reason="structure_gap")
    closes=[x["c"] for x in b];e9=ema(closes,9);e21=ema(closes,21)
    previous=None;tr=[]
    for x in b:
        pc=x["o"] if previous is None else previous
        tr.append(max(x["h"]-x["l"],abs(x["h"]-pc),abs(x["l"]-pc)))
        previous=x["c"]
    atr=sum(tr[-14:])/min(14,len(tr))
    if atr<=0:return r|dict(quality="INVALID",reason="atr_zero")
    structure=structure_metrics(b,tr)
    r.update(structure)
    if structure["choppy"]:
        return r|dict(quality="READY",score=0,reason="choppy_chart")
    price=closes[-1];vwap=sum(x["vw"]*x["v"] for x in b)/sum(x["v"] for x in b)
    slope=e9[-1]-e9[-4]
    direction=1 if price>vwap and e9[-1]>e21[-1] and slope>0 else -1 if price<vwap and e9[-1]<e21[-1] and slope<0 else 0
    r.update(price=round(price,4),vwap=round(vwap,4),quality="READY",
             direction="BULLISH" if direction==1 else "BEARISH" if direction==-1 else "NEUTRAL")
    if not direction:return r|dict(score=0,reason="direction_conflict")
    d=direction;impulse=d*(price-closes[-4])/atr;egap=d*(price-e9[-1])/atr;vgap=d*(price-vwap)/atr
    # A strong trend is not automatically a safe *entry* after the move.
    # Require a new setup rather than chasing price far beyond the prior 20m
    # range, EMA9 or session VWAP. Symmetric for bearish short candidates.
    preceding=b[-5:-1]
    prior_edge=max(x["h"] for x in preceding) if d>0 else min(x["l"] for x in preceding)
    breakout_extension=max(0.,d*(price-prior_edge)/atr)
    session_extension=d*(price-b[0]["o"])/atr
    late_chase=(egap>1.4 and vgap>2.1
                or impulse>2.25 and egap>1.0
                or breakout_extension>1.25 and egap>1.1
                or session_extension>8.0 and egap>.8 and vgap>2.0)
    chase=("HIGH" if late_chase or impulse>3.0 or (egap>2.2 and vgap>4.5)
           else "MED" if egap>1.3 or vgap>3.0 or impulse>1.8
           else "LOW")
    vbase=sum(x["v"] for x in b[-6:-3]);vr=sum(x["v"] for x in b[-3:])/vbase if vbase else 0
    hi=max(x["h"] for x in b[-6:]);lo=min(x["l"] for x in b[-6:])
    pos=(price-lo)/(hi-lo) if hi>lo else .5
    stock_move=change(closes,min(6,len(closes)-1))
    rel=d*(stock_move-spy_move) if stock_move is not None and spy_move is not None else None
    points=dict(trend=round(10+8*clamp(d*(e9[-1]-e21[-1])/atr)+7*clamp(d*slope/(.8*atr)),1),
                vwap=round(8+12*clamp(vgap/1.5),1),
                momentum=round(15*clamp(impulse/1.5),1),
                relative_spy=round(15*clamp(rel/.8),1) if rel is not None else 0.,
                volume=round(10*clamp((vr-.8)/.8),1),
                location=round(10*clamp((pos-.5)*2*d),1),liquidity=5)
    score=round(sum(points.values()),1)
    eligible=score>=MIN_SCORE and chase!="HIGH"
    r.update(score=score,points=points,chase=chase,atr=round(atr,4),
             relative_spy_pct=round(rel,3) if rel is not None else None,
             volume_ratio=round(vr,2),extension_ema_atr=round(egap,2),
             extension_vwap_atr=round(vgap,2),
             breakout_extension_atr=round(breakout_extension,2),
             session_extension_atr=round(session_extension,2),
             late_chase=late_chase,eligible=eligible,
             reason=("qualified" if eligible else "late_chase" if late_chase
                     else "chase_block" if chase=="HIGH"
                     else "score_below_minimum"))
    return r

def select(rows):
    result=[]
    for side in ("BULLISH","BEARISH"):
        pool=[r for r in rows if r.get("eligible") and r["direction"]==side]
        result+=sorted(pool,key=lambda r:(-r["score"],r["chase"]!="LOW",r["ticker"]))[:MAX_PER_SIDE]
    names={r["ticker"] for r in result}
    for row in rows:row["selected"]=row["ticker"] in names
    return result

def compare(previous,selected,rows):
    old={x["ticker"]:x["direction"] for x in previous}
    new={x["ticker"]:x["direction"] for x in selected}
    full={x["ticker"]:x for x in rows}
    states={sym:("CONTINUE" if old.get(sym)==side else "FLIP" if sym in old else "NEW") for sym,side in new.items()}
    removed=[]
    for sym,side in old.items():
        if sym in new:continue
        candidate=full.get(sym,{})
        reason=candidate.get("reason","not_ranked")
        new_side=candidate.get("direction","NEUTRAL")
        state=("FLIP_WATCH" if new_side not in ("NEUTRAL",side) else
               "DATA_UNAVAILABLE" if candidate.get("quality") in ("INVALID","INSUFFICIENT") else
               "CHOPPY" if reason=="choppy_chart" else
               "CHASE" if reason in ("chase_block","late_chase") else
               "WEAKNESS" if reason in ("direction_conflict","score_below_minimum") else "REMOVE")
        removed.append(dict(ticker=sym,previous=side,status=state,reason=reason))
    return states,removed

def message(report):
    mt=datetime.fromisoformat(report["at"]).astimezone(DENVER)
    lines=[f"MARKET FORGE AI | INTRADAY V3 {mt:%H:%M} MT",
           "IEX 5분 완성봉 · WATCH ONLY (실제 진입 신호 아님)",
           f"데이터 READY {report['ready_count']}/150 · Catalyst UNKNOWN · 옵션 유동성 미검증",""]
    for direction,header in (("BULLISH","🟢 BULLISH TOP 10"),("BEARISH","🔴 BEARISH TOP 10")):
        picks=[p for p in report["selected"] if p["direction"]==direction]
        lines.append(header+f" · {len(picks)}/10")
        for p in picks:
            state=report["statuses"].get(p["ticker"],"NEW")
            lines.append(f'{p["ticker"]} {p["score"]:.1f} · {state} · USD {p["price"]:.2f} · CHASE {p["chase"]}')
        if not picks:lines.append("기준 통과 종목 없음")
        lines.append("")
    breakdown=report.get("quality_counts",{})
    lines.append("제외: CHOPPY "+str(breakdown.get("choppy_chart",0))+
                 " · LATE_CHASE "+str(breakdown.get("late_chase",0))+
                 " · 기타 추격 "+str(breakdown.get("chase_block",0)))
    lines.append("기준 통과 종목만 선정 · Top 10 강제 충원 없음")
    removed=report["removed"]
    lines.append(f"7:20/직전 시각 후보 제외·약화 {len(removed)}종목")
    for p in removed[:12]:lines.append(p["ticker"]+" "+p["status"])
    if len(removed)>12:lines.append(f"그 외 {len(removed)-12}종목: 관리화면")
    lines.extend(["","MC/SCMP 기존 엔진 분리 · Bigdata 유료조회 없음",
                  f'ID: intraday-v3:{report["day"]}:{mt:%H}'])
    text="\n".join(lines)
    if len(text)>3900:raise ValueError("telegram_message_too_long")
    return text

def gather(symbols,start,end):
    chunks=[symbols[i:i+50] for i in range(0,len(symbols),50)]
    with ThreadPoolExecutor(max_workers=3) as pool:
        results=list(pool.map(lambda grp:(grp,fetch(grp,"5Min",start,end)),chunks))
    return {s:bars.get(s,[]) for group,bars in results for s in group}

def worker(store,send,now):
    local=now.astimezone(DENVER)
    if local.weekday()>=5 or local.hour<7 or local.hour>13 or (local.hour==7 and local.minute<45):
        return dict(status="outside_intraday_window")
    today,_=calendar(now)
    if not today:return dict(status="market_closed")
    opened,closed=bounds(today)
    if local.hour==7:
        # First 15–25 closed minutes; isolated provisional WATCH, not V3 Top10
        # or MC/SCMP state. Normal intraday evaluate still needs six bars.
        if not opened+timedelta(minutes=15)<=now<opened+timedelta(minutes=30):
            return dict(status="early_open_waiting")
        end=min(now-timedelta(seconds=10),closed)
        end=end.replace(second=0,microsecond=0)-timedelta(minutes=end.minute%5)
        return opening_worker(store,send,now,opened,end,today["date"],
                              PREFIX+today["date"]+":",
                              pre_config()["symbols"],gather)
    if not opened+timedelta(minutes=30)<=now<=closed:
        return dict(status="outside_session")
    end=min(now-timedelta(seconds=10),closed)
    end=end.replace(second=0,microsecond=0)-timedelta(minutes=end.minute%5)
    if end<=opened:return dict(status="waiting_for_completed_bars")
    day=today["date"];prefix=PREFIX+day+":";bucket=int(end.timestamp())//300;token=str(uuid.uuid4())
    with store.transaction() as tx:
        if tx.get(prefix+"done:"+str(bucket)):return dict(status="already_scanned",asof=end.isoformat())
        prior=tx.get(prefix+"lease",{})
        if prior and (now-datetime.fromisoformat(prior["at"])).total_seconds()<120:return dict(status="busy")
        tx.put(prefix+"lease",dict(token=token,at=now.isoformat()))
    try:
        symbols=pre_config()["symbols"]
        all_bars=gather(symbols,opened,end)
        spy=validate(all_bars.get("SPY",[]),5,opened,end)
        spy_move=change([r["c"] for r in spy]) if len(spy)>=7 else None
        rows=[evaluate(sym,all_bars[sym],opened,end,spy_move) for sym in symbols]
        selected=select(rows)
        with store.transaction() as tx:
            last=tx.get(prefix+"hourly_last")
            if last is None:
                pre=tx.get("preopen-watch-v1:"+day+":report",{})
                seed=pre.get("prior_rth_context",{}).get("selected",[]) or pre.get("selected",[])
                previous=[dict(ticker=x["ticker"],direction=x["direction"]) for x in seed]
            else:previous=last["selected"]
            statuses,removed=compare(previous,selected,rows)
            snap=dict(day=day,at=now.isoformat(),bar_cutoff=end.isoformat(),
                      ready_count=sum(r["quality"]=="READY" for r in rows),
                      quality_counts=dict(Counter(r["reason"] for r in rows)),
                      selected=selected,rows=rows,statuses=statuses,removed=removed,
                      baseline="PREOPEN_0720" if last is None else "PRIOR_HOURLY",
                      spy_recent_change_pct=round(spy_move,3) if spy_move is not None else None,
                      feed="iex",mode="WATCH_ONLY",bigdata_calls=0,orders=0)
            tx.put(prefix+"latest",snap)
            # Immutable decision-time ledger for next-day 3m Shadow comparison.
            # Store only already-observed watch candidates, not all 150 rows.
            # Never retroactively evaluate a 3m event with a later watchlist.
            if day == "2026-11-11":
                tx.put(prefix+"timeline:"+str(bucket),{
                    "observed_at":now.isoformat(),"bar_cutoff":end.isoformat(),
                    "selected":[{k:p.get(k) for k in
                                  ("ticker","direction","score","chase","eligible","choppy","late_chase")}
                                 for p in selected],
                    "ready_count":snap["ready_count"],"feed":"IEX_5M"})
            tx.put(prefix+"done:"+str(bucket),dict(at=now.isoformat(),ready_count=snap["ready_count"]))
            hour_key=prefix+"hour:"+str(local.hour)
            should_send=tx.get(hour_key) is None
            if should_send:
                tx.put(hour_key,dict(at=now.isoformat(),selected_count=len(selected)))
                tx.put(prefix+"hourly_last",dict(at=now.isoformat(),selected=selected))
                history=tx.get(prefix+"history",[])
                history.append(dict(at=now.isoformat(),selected=selected,statuses=statuses,removed=removed))
                tx.put(prefix+"history",history[-8:])
                tx.enqueue(hour_key+":watch",dict(text=message(snap),expires_at=(now+timedelta(minutes=15)).isoformat()))
            tx.put(prefix+"lease",{})
        # EARLY WATCH is deliberately isolated in its own transaction.
        # If experimental alerts fail, hourly Top10 must still send normally.
        try:
            with store.transaction() as tx:
                early=live_low_alert(tx,prefix,selected,bucket,now,end,should_send)
        except Exception:
            early=dict(new=0,queued=False,error="early_alert_failure")
        delivery=drain(store,send,prefix=prefix+"hour:",now=now,limit=2) if should_send else []
        try:
            early_delivery=drain(store,send,prefix=prefix+"early:live:msg:",now=now,limit=2)
        except Exception:
            early_delivery=[]
        return dict(status="scanned",asof=end.isoformat(),ready=snap["ready_count"],
                    bullish=sum(p["direction"]=="BULLISH" for p in selected),
                    bearish=sum(p["direction"]=="BEARISH" for p in selected),
                    telegram_queued=should_send,delivery=delivery,bigdata_calls=0,
                    early_new=early.get("new",0),early_queued=early.get("queued",False),
                    early_delivery=early_delivery,early_error=early.get("error"))
    except Exception:
        with store.transaction() as tx:
            if tx.get(prefix+"lease",{}).get("token")==token:tx.put(prefix+"lease",{})
            tx.put(prefix+"last_error",dict(at=now.isoformat(),error="data_or_dependency_failure"))
        return dict(status="error",detail="data_or_dependency_failure")

def install(app,storage,send):
    @app.get("/intraday-v3",response_class=HTMLResponse)
    def panel():
        return '<meta charset="utf-8"><h2>INTRADAY V3 | Bullish 10 / Bearish 10</h2><p>IEX 5분 스캔 · 시간별 WATCH + 새 CHASE LOW 조기 WATCH (15분 오프닝 포함)</p><form method="post" action="/intraday-v3/control"><input type="password" name="secret" required placeholder="웹훅 키"><button name="action" value="status">오늘 결과 조회</button><button name="action" value="run">지금 스캔</button><button name="action" value="gate_status">MC/SCMP 필터 상태</button><button name="action" value="gate_off">필터 긴급 해제</button><button name="action" value="gate_on">필터 다시 활성화</button></form><p>매매 주문 없음. 기존 MC/SCMP/X/SX 유지.</p>'
    @app.post("/intraday-v3/control",response_class=HTMLResponse)
    async def control(request:Request):
        raw=await request.body()
        if len(raw)>2048:raise HTTPException(400,"Too large")
        fields=parse_qs(raw.decode());auth(fields.get("secret",[""])[0])
        action=fields.get("action",["status"])[0]
        if action=="run":result=worker(storage(),send,utc())
        elif action in ("gate_status","gate_on","gate_off"):
            from .top20_entry_gate import set_enabled, status as gate_status
            if action in ("gate_on","gate_off"):
                set_enabled(storage(),action=="gate_on",utc())
            result=gate_status(storage(),utc())
        elif action=="status":
            day=utc().astimezone(DENVER).date().isoformat()
            with storage().transaction() as tx:
                result=dict(latest=tx.get(PREFIX+day+":latest"),history=tx.get(PREFIX+day+":history",[]),
                            error=tx.get(PREFIX+day+":last_error"),
                            early_open=tx.get(PREFIX+day+":early:open:latest"),
                            early_history=tx.get(PREFIX+day+":early:live:history",[]),
                            early_open_error=tx.get(PREFIX+day+":early:open:error"),
                            deliveries=tx.execute("SELECT id,status FROM mf_outbox WHERE id LIKE ? ORDER BY id",(PREFIX+day+":hour:%",)).fetchall(),
                            early_deliveries=tx.execute("SELECT id,status FROM mf_outbox WHERE id LIKE ? ORDER BY id",(PREFIX+day+":early:%",)).fetchall())
        else:raise HTTPException(400,"Invalid action")
        return '<meta charset="utf-8"><h2>V3 현재 결과</h2><pre>'+html.escape(json.dumps(result,ensure_ascii=False,indent=2))+'</pre><a href="/intraday-v3">Back</a>'
    @app.get("/scanner/intraday-v3/worker")
    def cron(request:Request):
        secret=os.getenv("CRON_SECRET","")
        if not secret or not hmac.compare_digest(request.headers.get("authorization",""),"Bearer "+secret):
            raise HTTPException(401,"Unauthorized")
        result=worker(storage(),send,utc())
        # Sanitized operational receipt. No token, chat ID, prices or tickers.
        safe={k:result.get(k) for k in ("status","asof","ready","bullish","bearish",
                                        "telegram_queued","bigdata_calls",
                                        "early_new","early_queued")}
        safe["delivery_states"]=[x.get("status") for x in result.get("delivery",[])]
        early_outputs=(result.get("delivery",[]) if result.get("status")=="early_open_scanned"
                       else result.get("early_delivery",[]))
        safe["early_delivery_states"]=[x.get("status") for x in early_outputs]
        print("INTRADAY_V3_CRON "+json.dumps(safe,sort_keys=True),flush=True)
        return result
