"""Independent scheduled watchlist; no orders, news, or production entry state."""
import html
import json
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse
from ..free_iex import api, auth, bounds, calendar, DataError, ET, DENVER, utc
from ..store import drain
from .core import config, config_hash, evaluate, rank, stamp, validate
from .v2 import research_report

PREFIX = 'preopen-watch-v1:'


def fetch(symbols, timeframe, start, end):
    result = {s: [] for s in symbols}
    token = None
    seen = set()
    for _ in range(12):
        params = dict(symbols=','.join(symbols), timeframe=timeframe, start=start.isoformat(), end=end.isoformat(), feed='iex', adjustment='raw', sort='asc', limit=10000)
        if token: params['page_token'] = token
        data = api('/v2/stocks/bars', params)
        if not isinstance(data.get('bars'), dict): raise DataError('bars_invalid')
        for symbol, rows in data['bars'].items():
            if symbol not in result or not isinstance(rows, list): raise DataError('unexpected_bars')
            result[symbol].extend(rows)
        token = data.get('next_page_token')
        if not token: return result
        if token in seen: raise DataError('pagination_cycle')
        seen.add(token)
    raise DataError('pagination_incomplete')


def batches(symbols, start, end, frames):
    jobs = [(symbols[i:i+50], frame) for i in range(0,len(symbols),50) for frame in frames]
    def one(job):
        syms, frame = job
        try:
            values = fetch(syms, frame, start, end)
            return syms, frame, values, None, utc().isoformat()
        except Exception as e:
            # No exception URLs, auth headers, or credentials enter the audit log.
            return syms, frame, {}, str(e) if isinstance(e,DataError) else type(e).__name__, utc().isoformat()
    with ThreadPoolExecutor(max_workers=3) as pool:
        return list(pool.map(one, jobs))


def session_info(store, now):
    day = now.astimezone(ET).date().isoformat()
    with store.transaction() as tx: cached = tx.get(PREFIX+day+':calendar')
    if cached is None:
        today, prior = calendar(now)
        cached = dict(today=today, prior=prior)
        with store.transaction() as tx: tx.put(PREFIX+day+':calendar', cached)
    return cached['today'], cached['prior']


def capture(store, now, session, prior, c):
    day = session['date']
    opened, _ = bounds(session)
    start = opened.astimezone(ET).replace(hour=8,minute=0,second=0,microsecond=0).astimezone(timezone.utc)
    cutoff = opened - timedelta(minutes=10)
    with store.transaction() as tx: seeds = tx.get(PREFIX+day+':seed')
    if seeds is None:
        if not prior: raise DataError('prior_session_missing')
        po, pc = bounds(prior)
        ps = po.astimezone(ET).replace(hour=8,minute=0).astimezone(timezone.utc)
        seeds = dict(bars={}, errors={})
        for syms, _, data, error, at in batches(c['symbols'], ps, pc, ['5Min']):
            for s in syms:
                seeds['bars'][s] = data.get(s,[])
                if error: seeds['errors'][s] = error
        with store.transaction() as tx: tx.put(PREFIX+day+':seed', seeds)
    end = min(now-timedelta(seconds=2), cutoff)
    end = end.replace(second=0,microsecond=0)-timedelta(minutes=end.minute%5)
    snapshot = dict(start=start.isoformat(), end=end.isoformat(), cutoff=cutoff.isoformat(), symbols={}, config_hash=config_hash(c))
    for syms, frame, data, error, received in batches(c['symbols'], start, end, ['1Min','5Min']):
        for s in syms:
            item = snapshot['symbols'].setdefault(s, dict(errors={}, received={}, seed=seeds['bars'].get(s,[])))
            item[frame] = data.get(s,[]); item['received'][frame] = received
            if error: item['errors'][frame] = error
            if s in seeds['errors']: item['errors']['seed'] = seeds['errors'][s]
    snapshot['completed_at'] = utc().isoformat()
    # Preserve every capture rather than overwriting an on-time snapshot with a late one.
    key = PREFIX+day+':capture:'+str(int(now.timestamp()))
    with store.transaction() as tx:
        tx.put(key,snapshot)
        keys = tx.get(PREFIX+day+':captures',[])
        if key not in keys: keys.append(key)
        tx.put(PREFIX+day+':captures',keys)
    return dict(status='captured', end=end.isoformat(), symbols=len(snapshot['symbols']), failed=sum(bool(r['errors']) for r in snapshot['symbols'].values()))


def build_report(store, day, cutoff, c):
    with store.transaction() as tx:
        captures = [tx.get(k) for k in tx.get(PREFIX+day+':captures',[])]
        prior_seed = tx.get(PREFIX+day+':seed', {})
    rows = []
    for s in c['symbols']:
        choices = []
        for snap in captures:
            item = snap['symbols'].get(s)
            if item and snap['config_hash']==config_hash(c):
                received = max(stamp(t) for t in item['received'].values())
                if received <= cutoff: choices.append((stamp(snap['end']),received,snap,item))
        if not choices:
            rows.append(dict(ticker=s, quality='FETCH_FAILED', reasons=['no_on_time_capture'],technical_pass=False))
            continue
        _, received, snap, item = max(choices, key=lambda x:(x[0],x[1]))
        if item['errors']:
            rows.append(dict(ticker=s,quality='FETCH_FAILED',reasons=list(item['errors'].values()),technical_pass=False))
        else:
            rows.append(evaluate(s,item['seed'],item['1Min'],item['5Min'],stamp(snap['start']),cutoff,received,c))
    # Separate earlier RTH context is never marked as a V1 premarket technical pass.
    prior_context = research_report(prior_seed, day, c['symbols'])
    return dict(day=day, mode='RESEARCH_WATCH_ONLY', cutoff=cutoff.isoformat(), config_hash=config_hash(c),
                quality_counts=dict(Counter(r['quality'] for r in rows)),
                quality_reason_counts=dict(Counter(reason for r in rows for reason in r.get('reasons', []))),
                rows=rows, selected=rank(rows,c),
                prior_rth_context=prior_context,
                news_calls=0, feed='iex', strict_minute_pass=sum(r.get('strict_minute_policy_pass',False) for r in rows))


def message(report, now):
    mt = stamp(report['cutoff']).astimezone(DENVER)
    lines = ['MARKET FORGE AI | PRE-OPEN SCANNER', f'{report["day"]} 07:20 MT / 09:20 ET',
             'RESEARCH / WATCH ONLY · 실제 진입 신호 아님', 'IEX 단일 거래소 · Catalyst UNKNOWN',
             f'조회 {len(report["rows"])}종목 · 선정 {len(report["selected"])}종목',
             '데이터: '+', '.join(f'{k} {v}' for k,v in report['quality_counts'].items()),
             f'발송 {now.astimezone(DENVER):%H:%M:%S} MT'+(' · DELAYED' if now > stamp(report['cutoff'])+timedelta(minutes=1) else '')]
    reasons = report.get('quality_reason_counts', {})
    if reasons:
        reasons_top = sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))[:4]
        lines += ['주요 미통과 사유: ' + ', '.join(f'{name} {n}' for name, n in reasons_top)]
    for r in report['selected']:
        direction = '상승' if r['direction']=='BULLISH' else '하락'
        lines += ['', f'{r["ticker"]} {direction} WATCH · {r["score"]}/100 ({r["grade"]}) · ${r["price"]:.2f}',
                  f'EMA9/21 {"UP" if direction=="상승" else "DOWN"} · VWAP {"ABOVE" if direction=="상승" else "BELOW"} · {"HH/HL" if direction=="상승" else "LH/LL"}',
                  f'Chase PASS · RVOL UNKNOWN · {r["quality"]}',
                  f'기준봉 {stamp(r["data_end"]).astimezone(DENVER):%H:%M} MT · 눌림/돌파 관찰',
                  f'구조 무효화 참고 ${r["structural_stop"]:.2f} · 실제 진입 NOT YET']
    if not report['selected']: lines += ['', '장전 검증 통과 후보 0개. 데이터 부족과 기술 조건 탈락을 분리 기록.']
    # V2: prior RTH research context is never promoted to a premarket candidate
    # or a trading signal. The message identifies the distinct session.
    ctx = report.get('prior_rth_context', {})
    prior_picks = ctx.get('selected', [])
    counts = ctx.get('quality_counts', {})
    lines += ['', f'V2 전일 정규장 관찰 후보 {len(prior_picks)}종목 · IEX · 장전 미검증']
    if prior_picks:
        visible = prior_picks[:20] if len(report["selected"]) <= 10 else prior_picks[:10]
        for p in visible:
            direction = '상승관찰' if p['direction'] == 'BULLISH' else '하락관찰'
            lines += [f'{p["ticker"]} {direction} · 전일 {p["prior_date"]} · USD {p["prior_close"]:.2f} · 참고점수 {p["score"]}/100']
    else:
        lines += ['전일 기준값도 부족하거나 기술 관찰 기준 미충족']
    if counts:
        missing = sum(v for k, v in counts.items() if k not in ('context_eligible', 'context_score_below_minimum', 'no_confirmed_prior_trend'))
        lines += [f'전일 데이터/품질 제외 {missing}종목 · 현재일 진입검증 별도']
    lines += ['V2 전일 정보는 07:20 당일 프리마켓 신호·진입 계획이 아닙니다.']
    lines += ['', '점수는 검증 전 연구 가설 · 전체 시장 거래량 아님', 'ID: '+PREFIX+report['day']]
    text = '\n'.join(lines)
    if len(text)>4000: raise ValueError('telegram_message_too_long')
    return text


def finish(store, send, now, session, c):
    day = session['date']; opened,_ = bounds(session); cutoff = opened-timedelta(minutes=10)
    key = PREFIX+day+':report'
    with store.transaction() as tx: report=tx.get(key)
    if report is None:
        report=build_report(store,day,cutoff,c)
        with store.transaction() as tx:
            if not tx.get(PREFIX+'control',{}).get('enabled'): return dict(status='disabled')
            tx.put(key,report)
            tx.enqueue(PREFIX+day+':watchlist', dict(text=message(report,now),expires_at=(opened-timedelta(minutes=5)).isoformat()))
    delivery=drain(store,send,prefix=PREFIX+day+':',now=utc())
    with store.transaction() as tx: tx.put(PREFIX+day+':delivery',delivery or tx.get(PREFIX+day+':delivery',[]))
    return dict(status='finalized',selected=len(report['selected']),quality_counts=report['quality_counts'],delivery=delivery)


def worker(store,send,now):
    c=config(); token=str(uuid.uuid4()); result={}
    # Schedule is restricted in both cron and server; no catch-up of old watchlists.
    local=now.astimezone(DENVER)
    if local.weekday()>4 or local.hour!=7 or not 10<=local.minute<=24:
        return dict(status='outside_preopen_window')
    with store.transaction() as tx:
        if not tx.get(PREFIX+'control',{}).get('enabled'): return dict(status='disabled')
        lease=tx.get(PREFIX+'lease',{})
        if lease and (now-stamp(lease['at'])).total_seconds()<120: return dict(status='busy')
        tx.put(PREFIX+'lease',dict(id=token,at=now.isoformat()))
    try:
        today,prior=session_info(store,now)
        if not today: result=dict(status='market_closed')
        else:
            opened,_=bounds(today); cutoff=opened-timedelta(minutes=10)
            if now<cutoff:
                result=capture(store,now,today,prior,c)
            else:
                result=finish(store,send,now,today,c)
    except Exception as e:
        result=dict(status='error',error=str(e) if isinstance(e,DataError) else type(e).__name__)
    finally:
        with store.transaction() as tx:
            tx.put(PREFIX+'status',result|dict(at=utc().isoformat()))
            if tx.get(PREFIX+'lease',{}).get('id')==token: tx.put(PREFIX+'lease',{})
    return result


def probe(store, now):
    c=config(); today,prior=calendar(now)
    # Read-only market data probe; never eligible for scheduled picks or queued signals.
    session=prior
    if not session: raise DataError('prior_session_missing')
    opened,_=bounds(session)
    start=opened.astimezone(ET).replace(hour=8,minute=0).astimezone(timezone.utc)
    end=opened-timedelta(minutes=10)
    rows=[]
    for syms,_,data,error,at in batches(c['symbols'],start,end,['5Min']):
        for s in syms:
            symbol_error=error
            try: count=len(validate(data.get(s,[]),5,start,end))
            except (ValueError,KeyError,TypeError): count=0; symbol_error='invalid_data'
            rows.append(dict(ticker=s,count=count,status='FETCH_FAILED' if symbol_error else 'DATA_PRESENT' if count else 'EMPTY',error=symbol_error))
    result=dict(at=now.isoformat(),data_day=session['date'],feed='iex',counts=dict(Counter(r['status'] for r in rows)),rows=rows,config_hash=config_hash(c),historical_only=True)
    with store.transaction() as tx: tx.put(PREFIX+'probe',result)
    return result


def install(app,storage,send):
    @app.get('/preopen-watch',response_class=HTMLResponse)
    def panel():
        return '''<meta charset="utf-8"><h1>장전 150종목 WATCHLIST</h1><p>07:20 America/Denver · 무료 IEX · 연구용 · 주문/진입 없음</p><form method="post" action="/preopen-watch/control"><label>웹훅 키 <input type="password" name="secret" required></label><button name="action" value="status">상태 확인</button><button name="action" value="probe">무료 데이터 150종목 확인</button><button name="action" value="connect">Telegram 연결 테스트</button><button name="action" value="enable">장전 WATCH 활성화</button><button name="action" value="disable">장전 WATCH 중지</button><button name="action" value="run">현재 시간 점검</button></form>'''

    @app.post('/preopen-watch/control',response_class=HTMLResponse)
    async def control(request:Request):
        body=await request.body()
        if len(body)>2048: raise HTTPException(400,'Request too large')
        fields=parse_qs(body.decode());auth(fields.get('secret',[''])[0]);action=fields.get('action',['status'])[0];now=utc();store=storage()
        if action=='probe': result=probe(store,now)
        elif action=='connect':
            ident=PREFIX+'connect:'+now.astimezone(ET).date().isoformat()
            with store.transaction() as tx:
                tx.enqueue(ident,dict(text='Market Forge 장전 스캐너 연결 테스트\n매 거래일 07:20 MT 연구용 WATCHLIST\n실제 진입/주문 아님 · 무료 IEX\nID: '+ident,expires_at=(now+timedelta(minutes=2)).isoformat()))
            result=dict(delivery=drain(store,send,prefix=ident,now=now))
        elif action in ('enable','disable'):
            with store.transaction() as tx:
                if action=='enable':
                    p=tx.get(PREFIX+'probe',{})
                    if p.get('config_hash')!=config_hash(config()) or (now-stamp(p['at'])).total_seconds()>86400 or p.get('counts',{}).get('DATA_PRESENT',0)==0:
                        raise HTTPException(409,'Verify free data first')
                    sent=tx.execute("SELECT COUNT(*) FROM mf_outbox WHERE id LIKE ? AND status='sent'",(PREFIX+'connect:%',)).fetchone()[0]
                    if not sent: raise HTTPException(409,'Verify Telegram first')
                result=dict(enabled=action=='enable',updated_at=now.isoformat(),mode='RESEARCH_WATCH_ONLY',expires_at=None)
                tx.put(PREFIX+'control',result)
        elif action=='run': result=worker(store,send,now)
        elif action=='status':
            with store.transaction() as tx:
                day=now.astimezone(ET).date().isoformat()
                result={k:tx.get(PREFIX+k) for k in ('control','status','probe')}
                result.update(config=config(),report=tx.get(PREFIX+day+':report'),delivery=tx.get(PREFIX+day+':delivery'))
        else: raise HTTPException(400,'Unknown action')
        return '<meta charset="utf-8"><pre>'+html.escape(json.dumps(result,ensure_ascii=False,indent=2))+'</pre><a href="/preopen-watch">Back</a>'

    @app.post('/preopen-watch/worker')
    def tick(data:dict):
        auth(data.get('secret'));return worker(storage(),send,utc())
