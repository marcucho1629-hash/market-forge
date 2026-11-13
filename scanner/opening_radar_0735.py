"""Opening 7:35/7:40 5-minute completed-bar RADAR.

First regular-session bar is observation, not an MC/SCMP signal or CHASE LOW
confirmation. Never use premarket or future bars. Remains isolated from the
existing first-15m EARLY WATCH and from the DAY/Swing trading engine.
"""
import json
import uuid
from collections import Counter
from datetime import datetime,timedelta

from .free_iex import DENVER
from .preopen.core import validate
from .store import drain

MIN_FIRST_MOVE_PCT=.45
MAX_FIRST_MOVE_PCT=2.5
MIN_RELATIVE_SPY_PCT=.30
MIN_FIRST_IEX_NOTIONAL=250000
MAX_SEND_PER_BUCKET=3
MAX_SEND_PER_DAY=6

def evaluate(ticker,bars,spy_bars,opened,end):
    result={"ticker":ticker,"eligible":False,"reason":"unverified",
            "chase":"UNKNOWN","options_liquidity":"NOT_VERIFIED",
            "catalyst":"UNKNOWN","mode":"OPENING_1_2_BAR_RADAR"}
    try:
        own=validate(bars,5,opened,end)
        spy=validate(spy_bars,5,opened,end)
    except (ValueError,KeyError,TypeError):
        return result|{"reason":"invalid_data"}
    count=int((end-opened).total_seconds()//300)
    if count not in (1,2):
        return result|{"reason":"outside_radar_window"}
    if len(own)!=count or len(spy)!=count:
        return result|{"reason":"missing_5m_bar"}
    def timestamp(x):return datetime.fromisoformat(x["t"].replace("Z","+00:00"))
    expected=[opened+timedelta(minutes=5*i) for i in range(count)]
    if [timestamp(x) for x in own]!=expected or [timestamp(x) for x in spy]!=expected:
        return result|{"reason":"opening_bar_gap"}
    first=own[0]["o"];last=own[-1]["c"]
    if first<5:return result|{"reason":"price_below_5"}
    move_pct=100*(last/first-1)
    direction=1 if move_pct>0 else -1 if move_pct<0 else 0
    if direction==0 or abs(move_pct)<MIN_FIRST_MOVE_PCT:
        return result|{"reason":"weak_first_wave"}
    if abs(move_pct)>MAX_FIRST_MOVE_PCT:
        return result|{"reason":"opening_spike_chase"}
    if count==2 and any(direction*(b["c"]-b["o"])<=0 for b in own):
        return result|{"reason":"mixed_bars"}
    spy_pct=100*(spy[-1]["c"]/spy[0]["o"]-1)
    relative=direction*(move_pct-spy_pct)
    if relative<MIN_RELATIVE_SPY_PCT:
        return result|{"reason":"weak_vs_spy"}
    notional=sum(x["v"]*x["vw"] for x in own)
    if notional<MIN_FIRST_IEX_NOTIONAL:
        return result|{"reason":"iex_liquidity_low"}
    # Price must close in directional quarter of each current RTH candle.
    b=own[-1]
    width=b["h"]-b["l"]
    if width<=0:return result|{"reason":"zero_range"}
    location=(b["c"]-b["l"])/width
    body=abs(b["c"]-b["o"])/width
    if body<.55 or (direction>0 and location<.75) or (direction<0 and location>.25):
        return result|{"reason":"weak_close_location"}
    return result|{"eligible":True,"reason":"qualified","bars":count,
        "direction":"BULLISH" if direction>0 else "BEARISH",
        "price":round(last,3),"move_pct":round(move_pct,2),
        "relative_spy_pct":round(relative,2),"iex_notional":round(notional),
        "bar_cutoff":end.isoformat()}

def message(picks,now,end):
    local=now.astimezone(DENVER)
    cutoff=end.astimezone(DENVER)
    lines=[f"MARKET FORGE AI | 07:35 OPEN RADAR {local:%H:%M} MT",
        f"완성 5분봉 {cutoff:%H:%M} MT · 첫 {picks[0]['bars']}봉만 관찰",
        "🟡 PRELIMINARY · 초기 움직임 감지 · 매매 신호 아님",
        "CHASE LOW 검증 전 (UNKNOWN) · 옵션/촉매 미검증", ""]
    for p in picks:
        icon="🟢" if p["direction"]=="BULLISH" else "🔴"
        lines.append(f'{icon} {p["ticker"]} {p["direction"]} · USD {p["price"]:.2f} · 시작대비 {p["move_pct"]:+.2f}% · SPY 상대 {p["relative_spy_pct"]:+.2f}%')
    lines+=["","첫 1~2개 봉만으로 지속 모멘텀·안전한 진입을 판단하지 말 것",
            "7:45 이후 EARLY WATCH 및 8:00 Top 10에서 재검증"]
    msg="\n".join(lines)
    if len(msg)>3900:raise ValueError("radar_telegram_limit")
    return msg

def worker(store,send,now,opened,end,day,prefix,symbols,gather,observer=None):
    if end not in (opened+timedelta(minutes=5),opened+timedelta(minutes=10)):
        return {"status":"radar_waiting_for_0735"}
    token=str(uuid.uuid4())
    bucket=int(end.timestamp())//300
    done=prefix+"early:radar:done:"+str(bucket)
    leasekey=prefix+"early:radar:lease"
    with store.transaction() as tx:
        if tx.get(done):return {"status":"radar_already_scanned","asof":end.isoformat()}
        lease=tx.get(leasekey,{})
        if lease and (now-datetime.fromisoformat(lease["at"])).total_seconds()<120:
            return {"status":"radar_busy"}
        tx.put(leasekey,{"token":token,"at":now.isoformat()})
    try:
        market=gather(symbols,opened,end)
        spy=market.get("SPY",[])
        rows=[evaluate(s,market.get(s,[]),spy,opened,end) for s in symbols if s!="SPY"]
        # Optional date-limited research tap gets all real raw RTH bars,
        # including those rejected as CHASE or mixed two-bar direction.
        # Failure must never suppress live opening alerts.
        observer_state=None
        if observer is not None:
            try:observer_state=observer(market)
            except Exception:observer_state="research_capture_failed"
        qualified=[r for r in rows if r["eligible"]]
        qualified=sorted(qualified,key=lambda r:(-r["relative_spy_pct"],-r["iex_notional"],r["ticker"]))
        with store.transaction() as tx:
            sent=tx.get(prefix+"early:sent",{})
            used=sum(v.get("origin")=="RADAR" for v in sent.values())
            fresh=[r for r in qualified if r["ticker"] not in sent][:min(MAX_SEND_PER_BUCKET,max(0,MAX_SEND_PER_DAY-used))]
            if fresh:
                for r in fresh:sent[r["ticker"]]={"origin":"RADAR","side":r["direction"],"at":now.isoformat()}
                tx.put(prefix+"early:sent",sent)
                tx.enqueue(prefix+"early:radar:msg:"+str(bucket),{
                    "text":message(fresh,now,end),
                    "expires_at":(now+timedelta(minutes=7)).isoformat()})
            tx.put(prefix+"early:radar:latest",{"at":now.isoformat(),"bar_cutoff":end.isoformat(),
                "eligible":len(qualified),"new":len(fresh),"items":fresh,
                "reasons":dict(Counter(r["reason"] for r in rows)),
                "mode":"PRELIMINARY_WATCH","options_liquidity":"NOT_VERIFIED"})
            tx.put(done,{"at":now.isoformat(),"count":len(qualified)})
            tx.put(leasekey,{})
        delivery=drain(store,send,prefix=prefix+"early:radar:msg:",now=now,limit=2)
        result={"status":"radar_scanned","asof":end.isoformat(),"eligible":len(qualified),
                "new":len(fresh),"telegram_queued":bool(fresh),"delivery":delivery}
        if observer is not None:result["research_observer"]=observer_state
        return result
    except Exception:
        with store.transaction() as tx:
            if tx.get(leasekey,{}).get("token")==token:
                tx.put(leasekey,{})
            tx.put(prefix+"early:radar:error",{"at":now.isoformat(),
                    "error":"data_or_dependency_failure"})
        return {"status":"radar_error"}
