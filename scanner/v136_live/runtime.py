"""Isolated paper signals from validated TradingView final 1m OHLCV."""
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from .engine import Bar, Engine, Config, SYMBOLS, ET
from ..calendar import schedule
from ..store import drain

VERSION = 'MF_V136_RESEARCH_LIVE_1'
PREFIX = 'research136live:'
BANKS = {1: SYMBOLS[:10], 2: SYMBOLS[10:]}
DENVER = ZoneInfo('America/Denver')

def dt(ms):
    if type(ms) is not int: raise ValueError('integer epoch milliseconds required')
    return datetime.fromtimestamp(ms / 1000, timezone.utc)

def enabled(tx, now):
    c = tx.get(PREFIX+'control', {})
    return c.get('enabled') is True and now < datetime.fromisoformat(c['expires_at'])

def queue(tx, identity, text, now):
    tx.enqueue(identity, {'text': text, 'expires_at': (now+timedelta(seconds=90)).isoformat()})

def ingest(store, data, now, calendar=schedule):
    if data.get('source') != VERSION or type(data.get('bank')) is not int or data['bank'] not in BANKS:
        raise ValueError('wrong producer')
    if not 0 <= (now-dt(data['emitted'])).total_seconds() <= 45:
        raise ValueError('stale envelope')
    rows = data.get('bars')
    if not isinstance(rows,list) or len(rows)>50: raise ValueError('invalid bars')
    day = now.astimezone(ET).date().isoformat()
    session = calendar(day)
    if not session['is_open']: return {'accepted':0, 'reason':'market_closed'}
    opened, closed = datetime.fromisoformat(session['open']),datetime.fromisoformat(session['close'])
    if not opened <= now <= closed+timedelta(minutes=2): return {'accepted':0,'reason':'outside_session'}
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
            saved={r[1]:r for r in state['bars']}
            conflict=False
            for r in new:
                if r[1] in saved and saved[r[1]]!=r: conflict=True
                elif r[1] not in saved: saved[r[1]]=r; accepted+=1
            state['bars']=sorted(saved.values(),key=lambda r:r[1])
            state['received_at']=now.isoformat()
            if conflict: state['halted']='conflicting revision of final bar'
            first=int((opened+timedelta(minutes=1)).timestamp()*1000)
            continuous=[]
            expected=first
            for r in state['bars']:
                if r[1]!=expected: break
                continuous.append(r);expected+=60000
            missing=expected if len(continuous)<len(state['bars']) else None
            state['missing_end']=missing
            # Reconstruct from persisted raw bars: no untrusted pickle, no in-memory dependency.
            cfg=Config(entry_cutoff_et=min(Config().entry_cutoff_et,(closed-timedelta(minutes=30)).astimezone(ET).time()),
                       flat_alert_et=min(Config().flat_alert_et,(closed-timedelta(minutes=15)).astimezone(ET).time()))
            engine=Engine(cfg)
            for r in continuous:
                events=engine.on_bar(Bar(symbol,dt(r[1]),*r[2:]))
                if r[1]<=state['processed']: continue
                for e in events:
                    event=asdict(e);event['time']=e.time.isoformat()
                    eid=f'{PREFIX}{day}:{symbol}:{e.episode}:{e.action}:{r[1]}'
                    fresh=0 <= (now-e.time).total_seconds() <= 90
                    event.update(source=VERSION,feed='BATS:'+symbol,received_at=now.isoformat(),replay_only=not fresh)
                    tx.event(eid,day,event)
                    if active and fresh and not state.get('halted'):
                        # Never announce exits/continuations for an entry that was not queued live.
                        if e.action in ('MC','SCMP'): state['sent_entry']=e.episode
                        entry_sent=e.action in ('MC','SCMP') or tx.execute("SELECT id FROM mf_outbox WHERE status='sent' AND id LIKE ? LIMIT 1",(f'{PREFIX}{day}:{symbol}:{e.episode}:' + ('MC' if e.side==1 else 'SCMP') + ':%',)).fetchone()
                        if state.get('sent_entry')==e.episode and entry_sent:
                            text=(f'NEW TEST {symbol} {e.action}\n${e.price:.2f} · 확정 {e.timeframe}분봉 · BATS\n'
                                  f'봉 마감 {e.time.astimezone(DENVER):%Y-%m-%d %H:%M:%S} 덴버\n'
                                  f'연구용 신호 · 주문 아님\nID {day}-{symbol}-{e.episode}')
                            queue(tx,eid,text,now)
                state['processed']=r[1]
            state['position']=asdict(engine.states[symbol].position) if symbol in engine.states and engine.states[symbol].position else None
            if state['position']: state['position']['entered']=state['position']['entered'].isoformat()
            if active and (state.get('halted') or (missing and (now-dt(missing)).total_seconds()>90)):
                queue(tx,f'{PREFIX}{day}:{symbol}:DATA_GAP',f'NEW TEST {symbol} 데이터 누락/충돌\n계산 중단: 확정 1분봉 복구 필요. 청산 신호가 아닙니다.',now)
            tx.put(key,state)
        tx.put(PREFIX+'bank:'+str(data['bank']),{'received_at':now.isoformat(),'day':day,'accepted':accepted,'submitted':len(rows)})
    return {'source':VERSION,'accepted':accepted,'bank':data['bank']}

def delivery(store,send,now):
    with store.transaction() as tx:
        active=enabled(tx,now)
        if not active:
            tx.execute("UPDATE mf_outbox SET status='expired' WHERE status='pending' AND id LIKE ?",(PREFIX+'%',))
    return drain(store,send,limit=30,prefix=PREFIX,now=now) if active else []

def control(store,on,now):
    if type(on) is not bool: raise ValueError('boolean required')
    with store.transaction() as tx:
        cfg={'enabled':on,'expires_at':(now+timedelta(days=7)).isoformat(),'updated_at':now.isoformat(),'source':VERSION}
        tx.put(PREFIX+'control',cfg)
        if on:
            queue(tx,PREFIX+'setup:'+now.isoformat(),
                  'NEW TEST 새 연구 엔진 수신 경로 활성화\n20종목 · 10+10 뱅크 · 확정 1분봉 입력\n실제 데이터 수신은 TradingView 알림 시작 후 확인됩니다.\n7일 테스트 · 주문 없음',now)
        else: tx.execute("UPDATE mf_outbox SET status='expired' WHERE status='pending' AND id LIKE ?",(PREFIX+'%',))
        return cfg

def status(store,now):
    day=now.astimezone(ET).date().isoformat()
    with store.transaction() as tx:
        states={}
        for symbol in SYMBOLS:
            s=tx.get(PREFIX+day+':'+symbol,{})
            states[symbol]={k:v for k,v in s.items() if k!='bars'} | {'bar_count':len(s.get('bars',[]))}
        return {'source':VERSION,'control':tx.get(PREFIX+'control',{}),
                'banks':{i:tx.get(PREFIX+'bank:'+str(i),{}) for i in BANKS},'symbols':states,
                'events':[e for e in tx.events(day) if e.get('source')==VERSION]}
