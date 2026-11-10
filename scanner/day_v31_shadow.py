"""One-off 2026-11-11 DAY V3.1 SHADOW: 1+2+3 vs closed 3m, 20 IEX symbols.

No orders, no MC/SCMP trade alerts, no Telegram intraday, no live engine
mutation. Replays exactly the same observed 1m bars for A/B after close,
then matches 5m Scanner Top10 LOW decisions available strictly as-of each
signal time. C = latency opportunity diagnostic, NOT a deployed fast scanner.
"""
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import html, hmac, json, os
from urllib.parse import parse_qs
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse
from .calendar import schedule
from .core import ET
from .free_iex import DENVER, auth, utc
from .preopen.core import validate
from .preopen.runtime import fetch
from .store import drain
from .v136_live.engine import Bar, Config, Engine, SYMBOLS

TARGET_DAY="2026-11-11"
PREFIX="day-v31-shadow:"
LOOKBACK_MAX_SECONDS=11*60
CUTOFF_MINUTES=(5,15,30)

def clock(value):
    return datetime.fromisoformat(value.replace("Z","+00:00"))

def window(now):
    local=now.astimezone(DENVER)
    # 14:07 to 14:19 MT after RTH close, local-DST safe.
    return local.date().isoformat()==TARGET_DAY and local.hour==14 and 7<=local.minute<=19

def prepare_bars(symbol,raw,opened,closed):
    """Do not interpolate absent candles or let conflicting bars pass."""
    valid=validate(raw,1,opened,closed)
    result=[]
    for r in valid:
        b=Bar(symbol,clock(r["t"])+timedelta(minutes=1),
              r["o"],r["h"],r["l"],r["c"],r["v"])
        b.validate()
        result.append(b)
    return result

def replay_one(symbol,bars,mode,opened):
    """Baseline and alternate use identical confirmed 1m inputs; each owns state."""
    engine=Engine(Config(entry_mode=mode))
    events=[]
    gaps=0
    previous=None
    for b in bars:
        if previous is None:
            if b.end!=opened+timedelta(minutes=1):
                engine.recover_gap(b)
                gaps+=1
        elif b.end-previous.end!=timedelta(minutes=1):
            engine.recover_gap(b)
            gaps+=1
        created=engine.on_bar(b)
        for e in created:
            if e.action in ("MC","SCMP"):
                events.append({"ticker":symbol,"action":e.action,
                               "side":e.side,"time":e.time.isoformat(),
                               "price":e.price,"reason":e.reason,
                               "mode":mode})
        previous=b
    return events,gaps

def history_from_rows(rows):
    records=[]
    for row in rows:
        try:
            snap=json.loads(row[0])
            at=clock(snap["observed_at"])
            cutoff=clock(snap["bar_cutoff"])
            if at.tzinfo is None or cutoff.tzinfo is None or at<cutoff:
                continue
            if snap.get("feed")!="IEX_5M" or type(snap.get("selected")) is not list:
                continue
            if at.astimezone(ET).date().isoformat()!=TARGET_DAY:
                continue
            records.append((at,snap))
        except (ValueError,TypeError,KeyError,AttributeError):
            continue
    records.sort(key=lambda x:x[0])
    return records

def gate_event(event,timeline):
    """Selection and chase snapshot must have been observed before signal close."""
    event_time=clock(event["time"])
    eligible=[(at,snap) for at,snap in timeline
              if at<=event_time and clock(snap["bar_cutoff"])<=event_time]
    if not eligible:
        return {"status":"NO_ASOF_TOP10","chase":None,"watch_score":None,
                "watch_asof":None,"lag_sec":None}
    asof,snapshot=eligible[-1]
    lag=(event_time-asof).total_seconds()
    result={"chase":None,"watch_score":None,"watch_asof":asof.isoformat(),
            "lag_sec":round(lag)}
    if lag>LOOKBACK_MAX_SECONDS:
        return result|{"status":"STALE_TOP10"}
    side="BULLISH" if event["side"]==1 else "BEARISH"
    rows=[r for r in snapshot["selected"] if isinstance(r,dict)
          and r.get("ticker")==event["ticker"]]
    if not rows:
        return result|{"status":"NOT_IN_TOP10"}
    r=rows[0]
    result.update(chase=r.get("chase"),watch_score=r.get("score"))
    if r.get("direction")!=side:
        return result|{"status":"OPPOSITE_TOP10"}
    if r.get("eligible") is not True or r.get("choppy") is True or r.get("late_chase") is True:
        return result|{"status":"WATCH_DISQUALIFIED"}
    if r.get("chase")=="LOW":
        return result|{"status":"TOP10_LOW_PASS"}
    if r.get("chase")=="MED":
        return result|{"status":"TOP10_MED_WATCH"}
    return result|{"status":"CHASE_OTHER"}

def forward_returns(event,bars):
    # Exact future final candle closes only; no endpoint substitution.
    by_end={b.end:b.close for b in bars}
    at=clock(event["time"])
    px=event["price"]
    return {str(minutes):round(100*event["side"]*(by_end[at+timedelta(minutes=minutes)]/px-1),3)
            if at+timedelta(minutes=minutes) in by_end else None
            for minutes in CUTOFF_MINUTES}

def summarize(records):
    return {"count":len(records),
            "mc":sum(x["action"]=="MC" for x in records),
            "scmp":sum(x["action"]=="SCMP" for x in records),
            "gate_reasons":dict(Counter(x["gate"]["status"] for x in records)),
            "positive_5m":sum(x["forward_pct"]["5"] is not None and x["forward_pct"]["5"]>0 for x in records),
            "covered_5m":sum(x["forward_pct"]["5"] is not None for x in records),
            "positive_15m":sum(x["forward_pct"]["15"] is not None and x["forward_pct"]["15"]>0 for x in records),
            "covered_15m":sum(x["forward_pct"]["15"] is not None for x in records),
            "positive_30m":sum(x["forward_pct"]["30"] is not None and x["forward_pct"]["30"]>0 for x in records),
            "covered_30m":sum(x["forward_pct"]["30"] is not None for x in records)}

def compare(data,timeline,received_at,preopen=None,live_events=None):
    """Retrospective research; never describe post-close replay as observed live."""
    opened=clock(schedule(TARGET_DAY)["open"])
    closed=clock(schedule(TARGET_DAY)["close"])
    diagnostics={}
    replay={"A":[],"B":[]}
    symbols_with_data=0
    for symbol in SYMBOLS:
        try:
            bars=prepare_bars(symbol,data.get(symbol,[]),opened,closed)
        except (ValueError,TypeError,KeyError,OverflowError) as exc:
            diagnostics[symbol]={"quality":"INVALID_SOURCE_BARS","error_type":type(exc).__name__}
            continue
        if len(bars)<25:
            diagnostics[symbol]={"quality":"SPARSE_1M","bars":len(bars)}
            continue
        symbols_with_data+=1
        map_by_mode={"A":"aligned_123","B":"closed_3m"}
        for name,mode in map_by_mode.items():
            try:
                signals,gaps=replay_one(symbol,bars,mode,opened)
            except (ValueError,TypeError,KeyError,OverflowError) as exc:
                diagnostics[symbol]={"quality":"REPLAY_FAILED","error_type":type(exc).__name__}
                break
            for e in signals:
                e["gate"]=gate_event(e,timeline)
                e["forward_pct"]=forward_returns(e,bars)
                e["observed_at"]=None  # IEX end-of-day replay was NOT a live event
                e["feed"]="ALPACA_IEX_1M_REPLAY"
                replay[name].append(e)
            diagnostics.setdefault(symbol,{"quality":"OK","bars":len(bars),"data_gaps":gaps})
    for name in replay:
        replay[name].sort(key=lambda x:(x["time"],x["ticker"],x["action"]))
    a_keys={(x["ticker"],x["action"],x["time"]) for x in replay["A"]}
    b_keys={(x["ticker"],x["action"],x["time"]) for x in replay["B"]}
    eligible_b=[x for x in replay["B"] if x["gate"]["status"]=="TOP10_LOW_PASS"]
    possible_fast=[x for x in replay["B"] if x["gate"]["status"] in
                   ("TOP10_MED_WATCH","NOT_IN_TOP10","STALE_TOP10","NO_ASOF_TOP10")]
    pre_picks=(preopen or {}).get("prior_rth_context",{}).get("selected",[])
    return {
        "version":"DAY_V31_THREE_MINUTE_SHADOW_REPLAY_V1",
        "day":TARGET_DAY,"generated_at":received_at.isoformat(),
        "source":"Alpaca IEX native 1m (same data to A/B), retrospective",
        "model_A":"unchanged 1m+2m+3m aligned replay",
        "model_B":"completed 3m-only replay, separate hypothetical position state",
        "model_C":"diagnostic only: 3m candidate missed/held by older 5m Top10; not a working fast 150-symbol gate",
        "research_only":True,"orders":0,"new_mc_scmp_telegram":0,"bigdata_calls":0,
        "universe_total":len(SYMBOLS),"universe":list(SYMBOLS),
        "source_symbols_with_25min":symbols_with_data,
        "watch_timeline_snapshots":len(timeline),
        "preopen_prior_watch_count":len(pre_picks),
        "preopen_prior_watch_symbols":[r["ticker"] for r in pre_picks],
        "live_research_bats_events_seen":len(live_events or []),
        "live_bats_note":"Only confirmed persisted V13.36 events; not Telegram sends and not A/B IEX replay.",
        "A":summarize(replay["A"]),
        "B":summarize(replay["B"]),
        "B_plus_TOP10_LOW":summarize(eligible_b),
        "C_fast_recheck_needed_not_validated":summarize(possible_fast),
        "B_only_same_bar":len(b_keys-a_keys),"A_only_same_bar":len(a_keys-b_keys),
        "both_same_bar":len(a_keys & b_keys),
        "symbol_quality":diagnostics,
        "signals_A":replay["A"][:240],
        "signals_B":replay["B"][:240],
        "signal_cap":240,
        "methodology_notes":[
            "A/B are retrospective IEX 1m replays, not actual TradingView BATS signals or execution.",
            "Only 20 of the Scanner's 150 symbols have a matched 3m engine model.",
            "Top10 decision chosen only if recorded observed_at <= signal completed-bar end; no future watchlist leakage.",
            "Top10 LOW may still be stale/unavailable. MED remains WATCH; never relabel MED as LOW.",
            "C is a blocked-candidate diagnostic, not a calibrated or live 3m Top10 engine.",
            "Forward price changes exclude fees, spread, slippage and missing future candles; not win rates.",
            "Do not imply signals arrived in Telegram; baseline/exits in production unchanged."]}

def telegram_report(report):
    a=report["A"];b=report["B"];l=report["B_plus_TOP10_LOW"];c=report["C_fast_recheck_needed_not_validated"]
    lines=["MARKET FORGE AI | 11/11 DAY 3M SHADOW VS TOP10",
        "장 마감 후 동일 IEX 1분봉 회고 비교 · 주문/신규 MC 알림 없음",
        f'20종목 중 데이터 확보 {report["source_symbols_with_25min"]}/20 · 5분 Top10 이력 {report["watch_timeline_snapshots"]}개',
        f'A 기존 1+2+3 일치: {a["count"]}개 (MC {a["mc"]} / SCMP {a["scmp"]})',
        f'B 확정 3분 단독: {b["count"]}개 (MC {b["mc"]} / SCMP {b["scmp"]})',
        f'A와 B 같은 봉 공통 {report["both_same_bar"]} / B만 {report["B_only_same_bar"]}',
        f'B 중 당시 Top10 + CHASE LOW: {l["count"]}개',
        f'B 차단 사유: {", ".join(k+" "+str(v) for k,v in b["gate_reasons"].items()) or "없음"}',
        f'3분 후보였지만 더 빠른 재검증 검토 대상(C 연구): {c["count"]}개',
        f'B+LOW 방향 일치 15분후 {l["positive_15m"]}/{l["covered_15m"]} (검증 아닌 관찰)',
        "C는 아직 실제 150종목 조기스캐너가 아닙니다.",
        "IEX 재생≠BATS 실시간 알림 · 옵션/뉴스/체결비용 미검증",
        "기존 MC·SCMP/X·SX/HX 운영 알림은 변경 없음.",
        "전체 기록: /v31-shadow-compare (기존 웹훅 키 필요)"]
    return "\n".join(lines)[:3700]

def worker(store,send,now):
    if not window(now):
        return {"status":"outside_one_day_post_close_window"}
    market=schedule(TARGET_DAY)
    if not market.get("is_open"):
        return {"status":"exchange_closed"}
    closed=clock(market["close"])
    if now<closed+timedelta(minutes=6):
        return {"status":"wait_for_final_provider_bars"}
    identity=PREFIX+TARGET_DAY+":report"
    with store.transaction() as tx:
        prior=tx.get(identity)
        timeline=history_from_rows(tx.execute("SELECT value FROM mf_kv WHERE key LIKE ?",
                          ("intraday-v3:"+TARGET_DAY+":timeline:%",)).fetchall())
        preopen=tx.get("preopen-watch-v1:"+TARGET_DAY+":report",{})
        live=[x for x in tx.events(TARGET_DAY)
              if x.get("source")=="MF_V136_RESEARCH_LIVE_1" and x.get("action") in ("MC","SCMP")]
    if prior is not None:
        delivery=drain(store,send,prefix=PREFIX+TARGET_DAY+":",limit=1,now=now)
        return {"status":"already_recorded","A":prior["A"]["count"],"B":prior["B"]["count"],
                "B_low":prior["B_plus_TOP10_LOW"]["count"],"delivery":delivery}
    try:
        data=fetch(list(SYMBOLS),"1Min",clock(market["open"]),closed)
        result=compare(data,timeline,now,preopen,live)
        if result["source_symbols_with_25min"]==0:
            return {"status":"data_unavailable_no_report","ticker_quality":result["symbol_quality"],
                    "snapshots":len(timeline)}
        if len(timeline)==0:
            result["quality_warning"]="NO_5M_SCANNER_HISTORY_DO_NOT_CLAIM_TOP10_MATCH"
        with store.transaction() as tx:
            if tx.get(identity) is None:
                tx.put(identity,result)
                tx.enqueue(PREFIX+TARGET_DAY+":telegram",{
                    "text":telegram_report(result),
                    "expires_at":(now+timedelta(minutes=12)).isoformat()})
        delivery=drain(store,send,prefix=PREFIX+TARGET_DAY+":",limit=1,now=now)
        return {"status":"recorded","A":result["A"]["count"],"B":result["B"]["count"],
                "B_low":result["B_plus_TOP10_LOW"]["count"],
                "timeline":len(timeline),"coverage":result["source_symbols_with_25min"],
                "delivery":delivery}
    except Exception:
        return {"status":"research_data_or_dependency_failure","report_recorded":False}

def install(app,storage,send):
    @app.get("/v31-shadow-compare",response_class=HTMLResponse)
    def panel():
        return '<meta charset="utf-8"><h2>DAY V3.1 3-minute Shadow · 2026-11-11</h2><p>연구용 A/B 및 Top10 LOW 비교 · 주문·추가 진입신호 없음</p><form method="post" action="/v31-shadow-compare/control"><input type="password" name="secret" placeholder="기존 웹훅 키" required><button name="action" value="status">11/11 비교 결과</button><button name="action" value="run">마감 후 비교 재시도</button></form>'
    @app.post("/v31-shadow-compare/control",response_class=HTMLResponse)
    async def control(request:Request):
        raw=await request.body()
        if len(raw)>2048:raise HTTPException(400,"Request too large")
        params=parse_qs(raw.decode());auth(params.get("secret",[""])[0])
        action=params.get("action",["status"])[0]
        if action=="run":
            result=worker(storage(),send,utc())
        elif action=="status":
            with storage().transaction() as tx:
                result=tx.get(PREFIX+TARGET_DAY+":report",{"status":"not_yet_recorded","day":TARGET_DAY})
        else:raise HTTPException(400,"Invalid action")
        return '<meta charset="utf-8"><h2>V3.1 Shadow comparison</h2><pre>'+html.escape(json.dumps(result,ensure_ascii=False,indent=2))+'</pre><a href="/v31-shadow-compare">Back</a>'
    @app.get("/scanner/v31-shadow/worker")
    def cron(request:Request):
        secret=os.getenv("CRON_SECRET","")
        if not secret or not hmac.compare_digest(
                request.headers.get("authorization",""),"Bearer "+secret):
            raise HTTPException(401,"Unauthorized")
        result=worker(storage(),send,utc())
        safe={k:result.get(k) for k in
              ("status","A","B","B_low","coverage","timeline","report_recorded")}
        safe["delivery_states"]=[r.get("status") for r in result.get("delivery",[])]
        print("DAY_V31_SHADOW "+json.dumps(safe,sort_keys=True),flush=True)
        return safe
