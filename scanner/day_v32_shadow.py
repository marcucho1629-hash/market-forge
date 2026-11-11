"""2026-11-12 forward-observed 150-symbol 5m -> completed 3m SHADOW study.

5m candidate decisions are journaled AT observed time by the production scanner.
After close, native IEX 1m candles retrospectively test a conservative *proxy*
3m confirmation AFTER the recorded 5m decision, with exact future bar ends.

This is NOT live 3m detection, not V13.36 MC/SCMP, not BATS, not an entry/
exit alert, not an order. The Pine engine, DAY/Swing signals, hourly Top10,
and Bigdata subscription remain untouched. No false historical messages.
"""
import hmac,html,json,math,os
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
from .store import drain

DAY="2026-11-12"
PREFIX="day-v32-150-3m-shadow:"
MIN_SCORE=70
MIN_MOMENTUM=8
MAX_CONFIRM_DELAY_MIN=15
MAX_BARS_PER_REQUEST=25
FORWARD_MINUTES=(5,15,30)
SOURCES=("OPEN_RADAR","OPEN_EARLY","5M_STRONG_ALL")

def stamp(value):
    return datetime.fromisoformat(value.replace("Z","+00:00"))

def eligible_5m(row):
    return (isinstance(row,dict) and row.get("eligible") is True
            and row.get("chase")=="LOW" and row.get("score",0)>=MIN_SCORE
            and row.get("points",{}).get("momentum",0)>=MIN_MOMENTUM
            and row.get("choppy") is not True
            and row.get("late_chase") is not True
            and row.get("direction") in ("BULLISH","BEARISH"))

def capture(store,day,observed_at,cutoff,source,rows):
    """Log as-of decisions only. Error isolation is done by caller.

    This record is a forward-observed decision, NOT a backdated signal.
    """
    if day!=DAY or source not in SOURCES:return {"status":"outside_study"}
    if not observed_at.tzinfo or not cutoff.tzinfo or cutoff>observed_at:
        raise ValueError("invalid_asof")
    if source=="5M_STRONG_ALL":
        chosen=[dict(ticker=r["ticker"],direction=r["direction"],
                     score=r["score"],momentum=r["points"]["momentum"],
                     chase=r["chase"],top10=bool(r.get("selected")))
                for r in rows if eligible_5m(r)]
    elif source=="OPEN_EARLY":
        chosen=[dict(ticker=r["ticker"],direction=r["direction"],
                     score=r.get("score"),momentum=None,chase=r.get("chase"),
                     top10=False)
                for r in rows if r.get("eligible") is True and r.get("chase")=="LOW"
                and r.get("score",0)>=MIN_SCORE and r.get("direction") in ("BULLISH","BEARISH")]
    else:
        chosen=[dict(ticker=r["ticker"],direction=r["direction"],
                     score=None,momentum=None,chase="UNKNOWN",top10=False)
                for r in rows if r.get("eligible") is True
                and r.get("direction") in ("BULLISH","BEARISH")]
    identity=PREFIX+day+":timeline:"+source+":"+str(int(cutoff.timestamp()))
    entry={"observed_at":observed_at.isoformat(),"bar_cutoff":cutoff.isoformat(),
           "source":source,"selected":chosen,"feed":"IEX_5M",
           "universe_size":150,"live_3m":False}
    with store.transaction() as tx:
        if tx.get(identity) is not None:return {"status":"duplicate","count":len(chosen)}
        tx.put(identity,entry)
    return {"status":"captured","count":len(chosen)}

def read_timeline(store):
    with store.transaction() as tx:
        rows=tx.execute("SELECT value FROM mf_kv WHERE key LIKE ? ORDER BY key",
                        (PREFIX+DAY+":timeline:%",)).fetchall()
    timeline=[]
    for raw, in rows:
        try:
            s=json.loads(raw)
            obs=stamp(s["observed_at"]);cutoff=stamp(s["bar_cutoff"])
            if (obs.tzinfo is None or cutoff.tzinfo is None or
                obs<cutoff or obs.astimezone(ET).date().isoformat()!=DAY or
                s.get("feed")!="IEX_5M" or s.get("source") not in SOURCES):
                continue
            if type(s.get("selected")) is not list:continue
            timeline.append(s)
        except (ValueError,TypeError,KeyError,AttributeError):
            continue
    timeline.sort(key=lambda x:stamp(x["observed_at"]))
    return timeline

def first_seen(timeline):
    """Use first *observed* candidate per source, ticker and direction."""
    seen=set();out=[]
    for record in timeline:
        obs=stamp(record["observed_at"]);cutoff=stamp(record["bar_cutoff"])
        for pick in record["selected"]:
            if not isinstance(pick,dict):continue
            ticker=pick.get("ticker");side=pick.get("direction")
            if not isinstance(ticker,str) or side not in ("BULLISH","BEARISH"):continue
            key=(record["source"],ticker,side)
            if key in seen:continue
            seen.add(key)
            out.append(dict(ticker=ticker,direction=side,source=record["source"],
                 observed_at=obs.isoformat(),bar_cutoff=cutoff.isoformat(),
                 score=pick.get("score"),momentum=pick.get("momentum"),
                 chase=pick.get("chase"),top10=pick.get("top10") is True))
    return out

def closed_minute_bars(ticker,raw,opened,closed):
    # Strict source validator rejects duplicates/conflicts, future bars and
    # incomplete regular-session candles instead of quietly interpolating.
    rows=validate(raw,1,opened,closed)
    return rows

def aggregate_3m(minute_bars,opened):
    buckets={}
    for b in minute_bars:
        t=stamp(b["t"])
        index=int((t-opened).total_seconds()/180)
        if index<0:continue
        buckets.setdefault(index,[]).append(b)
    three=[]
    for index in sorted(buckets):
        rows=sorted(buckets[index],key=lambda r:r["t"])
        expected=[opened+timedelta(minutes=3*index+j) for j in range(3)]
        if [stamp(x["t"]) for x in rows]!=expected:continue
        count=sum(x["v"] for x in rows)
        three.append(dict(end=(opened+timedelta(minutes=3*(index+1))),
              o=rows[0]["o"],h=max(x["h"] for x in rows),
              l=min(x["l"] for x in rows),c=rows[-1]["c"],v=count))
    return three

def ema(prices,n):
    a=2/(n+1);values=[]
    for x in prices:values.append(x if not values else values[-1]+a*(x-values[-1]))
    return values

def try_confirm(event,three):
    asof=stamp(event["observed_at"])
    sign=1 if event["direction"]=="BULLISH" else -1
    has_future=False
    for idx,b in enumerate(three):
        if b["end"]<=asof or b["end"]>asof+timedelta(minutes=MAX_CONFIRM_DELAY_MIN):
            continue
        has_future=True
        if idx<4:continue
        history=three[:idx+1]
        # Require at least 5 consecutive native complete 3m candles so the
        # EMA, VWAP and volume baseline cannot be built across sparse gaps.
        last5=history[-5:]
        if any((n["end"]-p["end"]).total_seconds()!=180 for p,n in zip(last5,last5[1:])):
            continue
        e=ema([x["c"] for x in history],9)
        side_body=sign*(b["c"]-b["o"])
        width=b["h"]-b["l"]
        if width<=0 or side_body<.45*width:continue
        loc=(b["c"]-b["l"])/width
        if (sign>0 and loc<.72) or (sign<0 and loc>.28):continue
        avg_volume=sum(x["v"] for x in history[idx-4:idx])/4
        if avg_volume<=0 or b["v"]<1.10*avg_volume:continue
        vwap=sum(x["c"]*x["v"] for x in history)/sum(x["v"] for x in history)
        if sign*(b["c"]-vwap)<=0 or sign*(b["c"]-e[-1])<0:
            continue
        if sign*(e[-1]-e[-2])<=0:continue
        ranges=[max(x["h"]-x["l"],1e-8) for x in last5]
        atr=sum(ranges)/5
        if abs(b["c"]-e[-1])>1.5*atr:continue
        return dict(status="3M_PROXY_CONFIRMED",time=b["end"].isoformat(),
                    price=b["c"],delay_min=round((b["end"]-asof).total_seconds()/60,2))
    return dict(status="3M_NOT_CONFIRMED" if has_future else "NO_COMPLETE_3M_AFTER_OBSERVATION",
                time=None,price=None,delay_min=None)

def future_changes(confirmation,minute_bars,side):
    if confirmation["time"] is None:
        return {str(m):None for m in FORWARD_MINUTES}
    closes={stamp(x["t"])+timedelta(minutes=1):x["c"] for x in minute_bars}
    start=stamp(confirmation["time"]);price=confirmation["price"]
    sign=1 if side=="BULLISH" else -1
    return {str(minutes):(round(100*sign*(closes[start+timedelta(minutes=minutes)]/price-1),3)
             if start+timedelta(minutes=minutes) in closes else None)
            for minutes in FORWARD_MINUTES}

def load_1m(symbols,opened,closed):
    result={}
    for i in range(0,len(symbols),MAX_BARS_PER_REQUEST):
        group=symbols[i:i+MAX_BARS_PER_REQUEST]
        chunk=fetch(group,"1Min",opened,closed)
        result.update({symbol:chunk.get(symbol,[]) for symbol in group})
    return result

def research(timeline,data,opened,closed,now):
    candidates=first_seen(timeline)
    if not timeline:return {"status":"no_5m_observed_records"}
    if not candidates:return {"status":"no_eligible_5m_candidates","day":DAY,"timeline_count":len(timeline)}
    observed_symbols=sorted({x["ticker"] for x in candidates})
    minute_map={};three_map={};quality={}
    for symbol in observed_symbols:
        try:
            one=closed_minute_bars(symbol,data.get(symbol,[]),opened,closed)
            three=aggregate_3m(one,opened)
            minute_map[symbol]=one;three_map[symbol]=three
            quality[symbol]={"status":"OK" if len(one)>=15 else "SPARSE_IEX_1M",
                             "one_minute_bars":len(one),"complete_3m_bars":len(three)}
        except (ValueError,KeyError,TypeError,OverflowError):
            quality[symbol]={"status":"INVALID_IEX_1M","one_minute_bars":0,"complete_3m_bars":0}
            minute_map[symbol]=[];three_map[symbol]=[]
    out=[]
    for c in candidates:
        symbol=c["ticker"]
        check=try_confirm(c,three_map[symbol]) if quality[symbol]["status"]=="OK" else {
            "status":quality[symbol]["status"],"time":None,"price":None,"delay_min":None}
        returns=future_changes(check,minute_map[symbol],c["direction"])
        out.append(dict(c,three_min=check,forward_signed_pct=returns))
    counts=dict(Counter(r["three_min"]["status"] for r in out))
    per_source={source:{"observed":sum(r["source"]==source for r in out),
                         "confirmed":sum(r["source"]==source and
                                         r["three_min"]["status"]=="3M_PROXY_CONFIRMED" for r in out)}
                for source in SOURCES}
    signed={}
    for minutes in FORWARD_MINUTES:
        key=str(minutes)
        eligible=[r["forward_signed_pct"][key] for r in out if
                  r["three_min"]["status"]=="3M_PROXY_CONFIRMED" and
                  r["forward_signed_pct"][key] is not None]
        signed[key]={"positive":sum(x>0 for x in eligible),"covered":len(eligible)}
    return {"status":"ready","day":DAY,"generated_at":now.isoformat(),
            "mode":"RESEARCH_SHADOW_NOT_LIVE_3M","source":"Alpaca IEX native 1m post-close",
            "universe_5m":150,"unique_1m_replay_symbols":len(observed_symbols),
            "timeline_count":len(timeline),"observed_candidates":len(candidates),
            "counts":counts,"per_source":per_source,"forward_directional":signed,
            "quality":quality,"events":out[:300],"event_cap":300,
            "orders":0,"live_mc_scmp":False,"live_3m_alerts":False,"bigdata_calls":0,
            "limitations":["5m decisions observed live and persisted before post-close; 3m confirmation is hindsight proxy replay, not live.",
                           "The 3m candle/volume/EMA proxy is NOT the 20-symbol V13.36 MC/SCMP engine. Do not compare their scores as equivalent.",
                           "3m evaluation uses only completed 1m bars; first future 3m bar must finish AFTER the 5m decision was observed.",
                           "Opening RADAR has chase UNKNOWN; it is not counted as 5m CHASE LOW.",
                           "IEX is sparse and not consolidated SIP; missing 1m candles are never synthesized.",
                           "Forward 5/15/30m signed price moves are not fillable profits or win rates.",
                           "Options spread, OI, catalysts, slippage and market impact remain unchecked."]}

def telegram(report):
    count=report["counts"].get("3M_PROXY_CONFIRMED",0)
    p=report["per_source"];f=report["forward_directional"]
    lines=["MARKET FORGE AI | 11/12 150x5M -> 3M SHADOW",
           "장중 관측 5분 Scanner + 장마감 IEX 1분봉 3분 Proxy 재생",
           "연구용 WATCH · 신규 MC/SCMP 알림/주문 없음",
           f'5분 이력 {report["timeline_count"]}개 · 최초 후보 {report["observed_candidates"]}개 · 1분봉 대상 {report["unique_1m_replay_symbols"]}종목',
           f'3분봉 사후 확인 {count}개',
           f'7:35 RADAR {p["OPEN_RADAR"]["confirmed"]}/{p["OPEN_RADAR"]["observed"]} · 7:45 EARLY {p["OPEN_EARLY"]["confirmed"]}/{p["OPEN_EARLY"]["observed"]}',
           f'8시 이후 CHASE LOW 전체 {p["5M_STRONG_ALL"]["confirmed"]}/{p["5M_STRONG_ALL"]["observed"]}',
           "미확인: "+", ".join(k+":"+str(v) for k,v in report["counts"].items() if k!="3M_PROXY_CONFIRMED"),
           f'확인 후 5분 방향 일치 {f["5"]["positive"]}/{f["5"]["covered"]} · 15분 {f["15"]["positive"]}/{f["15"]["covered"]} · 30분 {f["30"]["positive"]}/{f["30"]["covered"]}',
           "3m Proxy ≠ 기존 20종목 MC 엔진; 실제 장중 3m 실시간 알림 아님",
           "옵션 유동성/Bigdata 촉매/체결비용 미검증",
           "상세: /day-v32-shadow (기존 웹훅 키 필요)"]
    result="\n".join(lines)
    if len(result)>3900:raise ValueError("telegram_too_long")
    return result

def window(now):
    local=now.astimezone(DENVER)
    return local.date().isoformat()==DAY and local.hour==14 and 33<=local.minute<=44

def worker(store,send,now):
    if not window(now):return {"status":"outside_one_day_research_window"}
    market=schedule(DAY)
    if not market.get("is_open"):return {"status":"exchange_closed"}
    opened=stamp(market["open"]);closed=stamp(market["close"])
    if now<closed+timedelta(minutes=25):
        return {"status":"waiting_for_post_close_provider"}
    key=PREFIX+DAY+":report"
    with store.transaction() as tx:prior=tx.get(key)
    if prior is None:
        timeline=read_timeline(store)
        if not timeline:return {"status":"no_live_5m_observation_no_report"}
        symbols=sorted({r["ticker"] for r in first_seen(timeline)})
        if not symbols:return {"status":"no_eligible_candidates_no_report","timeline":len(timeline)}
        try:
            data=load_1m(symbols,opened,closed)
            report=research(timeline,data,opened,closed,now)
        except Exception:
            return {"status":"data_provider_or_evaluation_error"}
        covered=sum(x["one_minute_bars"]>=15 for x in report["quality"].values())
        if covered==0:return {"status":"no_valid_1m_data_no_report"}
        report["symbols_with_15_1m_bars"]=covered
        with store.transaction() as tx:
            if tx.get(key) is None:
                tx.put(key,report)
                tx.enqueue(PREFIX+DAY+":telegram",{
                    "text":telegram(report),
                    "expires_at":(now+timedelta(minutes=12)).isoformat()})
        created=True
    else:
        report=prior;created=False
    deliveries=drain(store,send,prefix=PREFIX+DAY+":telegram",limit=1,now=now)
    return {"status":"recorded","created":created,"day":DAY,
            "observed":report["observed_candidates"],
            "confirmed":report["counts"].get("3M_PROXY_CONFIRMED",0),
            "delivery":[d["status"] for d in deliveries],
            "bigdata_calls":0}

def install(app,storage,send):
    @app.get("/day-v32-shadow",response_class=HTMLResponse)
    def panel():
        return '<meta charset="utf-8"><h2>DAY V3.2 150x5M → 3M Proxy | 11/12 Shadow</h2><p>관측 5분봉 + 장마감 3분봉 대조 · TradingView / MC 신호 아님</p><form method="post" action="/day-v32-shadow/control"><input type="password" name="secret" required placeholder="기존 웹훅 키"><button name="action" value="status">저장 보고서</button></form>'
    @app.post("/day-v32-shadow/control",response_class=HTMLResponse)
    async def control(request:Request):
        raw=await request.body()
        if len(raw)>2048:raise HTTPException(400,"Too large")
        qs=parse_qs(raw.decode());auth(qs.get("secret",[""])[0])
        if qs.get("action",["status"])[0]!="status":raise HTTPException(400,"Unsupported")
        with storage().transaction() as tx:
            out=tx.get(PREFIX+DAY+":report",{"status":"not_recorded","day":DAY})
        return '<meta charset="utf-8"><h2>DAY V3.2 사후 비교</h2><pre>'+html.escape(json.dumps(out,ensure_ascii=False,indent=2))+'</pre>'
    @app.get("/scanner/day-v32-shadow/worker")
    def cron(request:Request):
        secret=os.getenv("CRON_SECRET","")
        if not secret or not hmac.compare_digest(request.headers.get("authorization",""),"Bearer "+secret):
            raise HTTPException(401,"Unauthorized")
        result=worker(storage(),send,utc())
        print("DAY_V32_SHADOW "+json.dumps(result,sort_keys=True),flush=True)
        return result
