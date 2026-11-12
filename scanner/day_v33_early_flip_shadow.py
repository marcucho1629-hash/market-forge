"""One-day 2026-11-13 research: early momentum, 5m weakness/flip and giveback.

Only records confirmed IEX 5m scanner observations as received; subsequent IEX
1m replay runs after close to test 3m proxy and hypothetical max favorable
excursion. Does not trade, issue intraday flip alerts, change DAY/Pine exits,
or claim historical availability of post-close data.
"""
import hmac,html,json,os
from collections import Counter
from datetime import datetime,timedelta
from urllib.parse import parse_qs
from fastapi import HTTPException,Request
from fastapi.responses import HTMLResponse
from .calendar import schedule
from .core import ET
from .free_iex import DENVER,auth,utc
from .preopen.runtime import fetch
from .preopen.core import validate
from .day_v32_shadow import aggregate_3m,try_confirm,stamp,eligible_5m
from .store import drain

DAY="2026-11-13"
PREFIX="day-v33-early-protection-shadow:"
SOURCES=("OPEN_RADAR","OPEN_EARLY","FIVE_MIN")
MAX_PER_FETCH=25
EARLY_WINDOW_MIN=45
WEAK_SCORE=55
WEAK_MOMENTUM=5
MAX_EVENTS=300

def capture(store,day,observed_at,cutoff,source,rows):
    """Capture immutable as-of observations without affecting scanner delivery."""
    if day!=DAY or source not in SOURCES:return {"status":"outside_study"}
    if not observed_at.tzinfo or not cutoff.tzinfo or cutoff>observed_at:
        raise ValueError("bad_observation_clock")
    # Never backdate a live observation, use an unclosed 5m bar or silently
    # rewrite the Nov12 freeze when callers pass the wrong local session.
    if (observed_at.astimezone(ET).date().isoformat()!=day
            or cutoff.astimezone(ET).date().isoformat()!=day
            or observed_at-cutoff>timedelta(minutes=10)
            or cutoff.second or cutoff.microsecond or cutoff.minute%5):
        raise ValueError("stale_or_wrong_session_cutoff")
    if source=="FIVE_MIN":
        picks=[]
        for r in rows:
            if not isinstance(r,dict) or not isinstance(r.get("ticker"),str):continue
            q=r.get("quality")
            score=r.get("score")
            points=r.get("points") or {}
            picks.append(dict(ticker=r["ticker"],
                direction=r.get("direction","NEUTRAL"),
                ready=q=="READY",reason=r.get("reason"),
                score=score if isinstance(score,(int,float)) else None,
                momentum=points.get("momentum"),
                chase=r.get("chase"),choppy=bool(r.get("choppy")),
                eligible_strong=eligible_5m(r),
                price=r.get("price"),vwap=r.get("vwap"),
                top10=bool(r.get("selected"))))
    else:
        picks=[]
        for r in rows:
            if not isinstance(r,dict) or not r.get("eligible") or not isinstance(r.get("ticker"),str):continue
            if r.get("direction") not in ("BULLISH","BEARISH"):continue
            # RADAR is preliminary, chase UNKNOWN; EARLY only confirmed LOW.
            if source=="OPEN_EARLY" and r.get("chase")!="LOW":continue
            picks.append(dict(ticker=r["ticker"],direction=r["direction"],
                              chase="UNKNOWN" if source=="OPEN_RADAR" else "LOW",
                              price=r.get("price"),score=r.get("score"),
                              eligible_strong=False))
    key=PREFIX+day+":timeline:"+source+":"+str(int(cutoff.timestamp()))
    record=dict(observed_at=observed_at.isoformat(),bar_cutoff=cutoff.isoformat(),
                source=source,universe_max=150,feed="IEX_5M",rows=picks,
                mode="SHADOW_RESEARCH_ONLY")
    with store.transaction() as tx:
        if tx.get(key) is not None:return {"status":"duplicate","count":len(picks)}
        tx.put(key,record)
    return {"status":"captured","count":len(picks)}

def timeline(store):
    with store.transaction() as tx:
        results=tx.execute("SELECT value FROM mf_kv WHERE key>=? AND key<? ORDER BY key",
                           (PREFIX+DAY+":timeline:",PREFIX+DAY+":timeline;")).fetchall()
    events=[]
    for raw, in results:
        try:
            item=json.loads(raw)
            observed=stamp(item["observed_at"]);cutoff=stamp(item["bar_cutoff"])
            if (observed.tzinfo is None or cutoff.tzinfo is None or
                cutoff>observed or observed.astimezone(ET).date().isoformat()!=DAY or
                item.get("source") not in SOURCES or item.get("feed")!="IEX_5M"
                or not isinstance(item.get("rows"),list)):continue
            events.append(item)
        except (KeyError,ValueError,TypeError,AttributeError):
            continue
    return sorted(events,key=lambda x:(x["observed_at"],x["source"]))

def first_appearances(entries):
    """Real received (not retroactive) 7:35/7:45 and full 5m first sightings."""
    first_early={}
    first_strong={}
    five=[]
    for item in entries:
        src=item["source"]
        for r in item["rows"]:
            ticker=r.get("ticker");side=r.get("direction")
            if not isinstance(ticker,str):continue
            if src in ("OPEN_RADAR","OPEN_EARLY") and side in ("BULLISH","BEARISH"):
                first_early.setdefault((src,ticker,side),{
                    "source":src,"ticker":ticker,"direction":side,
                    "observed_at":item["observed_at"],"price":r.get("price"),
                    "chase":r.get("chase")})
            if src=="FIVE_MIN":
                five.append((item["observed_at"],r))
                if r.get("eligible_strong") is True and side in ("BULLISH","BEARISH"):
                    first_strong.setdefault(ticker,{
                        "ticker":ticker,"direction":side,"observed_at":item["observed_at"],
                        "price":r.get("price"),"score":r.get("score"),
                        "top10":bool(r.get("top10"))})
    return first_early,first_strong,five

def transitions(first_strong,five):
    """Only as-of five-minute snapshots; no synthetic intrabar flip signals."""
    observed={}
    first_weak={}
    first_flip={}
    for timestamp,r in five:
        ticker=r.get("ticker")
        start=first_strong.get(ticker)
        if not start or timestamp<=start["observed_at"]:continue
        initial=start["direction"]
        if r.get("eligible_strong") is True and r.get("direction")!=initial:
            first_flip.setdefault(ticker,dict(ticker=ticker,observed_at=timestamp,
                                               from_side=initial,to_side=r.get("direction"),
                                               status="OPPOSITE_STRONG_5M"))
            # Do not label a new opposite-side trade as automatic execution.
        if ticker in first_weak:continue
        # Neutral, CHOPPY, losing volume/momentum, or VWAP crossing are WARN
        # hypotheses, not broker exit conditions. Require valid quality for
        # numeric checks; choppy status is still a legible safety warning.
        ready=r.get("ready") is True
        side=-1 if initial=="BEARISH" else 1
        price=r.get("price");vwap=r.get("vwap")
        crossing=(ready and isinstance(price,(int,float)) and
                  isinstance(vwap,(int,float)) and side*(price-vwap)<0)
        weak=(r.get("choppy") is True or
              (ready and (r.get("direction")!=initial or
                (isinstance(r.get("score"),(int,float)) and r["score"]<WEAK_SCORE) or
                (isinstance(r.get("momentum"),(int,float)) and r["momentum"]<WEAK_MOMENTUM) or
                crossing)))
        if weak:
            reason=("CHOPPY" if r.get("choppy") else
                    "DIRECTION_CHANGE" if r.get("direction")!=initial else
                    "VWAP_CROSS" if crossing else
                    "WEAK_MOMENTUM_OR_SCORE")
            first_weak[ticker]=dict(ticker=ticker,observed_at=timestamp,
                                    direction=initial,reason=reason)
    return first_weak,first_flip

def prices_after(observed_at,side,_prior_5m_price,rows):
    """Conservative next-minute OHLC reference after receipt; NEVER backfill 5m close.

    IEX 1m bars are fetched post-close, and even next-minute 'open' is not a
    verified fill; this is an illustrative hypothetical price path only.
    """
    result=dict(covered_minutes=0,reference_1m_open=None,reference_at=None,
                mfe_30_pct=None,mae_30_pct=None,
                signed_close_30_pct=None,giveback_30_pct=None)
    t=stamp(observed_at).replace(second=0,microsecond=0)+timedelta(minutes=1)
    within=[r for r in rows if t<=stamp(r["t"])<t+timedelta(minutes=30)]
    if not within or stamp(within[0]["t"])!=t:return result
    price=within[0]["o"]
    if not isinstance(price,(int,float)) or price<=0:return result
    result["reference_1m_open"]=round(price,4)
    result["reference_at"]=t.isoformat()
    sign=1 if side=="BULLISH" else -1
    if sign==1:
        best=max(r["h"] for r in within);worst=min(r["l"] for r in within)
    else:
        best=min(r["l"] for r in within);worst=max(r["h"] for r in within)
    result["mfe_30_pct"]=round(100*sign*(best/price-1),3)
    result["mae_30_pct"]=round(100*sign*(worst/price-1),3)
    result["covered_minutes"]=len(within)
    exact={stamp(r["t"])+timedelta(minutes=1):r["c"] for r in within}
    final=exact.get(t+timedelta(minutes=30))
    if final is not None and len(within)==30 and all(
            stamp(r["t"])==t+timedelta(minutes=i) for i,r in enumerate(within)):
        signed=100*sign*(final/price-1)
        result["signed_close_30_pct"]=round(signed,3)
        result["giveback_30_pct"]=round(max(0,result["mfe_30_pct"]-signed),3)
    return result

def load_native(symbols,opened,closed):
    result={}
    for n in range(0,len(symbols),MAX_PER_FETCH):
        chunk=symbols[n:n+MAX_PER_FETCH]
        bars=fetch(chunk,"1Min",opened,closed)
        result.update({s:bars.get(s,[]) for s in chunk})
    return result

def analyze(entries,data,opened,closed,now):
    if not entries:return {"status":"no_asof_observations"}
    early,strong,five=first_appearances(entries)
    weak,flip=transitions(strong,five)
    names=sorted(set(strong)|{p["ticker"] for p in early.values()})
    source_quality={}
    bars_by_symbol={};three_by_symbol={}
    for s in names:
        try:
            valid=validate(data.get(s,[]),1,opened,closed)
            source_quality[s]=dict(status="OK" if len(valid)>=15 else "SPARSE",
                                   count=len(valid))
            bars_by_symbol[s]=valid
            three_by_symbol[s]=aggregate_3m(valid,opened)
        except (ValueError,TypeError,KeyError,OverflowError):
            source_quality[s]=dict(status="INVALID",count=0)
            bars_by_symbol[s]=[];three_by_symbol[s]=[]
    events=[]
    for s,e in sorted(strong.items()):
        asof=e["observed_at"]
        proxy=try_confirm(e,three_by_symbol[s]) if source_quality[s]["status"]=="OK" else {
            "status":"SOURCE_INCOMPLETE","time":None,"price":None,"delay_min":None}
        event=dict(e,three_min=proxy,weakness=weak.get(s),flip=flip.get(s),
                   price_path_30=prices_after(asof,e["direction"],e["price"],bars_by_symbol[s]))
        events.append(event)
    early_leads=[]
    for e in early.values():
        later=strong.get(e["ticker"])
        if later and later["direction"]==e["direction"]:
            minutes=(stamp(later["observed_at"])-stamp(e["observed_at"])).total_seconds()/60
            if 0<=minutes<=EARLY_WINDOW_MIN:
                early_leads.append(dict(ticker=e["ticker"],source=e["source"],
                                        observed_at=e["observed_at"],later_5m_at=later["observed_at"],
                                        lead_min=round(minutes,2),
                                        first_chase=e["chase"]))
    giveback=[x["price_path_30"]["giveback_30_pct"] for x in events
              if x["price_path_30"]["giveback_30_pct"] is not None]
    confirmed=sum(x["three_min"]["status"]=="3M_PROXY_CONFIRMED" for x in events)
    return dict(status="ready",day=DAY,generated_at=now.isoformat(),
                mode="RESEARCH_ONLY_NO_INTRADAY_WARNING",
                source="5m as-of server receipts + post-close IEX native 1m replay",
                captured_scans=len(entries),
                first_5m_strong=len(strong),early_watches=len(early),
                early_same_direction_leads=early_leads[:MAX_EVENTS],
                early_lead_count=len(early_leads),
                first_weakness_count=len(weak),first_opposite_strong_5m_count=len(flip),
                weakness_precedes_flip=sum(s in weak and
                    stamp(weak[s]["observed_at"])<stamp(fl["observed_at"])
                    for s,fl in flip.items()),
                three_proxy_confirmed=confirmed,three_proxy_tested=len(events),
                giveback_30_covered=len(giveback),
                giveback_30_avg_pct=round(sum(giveback)/len(giveback),3) if giveback else None,
                source_quality=source_quality,results=events[:MAX_EVENTS],
                event_cap=MAX_EVENTS,orders=0,new_live_entries=0,new_live_exits=0,
                telegram_during_session=0,bigdata_calls=0,
                limitations=[
                    "Only first 5m STRONG LOW observation per ticker forms hypothetical watch, not real filled positions.",
                    "WEAKNESS and FLIP are first subsequent completed 5m research flags; not early intrabar exit alarms.",
                    "3m proxy is post-close and not original TradingView MC/SCMP Pine strategy.",
                    "Radar CHASE remains UNKNOWN; no inferred LOW or execution.",
                    "IEX 1m bars are not SIP/BATS, spreads/option liquidity/slippage/earnings not validated.",
                    "30m giveback uses NEXT minute IEX open after arrival as hypothetical reference, not old 5m close or a verified fill.",
                    "No changes to live DAY/Pine X/SX/HX protective exit logic."])

def telegram(report):
    return "\n".join([
        "MARKET FORGE | 11/13 EARLY + FLIP + GIVEBACK SHADOW",
        "장중 실제 5분 관측 + 마감 후 IEX 1분 사후 재생 · 신규 진입/청산 알림 없음",
        f'관측 스캔 {report["captured_scans"]}회 · 첫 강한 LOW 후보 {report["first_5m_strong"]}종목',
        f'RADAR/EARLY 선행 관측 {report["early_lead_count"]}건 (같은 방향, 45분 이내)',
        f'5분봉 WEAKNESS 첫 감지 {report["first_weakness_count"]}종목',
        f'5분봉 강한 반대방향 FLIP 첫 감지 {report["first_opposite_strong_5m_count"]}종목',
        f'FLIP 전에 WEAKNESS 선행 {report["weakness_precedes_flip"]}종목',
        f'이후 3분봉 Proxy 확인 {report["three_proxy_confirmed"]}/{report["three_proxy_tested"]}',
        f'30분 MFE 반납 관측 {report["giveback_30_covered"]}종목 · 평균 {report["giveback_30_avg_pct"] if report["giveback_30_avg_pct"] is not None else "N/A"}%p',
        "실제 매매 수익/손절 개선 검증 아님 · CHOPPY/CHASE LOW 운영 필터 유지",
        "기존 MC/SCMP/X/SX/HX 변경 없음 · 옵션/촉매/호가 미검증",
        "상세: /day-v33-shadow (기존 웹훅 키 필요)"
    ])[:3800]

def window(now):
    local=now.astimezone(DENVER)
    return local.date().isoformat()==DAY and local.hour==14 and 52<=local.minute<=59

def worker(store,send,now):
    if not window(now):return {"status":"outside_1452_friday"}
    market=schedule(DAY)
    if not market.get("is_open"):return {"status":"exchange_closed"}
    opened=stamp(market["open"]);closed=stamp(market["close"])
    if now<closed+timedelta(minutes=35):return {"status":"provider_wait"}
    identity=PREFIX+DAY+":report"
    with store.transaction() as tx:prior=tx.get(identity)
    if prior is None:
        observed=timeline(store)
        if not observed:return {"status":"no_observed_5m_timeline"}
        early,strong,_=first_appearances(observed)
        names=sorted(set(strong)|{e["ticker"] for e in early.values()})
        try:
            provider=load_native(names,opened,closed) if names else {}
            report=analyze(observed,provider,opened,closed,now)
        except Exception:
            return {"status":"provider_or_research_error"}
        if names and not any(x["status"]=="OK" for x in report["source_quality"].values()):
            return {"status":"no_valid_1m_coverage"}
        with store.transaction() as tx:
            if tx.get(identity) is None:
                tx.put(identity,report)
                tx.enqueue(PREFIX+DAY+":telegram",{
                    "text":telegram(report),"expires_at":(now+timedelta(minutes=8)).isoformat()})
        prior=report
        created=True
    else:created=False
    delivery=drain(store,send,prefix=PREFIX+DAY+":telegram",limit=1,now=now)
    return dict(status="recorded",created=created,observed=prior["first_5m_strong"],
                warnings=prior["first_weakness_count"],flips=prior["first_opposite_strong_5m_count"],
                delivery=[d["status"] for d in delivery])

def install(app,storage,send):
    @app.get("/day-v33-shadow",response_class=HTMLResponse)
    def panel():
        return ('<meta charset="utf-8"><h2>DAY V3.3 | 11/13 Early + Flip + Protection SHADOW</h2>'
                '<p>기존 운영 엔진 미변경. 5분 실시간 관측, 3분 사후 Proxy, MFE·반납 연구</p>'
                '<form method="post" action="/day-v33-shadow/control">'
                '<input type="password" name="secret" required placeholder="기존 웹훅 키">'
                '<button name="action" value="status">연구 결과</button></form>')
    @app.post("/day-v33-shadow/control",response_class=HTMLResponse)
    async def control(request:Request):
        raw=await request.body()
        if len(raw)>2048:raise HTTPException(400,"Too large")
        values=parse_qs(raw.decode());auth(values.get("secret",[""])[0])
        if values.get("action",["status"])[0]!="status":raise HTTPException(400,"Unsupported")
        with storage().transaction() as tx:
            report=tx.get(PREFIX+DAY+":report",{"status":"not_recorded","day":DAY})
        return '<meta charset="utf-8"><h2>DAY V3.3 SHADOW</h2><pre>'+html.escape(json.dumps(report,ensure_ascii=False,indent=2))+'</pre>'
    @app.get("/scanner/day-v33-shadow/worker")
    def cron(request:Request):
        secret=os.getenv("CRON_SECRET","")
        if not secret or not hmac.compare_digest(request.headers.get("authorization",""),"Bearer "+secret):
            raise HTTPException(401,"Unauthorized")
        result=worker(storage(),send,utc())
        print("DAY_V33_SHADOW "+json.dumps(result,sort_keys=True),flush=True)
        return result
