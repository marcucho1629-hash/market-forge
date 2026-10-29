"""Isolated paper signals from validated TradingView final 1m OHLCV."""
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from .engine import Bar, Engine, Config, SYMBOLS, ET, VERSION as ENGINE_VERSION
from ..calendar import schedule
from ..store import drain

VERSION = 'MF_V136_RESEARCH_LIVE_1'
PREFIX = 'research139recovery:'
ENTRY_MAX_AGE = 45
CONTROL_KEY = 'research136live:control'  # Keep the existing explicit activation/expiry.
BANKS = {1: SYMBOLS[:10], 2: SYMBOLS[10:]}
DENVER = ZoneInfo('America/Denver')

def dt(ms):
    if type(ms) is not int: raise ValueError('integer epoch milliseconds required')
    return datetime.fromtimestamp(ms / 1000, timezone.utc)

def enabled(tx, now):
    c = tx.get(CONTROL_KEY, {})
    return c.get('enabled') is True and now < datetime.fromisoformat(c['expires_at'])

def queue(tx, identity, text, now, expires_at=None):
    tx.enqueue(identity, {'text': text, 'expires_at': (expires_at or now+timedelta(seconds=90)).isoformat()})

def prior_seed(tx, symbol, day):
    # Freeze a validated prior-session seed per day, so replays cannot change mid-session.
    target = datetime.fromisoformat(day).date()
    for back in range(1, 11):
        prior = (target-timedelta(days=back)).isoformat()
        for prefix in (PREFIX, 'research138sequence:', 'research137aligned:'):
            rows = tx.get(prefix+prior+':'+symbol, {}).get('bars', [])
            if len(rows) < 90:
                continue
            try:
                bars = [Bar(symbol, dt(r[1]), *r[2:]) for r in rows if r[0] == symbol]
                check = Engine(); check.seed(bars, allow_gaps=True)
                if len(bars) == len(rows) and bars[-1].end.astimezone(ET).date().isoformat() == prior:
                    return rows
            except (ValueError, TypeError, IndexError):
                pass
    return []

def ingest(store, data, now, calendar=schedule):
    if data.get('source') != VERSION or type(data.get('bank')) is not int or data['bank'] not in BANKS:
        raise ValueError('wrong producer')
    if not 0 <= (now-dt(data['emitted'])).total_seconds() <= 45:
        raise ValueError('stale envelope')
    rows = data.get('bars')
    if not isinstance(rows,list) or len(rows)>50: raise ValueError('invalid bars')
    day = now.astimezone(ET).date().isoformat()
    session = calendar(day)
    if not session['is_open']:
        with store.transaction() as tx: tx.put(PREFIX+'bank:'+str(data['bank']),{'received_at':now.isoformat(),'day':day,'mode':'closed_heartbeat'})
        return {'accepted':0, 'reason':'market_closed'}
    opened, closed = datetime.fromisoformat(session['open']),datetime.fromisoformat(session['close'])
    if not opened <= now <= closed+timedelta(minutes=2):
        with store.transaction() as tx: tx.put(PREFIX+'bank:'+str(data['bank']),{'received_at':now.isoformat(),'day':day,'mode':'outside_session_heartbeat','session_open':session['open']})
        return {'accepted':0,'reason':'outside_session'}
    parsed = {}
    for r in rows:
        if not isinstance(r,list) or len(r)!=7 or r[0] not in BANKS[data['bank']]: raise ValueError('wrong bar shape or bank')
        end = dt(r[1])
        if not opened < end <= closed or end > now or (now-end).total_seconds()>360: raise ValueError('invalid bar time')
        if any(type(x) not in (int,float) for x in r[2:]): raise ValueError('invalid numeric values')
        bar = Bar(r[0],end,*r[2:]);bar.validate()
        ident=(r[0],r[1])
        if ident in parsed and parsed[ident]!=r: raise ValueError('conflicting batch')
        parsed[ident]=r
    by_symbol={symbol:[] for symbol in BANKS[data['bank']]}
    for r in parsed.values(): by_symbol[r[0]].append(r)
    accepted=0
    with store.transaction() as tx:
        active=enabled(tx,now)
        for symbol, new in by_symbol.items():
            if not new: continue
            key=PREFIX+day+':'+symbol
            state=tx.get(key,{'bars':[], 'processed':0, 'sent_entry':None})
            if 'seed_bars' not in state:
                state['seed_bars'] = prior_seed(tx, symbol, day)
                state['seed_count'] = len(state['seed_bars'])
                state['warmup_mode'] = 'prior_session' if state['seed_bars'] else 'cold_start'
            saved={r[1]:r for r in state['bars']}
            conflict=False
            for r in new:
                if r[1] in saved and saved[r[1]]!=r: conflict=True
                elif r[1] not in saved:
                    saved[r[1]]=r; accepted+=1
                    if r[1] <= state['processed']:
                        state.setdefault('ignored_backfills',[]).append(r[1])
            state['bars']=sorted(saved.values(),key=lambda r:r[1])
            state['received_at']=now.isoformat()
            if conflict: state['halted']='conflicting revision of final bar'
            first=int((opened+timedelta(minutes=1)).timestamp()*1000)
            continuous=[r for r in state['bars'] if r[1] not in state.get('ignored_backfills',[])]
            expected=first
            gaps=[]
            for r in continuous:
                if r[1] > expected:
                    gaps.append({'from_ms':expected,'to_ms':r[1]-60000,'resumed_ms':r[1]})
                expected=r[1]+60000
            state['gaps']=gaps
            state['missing_end']=gaps[0]['from_ms'] if gaps else None
            state['data_quality']='partial_minutes' if gaps else 'complete'
            # Reconstruct from persisted raw bars: no untrusted pickle, no in-memory dependency.
            cfg=Config(entry_cutoff_et=min(Config().entry_cutoff_et,(closed-timedelta(minutes=30)).astimezone(ET).time()),
                       flat_alert_et=min(Config().flat_alert_et,(closed-timedelta(minutes=15)).astimezone(ET).time()))
            engine=Engine(cfg)
            engine.seed([Bar(symbol,dt(r[1]),*r[2:]) for r in state['seed_bars']], allow_gaps=True)
            expected=first
            for r in continuous:
                if r[1] != expected:
                    engine.recover_gap(Bar(symbol,dt(r[1]),*r[2:]))
                expected=r[1]+60000
                events=engine.on_bar(Bar(symbol,dt(r[1]),*r[2:]))
                if r[1]<=state['processed']: continue
                for e in events:
                    event=asdict(e);event['time']=e.time.isoformat()
                    eid=f'{PREFIX}{day}:{symbol}:{e.episode}:{e.action}:{r[1]}'
                    age=(now-e.time).total_seconds()
                    is_entry=e.action in ('MC','SCMP')
                    fresh=0 <= age <= (ENTRY_MAX_AGE if is_entry else 90)
                    event.update(source=VERSION,engine=ENGINE_VERSION,feed='BATS:'+symbol,received_at=now.isoformat(),replay_only=not fresh,bar_to_receive_sec=age,data_quality=state['data_quality'],delivery_decision=('eligible' if active and fresh and not state.get('halted') else 'stale' if not fresh else 'inactive_or_halted'))
                    tx.event(eid,day,event)
                    if active and fresh and not state.get('halted'):
                        # Never announce exits/continuations for an entry that was not queued live.
                        if e.action in ('MC','SCMP'): state['sent_entry']=e.episode
                        entry_sent=e.action in ('MC','SCMP') or tx.execute("SELECT id FROM mf_outbox WHERE status='sent' AND id LIKE ? LIMIT 1",(f'{PREFIX}{day}:{symbol}:{e.episode}:' + ('MC' if e.side==1 else 'SCMP') + ':%',)).fetchone()
                        if state.get('sent_entry')==e.episode and entry_sent:
                            detail = '1·2·3분 신호 일치 · 3분봉 확정' if e.action in ('MC','SCMP') else f'{e.timeframe}분봉 종료/추세 관리'
                            quality=' · 일부 1분봉 누락 후 재개' if gaps else ''
                            text=(f'DAY TEST {symbol} {e.action}\n${e.price:.2f} · {detail} · BATS{quality}\n'
                                  f'봉 마감 {e.time.astimezone(DENVER):%Y-%m-%d %H:%M:%S} 덴버\n'
                                  f'연구용 신호 · 주문 아님\nID {day}-{symbol}-{e.episode}')
                            queue(tx,eid,text,now,expires_at=e.time+timedelta(seconds=ENTRY_MAX_AGE) if is_entry else None)
                state['processed']=r[1]
            state['position']=asdict(engine.states[symbol].position) if symbol in engine.states and engine.states[symbol].position else None
            if state['position']: state['position']['entered']=state['position']['entered'].isoformat()
            if active and state.get('halted'):
                queue(tx,f'{PREFIX}{day}:{symbol}:DATA_CONFLICT',f'DAY TEST {symbol} 확정봉 충돌: 신규 신호 중지',now)
            if active and gaps and state.get('notified_gaps',0)<len(gaps):
                last=gaps[-1]
                queue(tx,f'{PREFIX}{day}:{symbol}:DATA_GAP:{last["from_ms"]}',
                      f'DAY TEST {symbol} 데이터 누락 감지\n누락 가격을 보충하지 않고 새 완성봉부터 재개합니다. 누락 구간 판단은 불가합니다.',now)
                state['notified_gaps']=len(gaps)
            tx.put(key,state)
        tx.put(PREFIX+'bank:'+str(data['bank']),{'received_at':now.isoformat(),'day':day,'accepted':accepted,'submitted':len(rows)})
    return {'source':VERSION,'accepted':accepted,'bank':data['bank']}

def delivery(store,send,now):
    with store.transaction() as tx:
        active=enabled(tx,now)
        if not active:
            tx.execute("UPDATE mf_outbox SET status='expired' WHERE status='pending' AND id LIKE ?",(PREFIX+'%',))
    return drain(store,send,limit=30,prefix=PREFIX,now=None) if active else []

def control(store,on,now):
    if type(on) is not bool: raise ValueError('boolean required')
    with store.transaction() as tx:
        cfg={'enabled':on,'expires_at':(now+timedelta(days=7)).isoformat(),'updated_at':now.isoformat(),'source':VERSION}
        tx.put(CONTROL_KEY,cfg)
        if on:
            queue(tx,PREFIX+'setup:'+now.isoformat(),
                  'DAY TEST 수신 경로 활성화\n20종목 · 1·2·3분 MC/SCMP 신호 일치 후 3분봉 마감 진입\n종료/보호 신호 별도 · 7일 테스트 · 주문 없음',now)
        else: tx.execute("UPDATE mf_outbox SET status='expired' WHERE status='pending' AND id LIKE ?",(PREFIX+'%',))
        return cfg

def status(store,now):
    day=now.astimezone(ET).date().isoformat()
    with store.transaction() as tx:
        states={}
        for symbol in SYMBOLS:
            s=tx.get(PREFIX+day+':'+symbol,{})
            states[symbol]={k:v for k,v in s.items() if k not in ('bars','seed_bars')} | {'bar_count':len(s.get('bars',[]))}
        outbox=tx.execute('SELECT status,COUNT(*) FROM mf_outbox WHERE id LIKE ? GROUP BY status',(PREFIX+'%',)).fetchall()
        return {'source':VERSION,'engine':ENGINE_VERSION,'control':tx.get(CONTROL_KEY,{}),'outbox':dict(outbox),
                'banks':{i:tx.get(PREFIX+'bank:'+str(i),{}) for i in BANKS},'symbols':states,
                'events':[e for e in tx.events(day) if e.get('engine')==ENGINE_VERSION],
                'deliveries':[{'id':r[0],'status':r[1]} for r in tx.execute('SELECT id,status FROM mf_outbox WHERE id LIKE ?',(PREFIX+day+':%',)).fetchall()]}


def diagnostic_export(store, now):
    day=now.astimezone(ET).date().isoformat()
    with store.transaction() as tx:
        result={}
        for symbol in SYMBOLS:
            for prefix in (PREFIX,'research138sequence:','research137aligned:'):
                value=tx.get(prefix+day+':'+symbol,{})
                if value.get('bars'):
                    result[symbol]={'prefix':prefix,'bars':value['bars'],'seed_bars':value.get('seed_bars',[]),'ignored_backfills':value.get('ignored_backfills',[])}
                    break
        return {'day':day,'symbols':result,'events':tx.events(day),
                'deliveries':[{'id':r[0],'status':r[1]} for r in tx.execute('SELECT id,status FROM mf_outbox WHERE id LIKE ? OR id LIKE ?',(PREFIX+day+':%','research138sequence:'+day+':%')).fetchall()]}
