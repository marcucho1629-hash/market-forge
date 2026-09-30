"""Date-limited 20-symbol EARLY experiment, isolated from the confirmed relay."""
import hashlib,json
from dataclasses import asdict
from datetime import date,timedelta
from .core import stamp,finite,ET
from .calendar import current_session
from .live_signals import DENVER
from .engine_relay import normalize as relay_normalize
from .early_lifecycle import State,advance
from .store import drain

VERSION='MF_V13_32'
BANKS={1:'SPY QQQ NVDA TSLA AVGO AMD META AAPL MSFT AMZN'.split(),2:'PLTR PANW SNDK GOOG RDDT HOOD COIN JPM AXP V'.split()}

def active(cfg,now):
    session,opened=current_session(now)
    return cfg.get('enabled') is True and cfg.get('test_date')==session['date'] and opened

def control(store,enabled,test_date,now):
    if type(enabled) is not bool:raise ValueError('boolean required')
    day=date.fromisoformat(test_date)
    if enabled and not 0<=(day-now.astimezone(ET).date()).days<=7:raise ValueError('test date must be within seven days')
    if current_session(now)[1]:raise ValueError('change test control outside market hours')
    # current_session evaluates an exchange calendar, including holidays.
    check=now.astimezone(ET).replace(year=day.year,month=day.month,day=day.day,hour=12,minute=0)
    if enabled and not current_session(check)[0].get('is_open'):raise ValueError('test date must be a trading day')
    with store.transaction() as tx:
        if tx.execute("SELECT id FROM mf_outbox WHERE status='attempting' LIMIT 1").fetchone():raise ValueError('reconcile in-flight delivery first')
        if enabled:
            for prefix in ('mf130','mf131'):
                old=tx.get(prefix+'-control',{})
                tx.put(prefix+'-control',{**old,'enabled':False,'updated_at':now.isoformat()})
                tx.execute("UPDATE mf_outbox SET status='expired' WHERE status='pending' AND id LIKE ?",(prefix+':%',))
        if not enabled:tx.execute("UPDATE mf_outbox SET status='expired' WHERE status='pending' AND id LIKE ?",('mf132:%',))
        value={'enabled':enabled,'version':VERSION,'test_date':test_date,'symbols':sum(BANKS.values(),[]),'updated_at':now.isoformat(),'restore_legacy':False}
        tx.put('mf132-control',value)
    return value

def status(store,now):
    with store.transaction() as tx:
        cfg=tx.get('mf132-control',{})
        return {'version':VERSION,'control':cfg,'sending_now':active(cfg,now),'banks':{str(i):tx.get('mf132-bank:'+str(i)) for i in BANKS},'v131_enabled':tx.get('mf131-control',{}).get('enabled',False),'v130_enabled':tx.get('mf130-control',{}).get('enabled',False)}

def normalize(data,now):
    if data.get('source')!=VERSION or data.get('schema_version')!=4:raise ValueError('unsupported early test')
    if type(data.get('bank')) is not int or data['bank'] not in BANKS:raise ValueError('only two banks authorized')
    kind,rows=relay_normalize({**data,'source':'MF_V13_31','schema_version':3},now)
    for r in rows:
        if r['ticker'] not in BANKS[data['bank']]:raise ValueError('ticker outside test bank')
        for field in ('early_mc_raw','early_scmp_raw'):
            if type(r.get(field)) is not bool:raise ValueError('early flags required')
        for field in ('early_long_distance_atr','early_short_distance_atr'):r[field]=finite(r[field])
    return kind,rows

def state_read(value):
    value=dict(value)
    for k in ('last_observed','first_early'):
        if value.get(k):value[k]=stamp(value[k])
    return State(**value)

def state_write(state):
    return {k:v.isoformat() if hasattr(v,'isoformat') else v for k,v in asdict(state).items()}

def ingest(store,data,now):
    kind,rows=normalize(data,now);day=now.astimezone(ET).date().isoformat();decisions=[]
    with store.transaction() as tx:
        live=active(tx.get('mf132-control',{}),now)
        namespace='mf132-state' if live else 'mf132-shadow'
        for r in rows:
            rowevents=[];trackkey=f'{namespace}-active:{day}:{r["ticker"]}';tracked=tx.get(trackkey,{})
            # A failed/expired entry is not a delivered candidate; ambiguous sends
            # remain blocked until a close or chart exit resolves this lifecycle.
            if tracked:
                sent=tx.execute('SELECT status FROM mf_outbox WHERE id=?',(tracked['message_id'],)).fetchone()
                if live and sent and sent[0]=='expired':tracked={};tx.put(trackkey,{})
            def emit(identity,action,label,detail,parent=None):
                msgid=identity+':'+action
                if parent:
                    p=tx.execute('SELECT status FROM mf_outbox WHERE id=?',(parent,)).fetchone()
                    if not p or p[0]!='sent':
                        rowevents.append({'action':action,'result':'parent_not_sent','id':msgid});return msgid
                at=stamp(r['observed_at']).astimezone(DENVER).strftime('%H:%M:%S')
                text=f'TEST · {r["ticker"]} {label}\n${r["price"]:.2f} · {detail}\n감지 {at} 덴버'
                payload={'text':text,'ticker':r['ticker'],'action':action,'detected_at':r['observed_at'],'received_at':now.isoformat(),'expires_at':(stamp(r['observed_at'])+timedelta(seconds=30)).isoformat(),'test_date':day}
                if live:tx.enqueue(msgid,payload)
                tx.event(msgid+(':live' if live else ':shadow'),day,{'source':'mf132_event','id':msgid,**payload,'live':live})
                rowevents.append({'action':action,'result':'queued' if live else 'shadow','id':msgid})
                return msgid
            # Chart X remains identifiable; no invented intrabar stop or trade order.
            if r['chart_exit'] and tracked and r['chart_exit_side']==(1 if tracked['code']=='MC' else -1):
                xid='mf132:'+hashlib.sha256((r['feed']+r['bar_start']+r['chart_exit']).encode()).hexdigest()
                emit(xid,'CHART_X','X','차트 '+r['chart_exit']+' · 실제 매매 청산 아님',tracked['message_id'])
                tracked={};tx.put(trackkey,{})
            for code,raw,dist,flag in [('MC','early_mc_raw','early_long_distance_atr','chart_mc'),('SCMP','early_scmp_raw','early_short_distance_atr','chart_scmp')]:
                identity='mf132:'+hashlib.sha256((r['feed']+'|5|'+r['bar_start']+'|'+code).encode()).hexdigest();key=namespace+':'+identity
                prior=state_read(tx.get(key,{}))
                candidate=r[raw] and not (r['early_mc_raw'] and r['early_scmp_raw'])
                if tracked and tracked.get('identity')!=identity:candidate=False
                state,event=advance(prior,observed=stamp(r['observed_at']),received=now,bar_start=stamp(r['bar_start']),bar_end=stamp(r['bar_end']),raw_candidate=candidate,distance_atr=r[dist],confirmed_flag=r[flag] if kind=='closed' else None)
                tx.put(key,state_write(state))
                if event=='EARLY_ENTRY':
                    mid=emit(identity,event,'EARLY '+code,'미확정 · 조기 후보')
                    tracked={'identity':identity,'message_id':mid,'code':code};tx.put(trackkey,tracked)
                elif event in ('CONFIRM_EARLY','INVALIDATE_EARLY'):
                    if tracked.get('identity')==identity:
                        if event=='INVALIDATE_EARLY':
                            tx.execute("UPDATE mf_outbox SET status='expired' WHERE id=? AND status='pending'",(tracked['message_id'],))
                        emit(identity,event,('확정 ' if event=='CONFIRM_EARLY' else '후보 취소 ')+code,'5분봉 확정 · 새 진입 아님' if event=='CONFIRM_EARLY' else '마감 시 조건 불충족 · 손절 주문 아님',tracked['message_id'])
                        if event=='INVALIDATE_EARLY':tracked={};tx.put(trackkey,{})
                elif event=='CONFIRMED_ENTRY':
                    # Comparison only: never present a missed EARLY as a fresh entry.
                    rowevents.append({'action':'CONFIRMED_ONLY','code':code,'result':'log_only','id':identity})
            decisions.append({'ticker':r['ticker'],'observation':r,'events':rowevents})
        digest=hashlib.sha256((json.dumps(data,sort_keys=True)+now.isoformat()).encode()).hexdigest()
        tx.event('mf132-observation:'+digest,day,{'source':'mf132','kind':kind,'received_at':now.isoformat(),'decisions':decisions})
        tx.put('mf132-bank:'+str(data['bank']),{'received_at':now.isoformat(),'kind':kind,'tickers':[r['ticker'] for r in rows]})
    return {'mode':'live_test' if live else 'shadow','decisions':decisions}

def delivery(store,send,now,clock=None):
    with store.transaction() as tx:
        if not active(tx.get('mf132-control',{}),now):
            tx.execute("UPDATE mf_outbox SET status='expired' WHERE status='pending' AND id LIKE ?",('mf132:%',))
            return []
    results=drain(store,send,limit=40,prefix='mf132:',clock=clock)
    with store.transaction() as tx:
        for r in results:tx.event('mf132-delivery:'+r['id'],now.astimezone(ET).date().isoformat(),{'source':'mf132_delivery',**r})
    return results
