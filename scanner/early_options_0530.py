"""Isolated 05:30-07:25 MT extended-options UNDERLYING watch; no trades."""
import hmac
import os
from datetime import timedelta, time

from fastapi import HTTPException, Request
from .free_iex import DENVER, calendar, bounds, utc, api, DataError
from .preopen.core import validate, stamp
from .early_schedule_shadow import first_three_1m, flip_five_1m
from .intraday_v3 import evaluate, select, change
from .store import drain

PREFIX = "early-options-0530:"
# Finalized Cboe GTH underlying list, Oct 19 2026; not a Robinhood
# account-specific eligibility list. Never assert option liquidity.
SYMBOLS = ("SPY","QQQ","IWM","GLD","XLF","AAPL","AMD","AMZN","AVGO",
           "BABA","BAC","F","GOOG","GOOGL","HOOD","INTC","META",
           "MRVL","MSFT","MU","NFLX","NOK","NVDA","ORCL","PFE",
           "PLTR","SNDK","SPCX","TSLA","TSM")


def slot(now):
    local = now.astimezone(DENVER)
    t = local.replace(second=0,microsecond=0).time().replace(tzinfo=None)
    if local.weekday() >= 5 or not time(5,30) <= t <= time(7,25):
        return None
    if local.minute % 5 != 0:
        return None
    return (local.date().isoformat(),
            local.replace(hour=5,minute=30,second=0,microsecond=0),
            local.replace(second=0,microsecond=0))


def sip_bars(symbols, frame, start, end):
    result = {s: [] for s in symbols}
    page = None
    seen = set()
    for _ in range(8):
        params = dict(symbols=",".join(symbols),timeframe=frame,
                      start=start.isoformat(),end=end.isoformat(),
                      feed="sip",adjustment="raw",sort="asc",limit=10000)
        if page: params["page_token"] = page
        data = api("/v2/stocks/bars",params)
        if not isinstance(data.get("bars"),dict): raise DataError("invalid_sip")
        for symbol,rows in data["bars"].items():
            if symbol not in result or not isinstance(rows,list):
                raise DataError("invalid_sip_symbol")
            result[symbol].extend(rows)
        page = data.get("next_page_token")
        if not page: return result
        if page in seen: raise DataError("sip_page_cycle")
        seen.add(page)
    raise DataError("sip_paging_incomplete")


def consecutive(raw, minutes, opened, cutoff, count):
    try: rows = validate(raw,minutes,opened,cutoff)
    except (ValueError,KeyError,TypeError,OverflowError): return None
    if len(rows)<count: return None
    rows=rows[-count:]
    times=[cutoff-timedelta(minutes=i*minutes) for i in range(count,0,-1)]
    return rows if [stamp(b["t"]) for b in rows]==times else None


def candidates(one,five,opened,cutoff):
    last5={s:consecutive(one.get(s,[]),1,opened,cutoff,5)
           for s in SYMBOLS}
    last5={s:bars for s,bars in last5.items() if bars}
    if "SPY" not in last5:
        return None,0
    prelim=first_three_1m({s:bars[-3:] for s,bars in last5.items()})
    flips=flip_five_1m(last5)
    result={}
    for row in prelim+flips:
        if row["state"] not in ("EARLY_MOMENTUM_PRELIM","POSSIBLE_FLIP"):
            continue
        result[row["ticker"]]=dict(ticker=row["ticker"],side=row["side"],
             state=row["state"],score=None,chase="UNKNOWN",
             relative=row.get("spy_relative_pct",0),price=row["price"])
    stock5={}
    for s in SYMBOLS:
        try: b=validate(five.get(s,[]),5,opened,cutoff)
        except (ValueError,KeyError,TypeError,OverflowError): continue
        if len(b)>=6 and consecutive(b,5,opened,cutoff,6):
            stock5[s]=b
    if "SPY" in stock5:
        closes=[b["c"] for b in stock5["SPY"]]
        spy_move=change(closes) if len(closes)>=7 else None
        rows=[evaluate(s,b,opened,cutoff,spy_move)
              for s,b in stock5.items() if s!="SPY"]
        for row in select(rows,strong=True):
            result[row["ticker"]]=dict(
                ticker=row["ticker"],side=row["direction"],
                state="STRONG_LOW_WATCH",score=row["score"],
                chase=row["chase"],relative=row.get("relative_spy_pct") or 0,
                price=row["price"])
        # Same CHOP/late-chase veto as the regular-hours engine.
        for row in rows:
            if row.get("choppy") or row.get("late_chase") or row.get("chase")=="HIGH":
                result.pop(row["ticker"],None)
    picks=sorted(result.values(),key=lambda p:
       (p["state"]!="STRONG_LOW_WATCH",-abs(p["relative"]),p["ticker"]))
    return picks[:5],len(last5)


def regular_open_handoff(store,now):
    """Record a clean 07:30 MT boundary; do not reuse premkt as RTH bars."""
    local=now.astimezone(DENVER)
    if local.weekday()>=5 or local.hour!=7 or local.minute!=30:
        return None
    market,_=calendar(now)
    if not market or market.get("date")!=local.date().isoformat():
        return {"status":"market_closed","orders":0}
    opened,_=bounds(market)
    if not opened<=now<opened+timedelta(minutes=1):
        return {"status":"not_regular_open","orders":0}
    key=PREFIX+market["date"]+":rth_handoff"
    with store.transaction() as tx:
        record=tx.get(key)
        if record is None:
            previous=tx.get(PREFIX+market["date"]+":latest")
            cutoff=previous.get("cutoff") if previous else None
            fresh=False
            if cutoff:
                from datetime import datetime
                elapsed=(opened-datetime.fromisoformat(cutoff)).total_seconds()
                fresh=0<=elapsed<=15*60
            record=dict(status="REGULAR_SESSION_OPEN",
                        opened_at=opened.isoformat(),observed_at=now.isoformat(),
                        premarket_feed="SIP_STOCK" if fresh else "MISSING",
                        premarket_cutoff=cutoff if fresh else None,
                        premarket_symbols=[p["ticker"] for p in previous.get("selected",[])]
                                         if fresh else [],
                        next_early_watch="07:33 MT IEX first 3 completed RTH minutes",
                        options_quotes="UNVERIFIED",robinhood_eligibility="UNVERIFIED",
                        research_only=True,orders=0)
            tx.put(key,record)
    return {"status":"regular_open_handoff",
            "premarket":record["premarket_feed"],"next_early":"07:33_MT",
            "orders":0}


def worker(store,send,now):
    handoff=regular_open_handoff(store,now)
    if handoff is not None:return handoff
    period=slot(now)
    if not period: return {"status":"outside_schedule"}
    day,opened,cutoff=period
    market,_=calendar(now)
    if not market or market.get("date")!=day: return {"status":"market_closed"}
    key=PREFIX+day+":slot:"+cutoff.strftime("%H%M")
    with store.transaction() as tx:
        if tx.get(key):return {"status":"already_scanned"}
    if cutoff==opened:
        with store.transaction() as tx:tx.put(key,{"status":"warmup"})
        return {"status":"warmup","first_closed_5m":"05:35 MT"}
    try:
        one=sip_bars(SYMBOLS,"1Min",opened,cutoff)
        five=sip_bars(SYMBOLS,"5Min",opened,cutoff)
        picks,valid=candidates(one,five,opened,cutoff)
    except (DataError,ValueError,KeyError,TypeError):
        return {"status":"sip_data_unavailable"}
    if picks is None:
        return {"status":"no_complete_fresh_sip","valid":valid}
    pending=[]
    with store.transaction() as tx:
        if tx.get(key):return {"status":"already_scanned"}
        oldkey=PREFIX+day+":notified"
        old=tx.get(oldkey,{})
        for p in picks:
            if old.get(p["ticker"])==p["side"]:continue
            pending.append(p)
            old[p["ticker"]]=p["side"]
        tx.put(oldkey,old)
        tx.put(PREFIX+day+":latest",dict(
            at=now.isoformat(),cutoff=cutoff.isoformat(),feed="SIP_STOCK",
            mode="WATCH_ONLY",options_quotes="UNVERIFIED",
            robinhood_eligibility="UNVERIFIED",selected=picks))
        tx.put(key,{"status":"scanned","count":len(picks)})
        if pending:
            m=now.astimezone(DENVER)
            lines=[f"MARKET FORGE AI | EARLY OPTIONS {m:%H:%M} MT",
                   "SIP 기초주식 모멘텀 · WATCH ONLY · 주문 아님",
                   "옵션 호가/유동성/Robinhood 개별 지원 미확인",""]
            for p in pending:
                score=str(p["score"]) if p["score"] is not None else "PRELIM"
                lines.append(f"{p['ticker']} {p['side']} · {p['state']}"
                             f" · {score} · USD {p['price']:.2f}")
            tx.enqueue(PREFIX+day+":msg:"+cutoff.strftime("%H%M"),
                       {"text":"\n".join(lines),
                        "expires_at":(now+timedelta(minutes=5)).isoformat()})
    sent=drain(store,send,prefix=PREFIX+day+":msg:",now=utc(),limit=3) if pending else []
    return {"status":"scanned","asof":cutoff.isoformat(),"candidates":len(picks),
            "new":len(pending),"feed":"SIP","options":"UNVERIFIED",
            "delivery_states":[d.get("status") for d in sent],"orders":0}


def install(app,storage,send):
    @app.get("/scanner/early-options-0530/worker")
    def cron(request:Request):
        secret=os.getenv("CRON_SECRET","")
        if not secret or not hmac.compare_digest(
              request.headers.get("authorization",""),"Bearer "+secret):
            raise HTTPException(401,"Unauthorized")
        return worker(storage(),send,utc())
