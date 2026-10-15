"""V135 closed-bar Telegram path. Requires explicit enable after validation."""
import json,hashlib
from datetime import timedelta
from .router import VERSION,parse,normalize,process,expire
from ..research.adapter import BANKS
from ..calendar import current_session
from ..core import ET
from ..live_signals import DENVER
from ..store import drain
PREFIX='mf135:'
def key(day,ticker):return PREFIX+day+':state:'+ticker

def notify(tx,day,ticker,e,now,enabled):
    eid=PREFIX+e['candidate_id']+':'+e['action']+':'+e['bar_end']
    tx.event(eid,day,dict(source=VERSION,ticker=ticker,event=e,received_at=now.isoformat()))
    if e['action'] in ('CONFIRM','UNCONFIRMED','CLOSE_MISSING'):
        tx.execute("UPDATE mf_outbox SET status='expired' WHERE status='pending' AND id LIKE ?",(PREFIX+e['candidate_id']+':EARLY:%',))
    if not enabled:return
    parent_action='EARLY' if e['action'] in ('UNCONFIRMED','CLOSE_MISSING') else 'CONFIRM' if e['action']=='X' else None
    if parent_action and not tx.execute("SELECT id FROM mf_outbox WHERE status='sent' AND id LIKE ? LIMIT 1",(PREFIX+e['candidate_id']+':'+parent_action+':%',)).fetchone():return
    code='MC' if e['side']==1 else 'SCMP'
    label={'EARLY':'⚡ EARLY '+code,'CONFIRM':'✅ CONFIRM '+code,'UNCONFIRMED':'✖ '+code+' 후보 미확정 종료','CLOSE_MISSING':'⌛ '+code+' 마감 데이터 미수신','X':'✖ X · '+('상승' if e['side']==1 else '하락')+' 추세 종료'}[e['action']]
    suffix='1분봉 확정 · 5분봉 미확정' if e['action']=='EARLY' else '5분봉 확정' if e['action'] in ('CONFIRM','X') else '새 진입 아님'
    text=f'TEST {ticker} {label}\n${e["price"]:.2f} · {suffix}\n봉 마감 {parse(e["bar_end"]).astimezone(DENVER):%H:%M:%S} 덴버'
    deadline=now+timedelta(seconds=60)
    if e['action']=='EARLY':
        deadline=min(parse(e['bar_end'])+timedelta(seconds=60),parse(e['bucket_end']))
        if now>=deadline:return
    tx.enqueue(eid,dict(text=text,expires_at=deadline.isoformat(),candidate_id=e['candidate_id'],action=e['action']))

def ingest(store,data,now):
    if data.get('source')!=VERSION or data.get('schema_version')!=7 or type(data.get('bank')) is not int or data['bank'] not in BANKS:raise ValueError('wrong producer')
    if not 0<=(now-parse(data['emitted_at'])).total_seconds()<=30:raise ValueError('stale envelope')
    rows=data.get('observations')
    if not isinstance(rows,list) or len(rows)>20:raise ValueError('invalid rows')
    session,opened=current_session(now);day=now.astimezone(ET).date().isoformat()
    if not session['is_open']:return dict(version=VERSION,accepted=0,rejected=['market_closed'])
    valid=[];rejected=[]
    for i,r in enumerate(rows):
        try:
            if r['ticker'] not in BANKS[data['bank']] or r['feed']!='BATS:'+r['ticker']:raise ValueError('wrong ticker/feed')
            valid.append(normalize(r,now,session))
        except (KeyError,TypeError,ValueError,OverflowError) as ex:rejected.append(dict(index=i,reason=str(ex)))
    valid.sort(key=lambda r:(r['bar_end'],r['timeframe']))
    with store.transaction() as tx:
        enabled=tx.get('mf135-control',{}).get('enabled') is True
        for r in valid:
            rk=key(day,r['ticker']);s=tx.get(rk,{})
            eventkey=r['timeframe']+':'+r['bar_start']
            seen=s.setdefault('seen',[])
            if eventkey in seen:continue
            # Do not let late previous bars rewind current state.
            latest=s.setdefault('latest',{}).get(r['timeframe'])
            if latest and r['bar_start']<latest:continue
            seen.append(eventkey);s['latest'][r['timeframe']]=r['bar_start']
            for e in process(s,r):notify(tx,day,r['ticker'],e,now,enabled)
            tx.put(rk,s)
        tx.put(PREFIX+'bank:'+str(data['bank']),dict(received_at=now.isoformat(),rows=len(valid),rejected=rejected))
    return dict(version=VERSION,accepted=len(valid),rejected=rejected)

def delivery(store,send,now):
    day=now.astimezone(ET).date().isoformat()
    with store.transaction() as tx:
        enabled=tx.get('mf135-control',{}).get('enabled') is True
        for ticker in sum(BANKS.values(),[]):
            rk=key(day,ticker);s=tx.get(rk,{})
            for e in expire(s,now):notify(tx,day,ticker,e,now,enabled)
            if s:tx.put(rk,s)
    return drain(store,send,limit=40,prefix=PREFIX,now=now) if enabled else []

def control(store,enabled,now):
    if type(enabled) is not bool:raise ValueError('boolean required')
    with store.transaction() as tx:
        if tx.execute("SELECT id FROM mf_outbox WHERE status='attempting' LIMIT 1").fetchone():raise ValueError('reconcile in-flight delivery')
        if enabled:
            for v in ('mf130','mf131','mf132','mf133','mf134'):
                tx.put(v+'-control',{**tx.get(v+'-control',{}),'enabled':False})
                tx.execute("UPDATE mf_outbox SET status='expired' WHERE status='pending' AND id LIKE ?",(v+':%',))
        else:tx.execute("UPDATE mf_outbox SET status='expired' WHERE status='pending' AND id LIKE 'mf135:%'")
        cfg=dict(enabled=enabled,version=VERSION,symbols=sum(BANKS.values(),[]),schedule='every_trading_day',opening_minutes=30,updated_at=now.isoformat(),restore_legacy=False)
        tx.put('mf135-control',cfg)
    return cfg

def status(store,now):
    day=now.astimezone(ET).date().isoformat()
    with store.transaction() as tx:
        return dict(version=VERSION,control=tx.get('mf135-control',{}),banks={str(i):tx.get(PREFIX+'bank:'+str(i),{}) for i in BANKS},events=[e for e in tx.events(day) if e.get('source')==VERSION])
