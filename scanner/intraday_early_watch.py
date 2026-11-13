"""Fast WATCH notifications from the existing 5-minute IEX scanner.

Never creates MC/SCMP orders/entries, edits the legacy Pine engine, or changes
the hourly V3 Top 10 selection / V3.1 timeline. A first-15m opening *WATCH*
is necessarily provisional; every alert declares its completed-bar cutoff.
"""
import json
import math
import uuid
from collections import Counter
from datetime import datetime,timedelta

from .free_iex import DENVER
from .preopen.core import validate
from .store import drain

MIN_5M_LIVE_SCORE=60
MAX_LIVE_PER_SCAN=3
MAX_LIVE_PER_DAY=12
MAX_OPEN_PER_SCAN=4
MAX_OPEN_PER_DAY=8
OPENING_NOTIONAL_MIN=150000
OPENING_RELATIVE_MIN_PCT=.20

def opening_candidate(sym,raw,opened,end,spy_change=None):
    """Strict closed-bar, consecutive 5m first-wave WATCH. Not an entry."""
    row=dict(ticker=sym,eligible=False,reason="not_ready",mode="PRE_0800_WATCH")
    try: b=validate(raw,5,opened,end)
    except (ValueError,KeyError,TypeError):return row|dict(reason="invalid_5m")
    if len(b)<3:return row|dict(reason="fewer_than_three_bars")
    # Never promote premarket bars or discontinuous/not-yet-completed bars.
    bars=b[-min(5,len(b)):]
    ts=[datetime.fromisoformat(x["t"].replace("Z","+00:00")) for x in bars]
    if ts[0]!=opened or any((n-p).total_seconds()!=300 for p,n in zip(ts,ts[1:])):
        return row|dict(reason="opening_bar_gap")
    if end-(ts[-1]+timedelta(minutes=5))>timedelta(minutes=5):
        return row|dict(reason="last_bar_stale")
    first=bars[0]["o"];last=bars[-1]["c"]
    closes=[x["c"] for x in bars]
    moves=[n-p for p,n in zip(closes,closes[1:])]
    up=all(d>0 for d in moves) and last>first
    down=all(d<0 for d in moves) and last<first
    side="BULLISH" if up else "BEARISH" if down else "NEUTRAL"
    row["direction"]=side
    if side=="NEUTRAL":return row|dict(reason="mixed_first_wave")
    sign=1 if up else -1
    percent=100*(last/first-1)
    if sign*percent<.35:return row|dict(reason="insufficient_price_movement")
    notional=sum(x["vw"]*x["v"] for x in bars)
    if notional<OPENING_NOTIONAL_MIN:
        return row|dict(reason="opening_iex_liquidity_low",iex_notional=round(notional))
    if spy_change is None:return row|dict(reason="spy_data_missing")
    rel=sign*(percent-spy_change)
    if rel<OPENING_RELATIVE_MIN_PCT:
        return row|dict(reason="relative_strength_low",relative_spy_pct=round(rel,2))
    volume=sum(x["v"] for x in bars)
    vwap=sum(x["vw"]*x["v"] for x in bars)/volume
    tr=[]
    prev=None
    for x in bars:
        prior=x["o"] if prev is None else prev
        tr.append(max(x["h"]-x["l"],abs(x["h"]-prior),abs(x["l"]-prior)))
        prev=x["c"]
    avgtr=sum(tr)/len(tr)
    if avgtr<=0:return row|dict(reason="atr_invalid")
    dist=sign*(last-vwap)/avgtr
    impulse=sign*(last-first)/avgtr
    if dist>1.25 or impulse>3.5:
        return row|dict(reason="late_chase",vwap_distance_atr=round(dist,2))
    if dist>.85 or impulse>2.5:
        return row|dict(reason="chase_med_watch_only",vwap_distance_atr=round(dist,2))
    close_location=(last-bars[-1]["l"])/(bars[-1]["h"]-bars[-1]["l"]) if bars[-1]["h"]>bars[-1]["l"] else .5
    if (up and close_location<.55) or (down and close_location>.45):
        return row|dict(reason="weak_latest_close")
    score=round(min(95,55+min(18,rel*9)+min(12,sign*percent*4)+
                    min(10,notional/OPENING_NOTIONAL_MIN*2)),1)
    return row|dict(eligible=True,reason="qualified",direction=side,score=score,
                    price=round(last,3),chase="LOW",relative_spy_pct=round(rel,2),
                    iex_notional=round(notional),vwap_distance_atr=round(dist,2),
                    cutoff=(ts[-1]+timedelta(minutes=5)).isoformat())

def open_watch_text(picks,now,end):
    local=now.astimezone(DENVER)
    cutoff=end.astimezone(DENVER)
    lines=[f"MARKET FORGE AI | OPEN EARLY WATCH {local:%H:%M} MT",
           f"완성 5분봉 {cutoff:%H:%M} MT 기준 · 정규장 첫 15~25분",
           "🟡 조기 관찰 후보 · MC/SCMP 진입 신호 아님",
           "IEX 데이터 · CHASE LOW만 · 옵션/촉매 미검증",""]
    for p in picks:
        code="🟢" if p["direction"]=="BULLISH" else "🔴"
        lines.append(f'{code} {p["ticker"]} {p["direction"]} {p["score"]:.0f}점 · USD {p["price"]:.2f} · RS {p["relative_spy_pct"]:+.2f}%')
    lines+=["","예비 모멘텀 감지 · 추격 진입 금지 · 8시 정식 Top 10에서 재검증"]
    result="\n".join(lines)
    if len(result)>3900:raise ValueError("early_watch_message_limit")
    return result

def live_watch_text(picks,now,end):
    local=now.astimezone(DENVER)
    cutoff=end.astimezone(DENVER)
    lines=[f"MARKET FORGE AI | 5MIN NEW LOW WATCH {local:%H:%M} MT",
           f"완성 5분봉 {cutoff:%H:%M} MT 기준",
           "🟡 새로 포착된 CHASE LOW 후보 · 실제 진입 신호 아님",
           "기존 시간별 Top 10과 별도 · 옵션/촉매 미검증",""]
    for p in picks:
        code="🟢" if p["direction"]=="BULLISH" else "🔴"
        lines.append(f'{code} {p["ticker"]} {p["direction"]} {p["score"]:.1f} · USD {p["price"]:.2f} · LOW')
    lines+=["","새로 나타난 WATCH 조건이며 추세 지속을 보장하지 않음"]
    text="\n".join(lines)
    if len(text)>3900:raise ValueError("live_watch_message_limit")
    return text

def opening_worker(store,send,now,opened,end,day,prefix,symbols,gather,observer=None):
    # Today's first open early scans cannot reuse 08+ V3 Top10/entry gate state.
    if not (opened+timedelta(minutes=15)<=end<=opened+timedelta(minutes=25)):
        return dict(status="early_open_waiting")
    bucket=int(end.timestamp())//300
    token=str(uuid.uuid4())
    key=prefix+"early:open:done:"+str(bucket)
    with store.transaction() as tx:
        if tx.get(key):return dict(status="early_open_already_scanned")
        lease=tx.get(prefix+"early:open:lease",{})
        if lease and (now-datetime.fromisoformat(lease["at"])).total_seconds()<120:
            return dict(status="early_open_busy")
        tx.put(prefix+"early:open:lease",dict(token=token,at=now.isoformat()))
    try:
        market=gather(symbols,opened,end)
        spy=validate(market.get("SPY",[]),5,opened,end)
        spy_move=100*(spy[-1]["c"]/spy[0]["o"]-1) if (
            len(spy)>=3 and
            datetime.fromisoformat(spy[0]["t"].replace("Z","+00:00"))==opened and
            (end-(datetime.fromisoformat(spy[-1]["t"].replace("Z","+00:00"))+timedelta(minutes=5))).total_seconds()<=300
            ) else None
        rows=[opening_candidate(symbol,market.get(symbol,[]),opened,end,spy_move)
              for symbol in symbols if symbol!="SPY"]
        # Research tap observes raw 5m first-wave bars regardless of whether
        # they pass live EARLY WATCH gates; isolated from Telegram delivery.
        observer_state=None
        if observer is not None:
            try:observer_state=observer(market)
            except Exception:observer_state="research_capture_failed"
        qualified=[r for r in rows if r["eligible"]]
        chosen=sorted(qualified,key=lambda r:(-r["score"],-r["relative_spy_pct"],r["ticker"]))
        with store.transaction() as tx:
            sent=tx.get(prefix+"early:sent",{})
            budget=max(0,MAX_OPEN_PER_DAY-sum(1 for v in sent.values() if v.get("origin")=="OPEN"))
            fresh=[p for p in chosen if p["ticker"] not in sent][:min(MAX_OPEN_PER_SCAN,budget)]
            tx.put(prefix+"early:open:latest",dict(at=now.isoformat(),bar_cutoff=end.isoformat(),
                   source="IEX_5M",qualified=len(qualified),selected=fresh,
                   reasons=dict(Counter(r["reason"] for r in rows)),quality=len(rows),
                   telegram_queued=bool(fresh),mode="RESEARCH_WATCH"))
            if fresh:
                for p in fresh:sent[p["ticker"]]=dict(side=p["direction"],at=now.isoformat(),origin="OPEN")
                tx.put(prefix+"early:sent",sent)
                tx.enqueue(prefix+"early:open:msg:"+str(bucket),dict(text=open_watch_text(fresh,now,end),
                                      expires_at=(now+timedelta(minutes=7)).isoformat()))
            tx.put(key,dict(at=now.isoformat(),qualified=len(qualified)))
            tx.put(prefix+"early:open:lease",{})
        delivered=drain(store,send,prefix=prefix+"early:open:msg:",now=now,limit=2)
        result=dict(status="early_open_scanned",qualified=len(qualified),new=len(fresh),
                    telegram_queued=bool(fresh),delivery=delivered,asof=end.isoformat())
        if observer is not None:result["research_observer"]=observer_state
        return result
    except Exception:
        with store.transaction() as tx:
            if tx.get(prefix+"early:open:lease",{}).get("token")==token:
                tx.put(prefix+"early:open:lease",{})
            tx.put(prefix+"early:open:error",dict(at=now.isoformat(),error="provider_or_logic_failure"))
        return dict(status="early_open_error")

def live_low_alert(tx,prefix,selected,bucket,now,end,should_send):
    """Transactional, at-most-once, first-discovery LOW notification only.

    First scan initializes baseline, hourly scan owns the hourly message.
    No invented entry signal. No re-sends from prior hour's stale snapshot.
    """
    current={p["ticker"]:p["direction"] for p in selected
             if p.get("eligible") is True and p.get("chase")=="LOW"
             and not p.get("choppy") and not p.get("late_chase")}
    previous=tx.get(prefix+"early:low_previous")
    tx.put(prefix+"early:low_previous",dict(asof=end.isoformat(),by_ticker=current))
    if should_send or not previous:
        return dict(new=0,queued=False,baseline=True)
    prev=previous.get("by_ticker",{})
    observed=[p for p in selected if p["ticker"] in current
              and prev.get(p["ticker"])!=p["direction"]
              and p["score"]>=MIN_5M_LIVE_SCORE]
    sent=tx.get(prefix+"early:sent",{})
    budget=max(0,MAX_LIVE_PER_DAY-sum(1 for v in sent.values() if v.get("origin")=="LIVE"))
    fresh=[p for p in sorted(observed,key=lambda r:-r["score"])
           if p["ticker"] not in sent][:min(budget,MAX_LIVE_PER_SCAN)]
    if fresh:
        for p in fresh:sent[p["ticker"]]=dict(side=p["direction"],at=now.isoformat(),origin="LIVE")
        tx.put(prefix+"early:sent",sent)
        tx.enqueue(prefix+"early:live:msg:"+str(bucket),dict(
            text=live_watch_text(fresh,now,end),
            expires_at=(now+timedelta(minutes=7)).isoformat()))
    events=tx.get(prefix+"early:live:history",[])
    events.append(dict(at=now.isoformat(),asof=end.isoformat(),new=len(fresh),
                       detected=len(observed),suppressed=len(observed)-len(fresh),
                       candidates=[dict(ticker=p["ticker"],direction=p["direction"],score=p["score"])
                                   for p in fresh]))
    tx.put(prefix+"early:live:history",events[-40:])
    return dict(new=len(fresh),queued=bool(fresh),baseline=False)
