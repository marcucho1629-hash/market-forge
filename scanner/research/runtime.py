"""PostgreSQL-backed research lifecycle and gated test Telegram outbox."""
import json,hashlib
from dataclasses import asdict
from datetime import datetime,timedelta
from .adapter import adapt,BANKS
from .collector import Collector
from .store import restore_state,dump
from ..calendar import current_session
from ..core import ET
from ..store import drain
from ..live_signals import DENVER
PREFIX='mf134:'
VERSION='MF_V13_34_SHADOW'

def control(store,enabled,now):
 if type(enabled) is not bool:raise ValueError('boolean required')
 with store.transaction() as tx:
  if tx.execute("SELECT id FROM mf_outbox WHERE status='attempting' LIMIT 1").fetchone():raise ValueError('reconcile in-flight delivery')
  if enabled:
   for old in ('mf130','mf131','mf132','mf133'):
    tx.put(old+'-control',{**tx.get(old+'-control',{}),'enabled':False})
    tx.execute("UPDATE mf_outbox SET status='expired' WHERE status='pending' AND id LIKE ?",(old+':%',))
  else:tx.execute("UPDATE mf_outbox SET status='expired' WHERE status='pending' AND id LIKE 'mf134:%'")
  cfg=dict(enabled=enabled,version=VERSION,schedule='every_trading_day',symbols=sum(BANKS.values(),[]),updated_at=now.isoformat(),restore_legacy=False)
  tx.put('mf134-control',cfg)
 return cfg

def load(tx,day):
 c=Collector('v134-daily','core1-time15-45-exp60')
 for key,value in tx.execute('SELECT key,value FROM mf_kv WHERE key LIKE ?',(PREFIX+day+':state:%',)).fetchall():
  d=json.loads(value);s=restore_state(json.dumps(d['state']));k=(d['engine'],s.identity)
  c.states[k]=s;m=d['metadata'];m['source_bar_start']=datetime.fromisoformat(m['source_bar_start']);c.metadata[k]=m
 return c

def save(tx,c,day,now,live):
 for (engine,cid),s in c.states.items():
  tx.put(PREFIX+day+':state:'+cid,dict(engine=engine,state=json.loads(dump(asdict(s))),metadata=json.loads(dump(c.metadata[(engine,cid)]))))
  parent=PREFIX+cid+':SEND'
  if s.phase not in ('PREP','WOULD_SEND'):
   tx.execute("UPDATE mf_outbox SET status='expired' WHERE id=? AND status='pending'",(parent,))
 for e in c.events:
  cid=e['candidate_id'];s=c.states[(e['engine'],cid)];ticker=c.metadata[(e['engine'],cid)]['ticker']
  phase=e['to_phase'];code='MC' if s.side==1 else 'SCMP'
  tx.event(PREFIX+cid+':'+phase,day,{'source':'mf134','event':e,'received_at':now.isoformat()})
  if not live:continue
  parent=PREFIX+cid+':SEND'
  if phase=='WOULD_SEND':
   deadline=min(s.prep_at+timedelta(seconds=45),s.send_at+timedelta(seconds=20),s.bar_end)
   if now>=deadline:continue
   text=f'TEST ⚡ {ticker} EARLY {code}\n${s.signal_price:.2f} · 미확정\n감지 {s.send_at.astimezone(DENVER):%H:%M:%S} 덴버'
   tx.enqueue(parent,dict(text=text,expires_at=deadline.isoformat(),candidate_id=cid,action='SEND'))
  elif phase in ('CONFIRMED','INVALIDATED','EXPIRED_CLOSE_MISSING'):
   sent=tx.execute('SELECT status FROM mf_outbox WHERE id=?',(parent,)).fetchone()
   if not sent or sent[0]!='sent':continue
   label={'CONFIRMED':'✅ 봉 마감 확인','INVALIDATED':'✖ 후보 취소','EXPIRED_CLOSE_MISSING':'⌛ 마감 확인 누락'}[phase]
   text=f'TEST {ticker} {code} · {label}\n새 진입 아님\n처리 {now.astimezone(DENVER):%H:%M:%S} 덴버'
   tx.enqueue(PREFIX+cid+':'+phase,dict(text=text,expires_at=(now+timedelta(minutes=2)).isoformat(),candidate_id=cid,action=phase))

def ingest(store,data,now):
 out=adapt(data,now);day=now.astimezone(ET).date().isoformat()
 digest=hashlib.sha256(json.dumps(data,sort_keys=True,separators=(',',':')).encode()).hexdigest()
 receipt=PREFIX+'receipt:'+digest
 with store.transaction() as tx:
  if tx.get(receipt):return dict(duplicate=True,version=VERSION)
  cfg=tx.get('mf134-control',{});opened=current_session(now)[1]
  c=load(tx,day)
  for row in out['rows']:c.ingest(row)
  c.sweep(now);save(tx,c,day,now,cfg.get('enabled') is True and opened)
  tx.put(receipt,{'received_at':now.isoformat()})
  tx.put(PREFIX+'bank:'+str(out['bank']),dict(received_at=now.isoformat(),rows=len(out['rows']),rejected=out['rejected']))
 return dict(version=VERSION,mode='test' if cfg.get('enabled') else 'shadow',rows=len(out['rows']),rejected=out['rejected'])

def delivery(store,send,now):
 day=now.astimezone(ET).date().isoformat()
 with store.transaction() as tx:
  enabled=tx.get('mf134-control',{}).get('enabled') is True
  c=load(tx,day);c.sweep(now);save(tx,c,day,now,enabled and current_session(now)[1])
 if not enabled:return []
 return drain(store,send,limit=40,prefix=PREFIX,now=now)

def status(store,now):
 with store.transaction() as tx:
  return dict(version=VERSION,control=tx.get('mf134-control',{}),banks={str(i):tx.get(PREFIX+'bank:'+str(i),{}) for i in BANKS},candidates=load(tx,now.astimezone(ET).date().isoformat()).rows())
