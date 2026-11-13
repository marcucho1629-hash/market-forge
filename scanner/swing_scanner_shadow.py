"""Independent swing discovery WATCH from native Alpaca IEX Day + RTH Hour bars.

This is research-only and cannot create Swing PLAN/ENTRY, orders or DAY alerts.
It shares the existing 100-symbol Swing bank universe, but no chart bank alert
is required to collect its own market data or publish a WATCH summary.
"""
import hmac,html,json,math,os
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone,time
from urllib.parse import parse_qs
from fastapi import HTTPException,Request
from fastapi.responses import HTMLResponse
from .calendar import schedule
from .core import ET
from .free_iex import DENVER,auth,utc,DataError
from .preopen.runtime import fetch
from .store import drain
from .swing_compare import BANKS

PREFIX="swing-scan-shadow-v1:"
UNIVERSE=sorted({s for bank in BANKS.values() for s in bank})
HORIZONS=("2-3","5-10")
MAX_PER_SIDE=5
MIN_BARS=60
MIN_NOTIONAL=600000
LIMITS={"2-3":65,"5-10":70}

# Expose only fixed, non-sensitive provider failure categories, never secrets
# or full API responses in health messages / telemetry.
SAFE_ERRORS={"alpaca_connection_or_payload_error","alpaca_credentials_missing",
             "pagination_incomplete","pagination_cycle","bars_invalid",
             "unexpected_bars","unexpected_symbol"}

def safe_error(exc):
    if isinstance(exc,DataError):
        s=str(exc)
        if s in SAFE_ERRORS:return s
        if s.startswith("alpaca_http_") and s[12:].isdigit() and len(s[12:])==3:
            return s
        return "alpaca_data_error"
    return "unexpected_"+type(exc).__name__ if type(exc).__name__ in {
        "ValueError","TypeError","KeyError","TimeoutError"} else "unexpected_error"

class ProviderBatchFailure(Exception):
    def __init__(self,errors):
        self.count=len(errors)
        self.causes=dict(Counter(errors))
        super().__init__("sanitized_swing_provider_batch_failure")


def dt(value):
    return datetime.fromisoformat(value.replace("Z","+00:00"))

def ema(xs,n):
    a=2/(n+1);out=[]
    for x in xs:out.append(x if not out else out[-1]+a*(x-out[-1]))
    return out

def rsi(xs,n=14):
    changes=[x-y for x,y in zip(xs[1:],xs[:-1])]
    if len(changes)<n:return None
    up=sum(max(0,v) for v in changes[:n])/n
    down=sum(max(0,-v) for v in changes[:n])/n
    for x in changes[n:]:
        up=(up*(n-1)+max(0,x))/n
        down=(down*(n-1)+max(0,-x))/n
    return 100 if not down else 100-100/(1+up/down)

def valid(r):
    try:
        o,h,l,c,v=[float(r[k]) for k in ("o","h","l","c","v")]
        return all(math.isfinite(q) for q in (o,h,l,c,v)) and 0<l<=o<=h and l<=c<=h and v>=0
    except (KeyError,TypeError,ValueError):return False

def clean(rows,day,kind,closed):
    result={}
    for bar in rows:
        if not valid(bar):continue
        try:t=dt(bar["t"]).astimezone(ET)
        except (KeyError,ValueError,TypeError):continue
        d=t.date().isoformat()
        if d>day:continue
        if kind=="hour" and not (time(9,30)<=t.time().replace(tzinfo=None)<time(16,0)
                                and t.astimezone(timezone.utc)<closed):
            continue
        key=d if kind=="day" else bar["t"]
        if key in result:return []
        result[key]=dict(bar,_day=d)
    return [result[k] for k in sorted(result)]

def atr14(bars):
    if len(bars)<16:return None
    prev=None;trs=[]
    for b in bars:
        prev=prev if prev is not None else b["o"]
        trs.append(max(b["h"]-b["l"],abs(b["h"]-prev),abs(b["l"]-prev)))
        prev=b["c"]
    return sum(trs[-14:])/14

def study(symbol,d,h,spy,day,closed,horizon,side):
    sign=1 if side=="BULLISH" else -1
    row=dict(ticker=symbol,horizon=horizon,direction=side,eligible=False,
             reason="unavailable",score=None,catalyst="UNKNOWN",options_liquidity="NOT_VERIFIED")
    if len(d)<MIN_BARS or d[-1]["_day"]!=day:
        return row|dict(reason="daily_missing_or_stale",daily_bars=len(d))
    if len(spy)<MIN_BARS or spy[-1]["_day"]!=day:return row|dict(reason="spy_missing")
    same_day=[x for x in h if x["_day"]==day]
    if len(same_day)<4 or len(h)<9 or dt(h[-1]["t"])<closed-timedelta(hours=2):
        return row|dict(reason="hourly_missing_or_stale",hourly_count=len(same_day))
    cs=[x["c"] for x in d];spycs=[x["c"] for x in spy];hs=[x["c"] for x in h]
    atr=atr14(d)
    if atr is None or atr<=0:return row|dict(reason="atr_missing")
    price=cs[-1]
    if price<5:return row|dict(reason="price_under_5")
    notional=sum(x["c"]*x["v"] for x in d[-15:])/15
    if notional<MIN_NOTIONAL:
        return row|dict(reason="iex_notional_low",iex_avg_notional=round(notional))
    e10=ema(cs,10);e20=ema(cs,20);e50=ema(cs,50)
    slope=sign*(e20[-1]-e20[-6])>0
    trend=(sign*(price-e20[-1])>0 and slope and
           (sign*(e10[-1]-e20[-1])>0 if horizon=="2-3" else sign*(e20[-1]-e50[-1])>0))
    if not trend:return row|dict(reason="daily_trend_conflict")
    moves=[b-a for a,b in zip(cs[-10:-1],cs[-9:])]
    path=sum(abs(x) for x in moves)
    efficiency=abs(cs[-1]-cs[-10])/path if path else 0
    signs=[1 if x>0 else -1 for x in moves if x]
    reversals=sum(a!=b for a,b in zip(signs,signs[1:]))
    if efficiency<.25 and reversals>=4:
        return row|dict(reason="choppy_daily",efficiency=round(efficiency,3))
    rv=rsi(cs)
    if rv is None:return row|dict(reason="rsi_unavailable")
    if (sign>0 and rv>=78) or (sign<0 and rv<=22):
        return row|dict(reason="rsi_overextended",rsi=round(rv,1))
    extension=sign*(price-e10[-1])/atr
    impulse=sign*(price-cs[-4])/atr
    if extension>1.6 or (impulse>3.5 and extension>1.0):
        return row|dict(reason="late_chase",extension_atr=round(extension,2))
    he6=ema(hs,6);he12=ema(hs,12)
    hourly_ok=sign*(hs[-1]-he6[-1])>0 and sign*(he6[-1]-he12[-1])>0
    if horizon=="2-3" and not hourly_ok:return row|dict(reason="hourly_trigger_missing")
    fived=100*(cs[-1]/cs[-6]-1)
    spy_fived=100*(spycs[-1]/spycs[-6]-1)
    relative=sign*(fived-spy_fived)
    weekly=sign*fived>0
    if horizon=="5-10" and not weekly:return row|dict(reason="weekly_momentum_conflict")
    score=round(25+(20 if hourly_ok else 0)+
                (15 if relative>.5 else 10 if relative>0 else 3 if relative>-.5 else 0)+
                (15 if weekly else 0)+(10 if notional>=2000000 else 5)+
                (15 if extension<.7 else 10 if extension<1.2 else 5),1)
    eligible=score>=LIMITS[horizon]
    return row|dict(reason="qualified" if eligible else "score_below_minimum",
         eligible=eligible,score=score,price=round(price,3),rsi=round(rv,1),
         atr14=round(atr,3),extension_atr=round(extension,2),
         chase="LOW" if extension<.7 else "MED",
         relative_spy_5d_pct=round(relative,2),five_day_move_pct=round(fived,2),
         hourly_aligned=hourly_ok,iex_avg_notional=round(notional),
         efficiency=round(efficiency,3))

def rank(data,day,closed):
    rawday=data.get("day",{});rawhour=data.get("hour",{})
    names=sorted(set(UNIVERSE+["SPY"]))
    daily={s:clean(rawday.get(s,[]),day,"day",closed) for s in names}
    hourly={s:clean(rawhour.get(s,[]),day,"hour",closed) for s in names}
    result=[study(s,daily[s],hourly[s],daily["SPY"],day,closed,h,side)
            for s in names if s!="SPY" for h in HORIZONS for side in ("BULLISH","BEARISH")]
    picks=[]
    for h in HORIZONS:
        for side in ("BULLISH","BEARISH"):
            pool=[r for r in result if r["horizon"]==h and r["direction"]==side and r["eligible"]]
            picks+=sorted(pool,key=lambda r:(-r["score"],-r["relative_spy_5d_pct"],r["ticker"]))[:MAX_PER_SIDE]
    return dict(picks=picks,rows=result,reason_counts=dict(Counter(x["reason"] for x in result)),
                coverage={s:dict(daily=len(daily[s]),hourly=len(hourly[s])) for s in names})

def history(closed):
    symbols=sorted(set(UNIVERSE+["SPY"]))
    batches=[symbols[i:i+35] for i in range(0,len(symbols),35)]
    jobs=[("day",batch,"1Day",closed-timedelta(days=280),closed+timedelta(minutes=10))
          for batch in batches]
    jobs += [("hour",batch,"1Hour",closed-timedelta(days=15),closed+timedelta(minutes=5))
             for batch in batches]
    result={"day":{},"hour":{}}
    failures=[]
    def job(arg):
        kind,syms,frame,start,end=arg
        try:
            return kind,fetch(syms,frame,start,end),None
        except Exception as exc:
            return kind,{},safe_error(exc)
    # Limit parallel provider work to reduce temporary overload. A whole
    # missing batch is failure, NOT zero candidate signals.
    with ThreadPoolExecutor(max_workers=2) as pool:
        for kind,data,error in pool.map(job,jobs):
            if error:failures.append(error)
            else:result[kind].update(data)
    if failures:
        raise ProviderBatchFailure(failures)
    return result

def completed_session(now):
    date=now.astimezone(ET).date()
    for offset in range(9):
        session=schedule((date-timedelta(days=offset)).isoformat())
        if session.get("is_open"):
            close=dt(session["close"])
            if now>=close+timedelta(minutes=15):
                return session["date"],close
    return None,None

def analyze(now):
    day,closed=completed_session(now)
    if day is None:return dict(status="no_complete_session")
    findings=rank(history(closed),day,closed)
    return dict(status="ready",day=day,asof=now.isoformat(),feed="IEX",
                mode="SWING_RESEARCH_WATCH_ONLY",universe_count=len(UNIVERSE),
                horizons=list(HORIZONS),max_per_side=MAX_PER_SIDE,
                picks=findings["picks"],rows=findings["rows"],
                reason_counts=findings["reason_counts"],coverage=findings["coverage"],
                chart_alerts_required=False,bigdata_calls=0,orders=0,
                cautions=["Research watch, not validated TradingView swing entry/exit",
                          "IEX not SIP; options liquidity/earnings not checked",
                          "No forced Top10 count, no simulated trades"])

def telegram(report):
    lines=[f'MARKET FORGE | SWING SCANNER SHADOW {report["day"]}',
           "IEX 일봉+정규장 1시간봉 연구용 후보 · 매매 진입 신호 아님",
           "옵션 유동성/실적/Bigdata 촉매 확인 전 · 억지 후보 없음",""]
    for h in HORIZONS:
        for side,tag in (("BULLISH","🟢 Bullish"),("BEARISH","🔴 Bearish")):
            picks=[r for r in report["picks"] if r["horizon"]==h and r["direction"]==side]
            lines.append(f'{h}거래일 {tag}: {len(picks)}/{MAX_PER_SIDE}')
            if not picks:lines.append("기준 충족 후보 없음")
            for r in picks:
                lines.append(f'{r["ticker"]} {r["score"]:.0f}점 · USD {r["price"]:.2f} · {r["chase"]} · RS {r["relative_spy_5d_pct"]:+.1f}%')
            lines.append("")
    reasons=report["reason_counts"]
    lines.append("제외: CHOPPY "+str(reasons.get("choppy_daily",0))+
                 " / CHASE "+str(reasons.get("late_chase",0))+
                 " / 1시간 조건 "+str(reasons.get("hourly_trigger_missing",0)))
    lines.append("기존 DAY·SWING 주문/보호 신호 변경 없음")
    msg="\n".join(lines)
    if len(msg)>3900:raise ValueError("telegram_too_long")
    return msg

def scheduled(now):
    local=now.astimezone(DENVER)
    return local.weekday()<5 and local.hour==14 and 25<=local.minute<=39

def worker(store,send,now):
    if not scheduled(now):return dict(status="outside_1425_mountain")
    day,closed=completed_session(now)
    if day!=now.astimezone(ET).date().isoformat():
        return dict(status="no_complete_current_session")
    key=PREFIX+day+":report"
    with store.transaction() as tx:existing=tx.get(key)
    if existing is None:
        try:report=analyze(now)
        except ProviderBatchFailure as exc:
            result=dict(status="provider_batch_failed",failed_batches=exc.count,
                        causes=exc.causes,at=now.isoformat())
            with store.transaction() as tx:
                tx.put(PREFIX+day+":last_error",result)
            return result
        except Exception as exc:
            result=dict(status="data_or_evaluation_unavailable",
                        cause=safe_error(exc),at=now.isoformat())
            with store.transaction() as tx:
                tx.put(PREFIX+day+":last_error",result)
            return result
        if report["status"]!="ready":
            return dict(status="not_ready")
        covered=sum(x["daily"]>=MIN_BARS for x in report["coverage"].values())
        if covered<50:
            result=dict(status="daily_coverage_insufficient",covered=covered,
                        at=now.isoformat())
            with store.transaction() as tx:
                tx.put(PREFIX+day+":last_error",result)
            return result
        # An empty hourly universe would make every trade hypothesis look
        # invalid even if daily bars arrived; fail closed instead.
        hourly_covered=sum(x["hourly"]>=9 for x in report["coverage"].values())
        if hourly_covered<50:
            result=dict(status="hourly_coverage_insufficient",covered=hourly_covered,
                        at=now.isoformat())
            with store.transaction() as tx:
                tx.put(PREFIX+day+":last_error",result)
            return result
        with store.transaction() as tx:
            if tx.get(key) is None:
                tx.put(PREFIX+day+":last_error",dict(status="recovered_or_ok",
                                                      at=now.isoformat()))
                tx.put(key,report)
                tx.enqueue(PREFIX+day+":telegram",{
                   "text":telegram(report),
                   "expires_at":(now+timedelta(minutes=12)).isoformat()})
        created=True
    else:
        report=existing;created=False
    delivered=drain(store,send,prefix=PREFIX+day+":telegram",limit=1,now=now)
    return dict(status="recorded",day=day,created=created,
                selected=len(report["picks"]),delivery=[r.get("status") for r in delivered],
                bigdata_calls=0)

def install(app,storage,send):
    @app.get("/swing-scanner-shadow",response_class=HTMLResponse)
    def panel():
        return '<meta charset="utf-8"><h2>SWING SCANNER SHADOW | 2~3 / 5~10 거래일</h2><p>IEX 일봉+정규장 1시간봉 · 100종목 연구용</p><form method="post" action="/swing-scanner-shadow/control"><input type="password" name="secret" required placeholder="기존 웹훅 키"><button name="action" value="status">저장된 후보</button><button name="action" value="preview">마지막 거래일 새로 분석 (Telegram 없음)</button></form><p>기존 MC·SCMP·X·SX·Swing 알림 변경 없음</p>'
    @app.post("/swing-scanner-shadow/control",response_class=HTMLResponse)
    async def control(request:Request):
        raw=await request.body()
        if len(raw)>2048:raise HTTPException(400,"Too large")
        fields=parse_qs(raw.decode());auth(fields.get("secret",[""])[0])
        action=fields.get("action",["status"])[0]
        if action=="preview":
            try:result=analyze(utc())
            except Exception:result=dict(status="data_or_provider_unavailable")
        elif action=="status":
            day,_=completed_session(utc())
            with storage().transaction() as tx:
                result=tx.get(PREFIX+day+":report",dict(status="not_recorded",day=day)) if day else dict(status="no_complete_session")
        else:raise HTTPException(400,"Invalid action")
        return '<meta charset="utf-8"><h2>Swing Scanner 연구 결과</h2><pre>'+html.escape(json.dumps(result,ensure_ascii=False,indent=2))+'</pre><a href="/swing-scanner-shadow">Back</a>'
    @app.get("/scanner/swing-shadow/worker")
    def cron(request:Request):
        secret=os.getenv("CRON_SECRET","")
        if not secret or not hmac.compare_digest(request.headers.get("authorization",""),"Bearer "+secret):
            raise HTTPException(401,"Unauthorized")
        result=worker(storage(),send,utc())
        print("SWING_SCANNER_SHADOW "+json.dumps(result,sort_keys=True),flush=True)
        return result
