"""Daily early 7:33 / 7:35 / 7:40 / 7:45 research-only schedule.

Four independent completed-bar as-of checkpoints over the configured 150-name
IEX universe. NO new Telegram, no orders, and no changes to production DAY,
Pine, Swing, MC/SCMP/X/SX/HX or Top10. The next-minute retry is for delayed
market-data arrival only; timestamps always reflect the real observed time.
"""
import hmac,json,os
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from fastapi import HTTPException,Request
from .free_iex import DENVER,calendar,bounds,utc
from .preopen.core import config,validate,stamp
from .preopen.runtime import fetch
from .intraday_early_watch import opening_candidate
from .store import drain

DAY="2026-11-16"  # historical V3.4 report and regression fixture
DAILY_START="2026-11-18"  # Nov 17 had no early captures; never backfill
PREFIX="early-schedule-shadow:"+DAY+":"

def prefix_for(day):
    """Isolate research snapshots by actual session date; preserve Nov16 keys."""
    return PREFIX if day==DAY else "early-schedule-shadow:"+day+":"
TARGETS={"MOMENTUM_0733":(33,"1Min",3),
         "POSSIBLE_FLIP_0735":(35,"1Min",5),
         "CONFIRM_0740":(40,"5Min",10),
         "EARLY_150_0745":(45,"5Min",15)}
STEP_BY_MINUTE={minute+retry:step for step,(minute,_,__) in TARGETS.items()
                for retry in (0,1)}
MIN_UNIVERSE_VALID=50
MIN_IEX_NOTIONAL=60000
MAX_DETAILS=40
LIVE_START="2026-11-18"
LIVE_PREFIX="early-direction-live:"
MAX_LIVE_DAILY=10
LIVE_PER_STAGE={"MOMENTUM_0733":4,"POSSIBLE_FLIP_0735":2,
                "CONFIRM_0740":2,"EARLY_150_0745":3}

def fetch_universe(symbols,frame,start,end):
    groups=[symbols[i:i+50] for i in range(0,len(symbols),50)]
    def one(g):return g,fetch(g,frame,start,end)
    with ThreadPoolExecutor(max_workers=3) as pool:
        result=list(pool.map(one,groups))
    return {symbol:bars.get(symbol,[]) for group,bars in result for symbol in group}

def _complete(raw,minutes,opened,cutoff):
    try:
        b=validate(raw,minutes,opened,cutoff)
        count=int((cutoff-opened).total_seconds()//(60*minutes))
        if len(b)!=count:return None
        if [stamp(x["t"]) for x in b]!=[opened+timedelta(minutes=i*minutes)
                                       for i in range(count)]:return None
        return b
    except (ValueError,KeyError,TypeError,OverflowError):
        return None

def _directional_pct(start,end):
    return 100*(end/start-1) if start and start>0 else 0.

def first_three_1m(complete):
    spy=complete["SPY"]
    spy_move=_directional_pct(spy[0]["o"],spy[-1]["c"])
    results=[]
    for ticker,b in complete.items():
        if ticker=="SPY":continue
        move=_directional_pct(b[0]["o"],b[-1]["c"])
        side="BULLISH" if move>0 else "BEARISH" if move<0 else "NEUTRAL"
        sign=1 if move>0 else -1 if move<0 else 0
        rel=sign*(move-spy_move)
        total_notional=sum(x["v"]*x["vw"] for x in b)
        width=b[-1]["h"]-b[-1]["l"]
        body=abs(b[-1]["c"]-b[-1]["o"])/width if width>0 else 0
        loc=(b[-1]["c"]-b[-1]["l"])/width if width>0 else .5
        extreme=abs(move)>1.5
        strong=(sign!=0 and abs(move)>=.35 and rel>=.20
                and total_notional>=MIN_IEX_NOTIONAL and body>=.40
                and (loc>=.65 if sign>0 else loc<=.35))
        state=("EXTENDED_NO_ENTRY" if extreme else
               "EARLY_MOMENTUM_PRELIM" if strong else "NOT_QUALIFIED")
        results.append(dict(ticker=ticker,side=side,state=state,
                            price=round(b[-1]["c"],4),session_pct=round(move,3),
                            spy_relative_pct=round(rel,3),iex_notional=round(total_notional),
                            bar_count=3,chase="UNKNOWN",entry=False))
    return results

def flip_five_1m(complete):
    spy=complete["SPY"]
    spy_last_move=_directional_pct(spy[3]["o"],spy[4]["c"])
    result=[]
    for ticker,b in complete.items():
        if ticker=="SPY":continue
        first=_directional_pct(b[0]["o"],b[1]["c"])
        last=_directional_pct(b[3]["o"],b[4]["c"])
        side="BULLISH" if last>0 else "BEARISH" if last<0 else "NEUTRAL"
        sign=1 if last>0 else -1 if last<0 else 0
        width=max(x["h"] for x in b[3:])-min(x["l"] for x in b[3:])
        loc=(b[-1]["c"]-min(x["l"] for x in b[3:]))/width if width>0 else .5
        relative=sign*(last-spy_last_move)
        notional=sum(x["v"]*x["vw"] for x in b[3:])
        possible=(first*last<0 and abs(first)>=.20 and abs(last)>=.30 and
                  relative>=.20 and notional>=MIN_IEX_NOTIONAL and
                  (loc>=.70 if sign>0 else loc<=.30))
        session_move=_directional_pct(b[0]["o"],b[-1]["c"])
        extended=abs(session_move)>2.0
        result.append(dict(ticker=ticker,side=side,
                           state="POSSIBLE_FLIP" if possible else
                                 "EXTENDED_NO_ENTRY" if extended else "NOT_QUALIFIED",
                           entry=False,chase="UNKNOWN",bar_count=5,
                           price=round(b[-1]["c"],4),session_pct=round(session_move,3),
                           first_2m_pct=round(first,3),last_2m_pct=round(last,3),
                           spy_relative_pct=round(relative,3),
                           extended=extended,iex_notional=round(notional)))
    return result

def opening_notional(complete,ticker):
    return round(sum(x["v"]*x["vw"] for x in complete[ticker]))

def two_five_5m(complete,opened,cutoff):
    # Lazy import avoids circular V3.4 EOD aggregator -> early schedule.
    from .day_v34_early_flip_freshness import opening_rows
    return [dict(item,iex_notional=opening_notional(complete,item["ticker"]))
            for item in opening_rows(complete,opened,cutoff)]

def three_five_5m(complete,opened,cutoff):
    from .day_v34_early_flip_freshness import opening_rows
    observed=opening_rows(complete,opened,cutoff)
    spy=complete["SPY"]
    spy_change=_directional_pct(spy[0]["o"],spy[-1]["c"])
    result=[]
    for item in observed:
        ticker=item["ticker"]
        # Existing EARLY algorithm reused *unchanged* for research scoring.
        row=opening_candidate(ticker,complete[ticker],opened,cutoff,spy_change)
        extended=item["extended"]
        qualified=bool(row.get("eligible")) and row.get("chase")=="LOW" and not extended
        result.append(dict(ticker=ticker,side=item["side"],
              state="EXTENDED_NO_ENTRY" if extended else
                    "EARLY_LOW_WATCH" if qualified else "NOT_QUALIFIED",
              price=item["price"],session_pct=item["session_move_pct"],
              score=row.get("score"),reason=row.get("reason"),
              iex_notional=opening_notional(complete,ticker),
              chase=row.get("chase","UNKNOWN"),entry=False,bar_count=3))
    return result

def compute(step,market,opened,cutoff):
    expected_frame=TARGETS[step][1]
    minutes=1 if expected_frame=="1Min" else 5
    complete={}
    for ticker,raw in market.items():
        b=_complete(raw,minutes,opened,cutoff)
        if b is not None:complete[ticker]=b
    valid=len(complete)
    if "SPY" not in complete or valid<MIN_UNIVERSE_VALID:
        return dict(status="insufficient_complete_iex_bars",valid_symbols=valid,
                    needed=MIN_UNIVERSE_VALID,spy_complete="SPY" in complete)
    if step=="MOMENTUM_0733":rows=first_three_1m(complete)
    elif step=="POSSIBLE_FLIP_0735":rows=flip_five_1m(complete)
    elif step=="CONFIRM_0740":rows=two_five_5m(complete,opened,cutoff)
    else:rows=three_five_5m(complete,opened,cutoff)
    return dict(status="ready",valid_symbols=valid,rows=rows)

def direction_candidates(stage,rows,previous):
    """Conservative directional notifications, never entry or trade authorization."""
    approved={
        "MOMENTUM_0733":{"EARLY_MOMENTUM_PRELIM"},
        "POSSIBLE_FLIP_0735":{"POSSIBLE_FLIP"},
        "CONFIRM_0740":{"EARLY_FLIP_WATCH","EARLY_MOMENTUM_WATCH"},
        "EARLY_150_0745":{"EARLY_LOW_WATCH"},
    }[stage]
    pool=[]
    for r in rows:
        side=r.get("side")
        if r.get("state") not in approved or side not in ("BULLISH","BEARISH"):
            continue
        if r.get("price",0)<5 or r.get("entry",False):
            continue
        if r.get("iex_notional",0)<(60000 if stage in ("MOMENTUM_0733","POSSIBLE_FLIP_0735") else 150000):
            continue
        if stage=="MOMENTUM_0733" and abs(r.get("session_pct",0))>1.5:
            continue
        if stage=="POSSIBLE_FLIP_0735" and abs(r.get("session_pct",0))>2:
            continue
        if stage=="CONFIRM_0740" and (r.get("extended") or r.get("relative_spy_pct",0)<.30):
            continue
        if stage=="EARLY_150_0745" and (r.get("chase")!="LOW" or not r.get("score")):
            continue
        # One first detection per ticker. A genuine reversed-side flip may
        # send a correction; no duplicate same-side messages across stages.
        old=previous.get(r["ticker"])
        if old and (old["side"]==side or "FLIP" not in r["state"]):
            continue
        pool.append(r)
    pool.sort(key=lambda r:(-r.get("spy_relative_pct",r.get("relative_spy_pct",0)),
                           -r.get("iex_notional",0),r["ticker"]))
    if stage=="MOMENTUM_0733":
        # Prevent one side from crowding out every candidate on the other.
        picks=[]
        for side in ("BULLISH","BEARISH"):
            picks.extend([r for r in pool if r["side"]==side][:2])
        return picks
    return pool[:LIVE_PER_STAGE[stage]]

def direction_message(stage,picks,observed_at,cutoff):
    observed=observed_at.astimezone(DENVER)
    candle=cutoff.astimezone(DENVER)
    title={"MOMENTUM_0733":"EARLY MOMENTUM","POSSIBLE_FLIP_0735":"POSSIBLE FLIP",
           "CONFIRM_0740":"EARLY STRUCTURE","EARLY_150_0745":"LOW SETUP WATCH"}[stage]
    lines=[f"MARKET FORGE AI | LIVE DIRECTION {observed:%H:%M} MT",
           f"📡 {title} · 관측봉 {candle:%H:%M} MT 완료",
           "실시간 방향성 검증 신호 · 주문/MC/SCMP 진입 아님",
           "촉매 미검증 · 옵션 유동성 미검증 · 추격 진입 금지",""]
    for r in picks:
        sign="🟢" if r["side"]=="BULLISH" else "🔴"
        rel=r.get("spy_relative_pct",r.get("relative_spy_pct",0))
        lines.append(f'{sign} {r["ticker"]} {r["side"]} · ${r["price"]:.2f} '
                     f'· RS {rel:+.2f}% · IEX ${r.get("iex_notional",0)/1000:.0f}K')
    lines.append("확정 방향/수익 보장 아님 · 5·15·30분 후 결과 비교 예정")
    msg="\n".join(lines)
    if len(msg)>3900:raise ValueError("direction_message_too_long")
    return msg

def scheduled_step(now):
    local=now.astimezone(DENVER)
    day=local.date().isoformat()
    if (local.weekday()>=5 or local.hour!=7
            or (day!=DAY and day<DAILY_START)):
        return None
    return STEP_BY_MINUTE.get(local.minute)

def worker(store,now,send=None):
    step=scheduled_step(now)
    if not step:return dict(status="outside_shadow_study_window")
    study_day=now.astimezone(DENVER).date().isoformat()
    key=prefix_for(study_day)+step
    with store.transaction() as tx:prior=tx.get(key)
    if prior is not None:return dict(status="already_captured",stage=step,
                                     observed_at=prior["observed_at"],
                                     qualified=prior["qualified"])
    today,_=calendar(now)
    if not today or today["date"]!=study_day:return dict(status="market_closed")
    opened,closed=bounds(today)
    minutes,frame,cutoff_min=TARGETS[step]
    cutoff=opened+timedelta(minutes=cutoff_min)
    if now<cutoff:return dict(status="not_yet_complete",stage=step)
    symbols=list(dict.fromkeys(list(config()["symbols"])+["SPY"]))
    try:
        fetched=fetch_universe(symbols,frame,opened,cutoff)
        result=compute(step,fetched,opened,cutoff)
    except Exception:
        return dict(status="data_provider_or_research_error",stage=step)
    if result["status"]!="ready":
        return dict(status=result["status"],stage=step,
                    valid_symbols=result["valid_symbols"],
                    spy_complete=result["spy_complete"])
    observed_at=utc()
    counts=Counter(r["state"] for r in result["rows"])
    rejects=Counter(r.get("reason","unclassified") for r in result["rows"]
                    if r["state"] in ("NOT_QUALIFIED","EARLY_UNCONFIRMED"))
    entry=dict(stage=step,day=study_day,observed_at=observed_at.isoformat(),
               cutoff=cutoff.isoformat(),feed="IEX",
               input_frame=frame,universe=len(symbols),
               valid_symbols=result["valid_symbols"],
               qualified=sum(x["state"] not in ("NOT_QUALIFIED","EARLY_UNCONFIRMED")
                             for x in result["rows"]),
               states=dict(counts),rejection_reasons=dict(rejects),
               rows=result["rows"],
               no_trade=True,telegram_sent=False,orders=0,
               research_only=True)
    delivery=[]
    live_picks=[]
    live_enabled=(send is not None and study_day>=LIVE_START and
                  0<=(observed_at-cutoff).total_seconds()<=180)
    with store.transaction() as tx:
        if tx.get(key) is None:
            if live_enabled:
                prev=tx.get(LIVE_PREFIX+study_day+":sent",{})
                budget=max(0,MAX_LIVE_DAILY-len(prev))
                picks=direction_candidates(step,result["rows"],prev)[:budget]
                if picks:
                    msg=direction_message(step,picks,observed_at,cutoff)
                    alert_id=LIVE_PREFIX+study_day+":"+step
                    tx.enqueue(alert_id,dict(text=msg,expires_at=(observed_at+
                                              timedelta(minutes=3)).isoformat()))
                    live_picks=[dict(ticker=p["ticker"],side=p["side"],
                                     price=p["price"],state=p["state"])
                                for p in picks]
                    for p in picks:
                        prev[p["ticker"]]=dict(side=p["side"],stage=step,
                                              at=observed_at.isoformat(),
                                              first_price=p["price"])
                    tx.put(LIVE_PREFIX+study_day+":sent",prev)
            entry["live_candidates"]=live_picks
            entry["live_queued"]=bool(live_picks)
            tx.put(key,entry)
    if live_picks:
        delivery=drain(store,send,prefix=LIVE_PREFIX+study_day+":"+step,
                       now=observed_at,limit=1)
    return dict(status="recorded",stage=step,asof=cutoff.isoformat(),
                observed_at=observed_at.isoformat(),valid_symbols=result["valid_symbols"],
                states=dict(counts),rejection_reasons=dict(rejects),
                live_candidates=live_picks,live_delivery=delivery,
                orders=0,telegram_sent=any(x["status"]=="sent" for x in delivery))

def phase_summary(store,five_history,day=DAY):
    """Research add-on; historical V3.4 uses Nov16 unless given a daily date."""
    with store.transaction() as tx:
        stored=[tx.get(prefix_for(day)+step) for step in TARGETS]
    phases={}
    all_early={}
    for step,record in zip(TARGETS,stored):
        if not record:
            phases[step]=dict(status="not_captured")
            continue
        phases[step]=dict(status="recorded",cutoff=record["cutoff"],
            observed_at=record["observed_at"],frame=record["input_frame"],
            valid_symbols=record["valid_symbols"],states=record["states"],
            rejection_reasons=record.get("rejection_reasons",{}))
        for r in record["rows"]:
            if r["state"] in ("EARLY_MOMENTUM_PRELIM","POSSIBLE_FLIP",
                              "EARLY_FLIP_WATCH","EARLY_MOMENTUM_WATCH",
                              "EARLY_LOW_WATCH","EXTENDED_NO_ENTRY"):
                all_early.setdefault((r["ticker"],r.get("side")),dict(
                    ticker=r["ticker"],side=r.get("side"),stage=step,
                    state=r["state"],observed_at=record["observed_at"],
                    entry=False))
    first_low={}
    for snap in sorted(five_history,key=lambda r:stamp(r["at"])):
        if snap.get("source")!="FIVE_MIN":continue
        for r in snap.get("rows",[]):
            if r.get("strong_low") and r.get("side") in ("BULLISH","BEARISH"):
                first_low.setdefault((r["ticker"],r["side"]),snap["at"])
    led=[]
    for key,early in all_early.items():
        if key not in first_low:continue
        delta=(stamp(first_low[key])-stamp(early["observed_at"])).total_seconds()/60
        if delta>=0:
            led.append(dict(**early,first_strong_low_at=first_low[key],
                            lead_minutes=round(delta,1)))
    led.sort(key=lambda r:-r["lead_minutes"])
    return dict(status="ready" if any(stored) else "no_early_captures",
                date=day,stages=phases,stages_recorded=sum(bool(x) for x in stored),
                stages_expected=len(TARGETS),early_distinct_directional=len(all_early),
                first_strong_low_distinct=len(first_low),
                early_preceded_strong_low=len(led),lead_examples=led[:MAX_DETAILS],
                caveats=["Times are actual IEX provider receipt, not theoretical clock time.",
                         "No entry/exit or standalone intraday Telegram signals.",
                         "No synthetic 1m/5m bars; incomplete data means missing stage.",
                         "Initial 3m and 5m flip hypotheses are not confirmed trades."])

def install(app,storage,send=None):
    @app.get("/scanner/early-schedule-shadow/worker")
    def cron(request:Request):
        secret=os.getenv("CRON_SECRET","")
        if not secret or not hmac.compare_digest(
                request.headers.get("authorization",""),"Bearer "+secret):
            raise HTTPException(401,"Unauthorized")
        result=worker(storage(),utc(),send=send)
        # Never log webhook secrets, full ticker-level rows, or raw API payloads.
        print("EARLY_SCHEDULE_SHADOW "+json.dumps(result,sort_keys=True),flush=True)
        return result
