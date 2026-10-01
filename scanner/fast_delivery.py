"""Durable Telegram bridge for V13.33; chart engine remains unchanged."""
from datetime import timedelta
from .core import ET, stamp
from .calendar import current_session
from .live_signals import DENVER
from .early_test import BANKS
from .store import drain

VERSION='MF_V13_33'

def active(cfg,now,entries=False):
    session,opened=current_session(now)
    if cfg.get('enabled') is not True or cfg.get('schedule')!='every_trading_day':return False
    return opened if entries else bool(session.get('is_open') and stamp(session['open'])<=now<=stamp(session['close'])+timedelta(minutes=3))

def control(store,enabled,now):
    if type(enabled) is not bool:raise ValueError('boolean required')
    with store.transaction() as tx:
        if tx.execute("SELECT id FROM mf_outbox WHERE status='attempting' LIMIT 1").fetchone():raise ValueError('reconcile in-flight delivery first')
        if enabled:
            for prefix in ('mf130','mf131','mf132'):
                tx.put(prefix+'-control',{**tx.get(prefix+'-control',{}),'enabled':False,'updated_at':now.isoformat()})
                tx.execute("UPDATE mf_outbox SET status='expired' WHERE status='pending' AND id LIKE ?",(prefix+':%',))
        else:tx.execute("UPDATE mf_outbox SET status='expired' WHERE status='pending' AND id LIKE ?",('mf133:%',))
        cfg=dict(enabled=enabled,version=VERSION,schedule='every_trading_day',symbols=sum(BANKS.values(),[]),updated_at=now.isoformat(),restore_legacy=False)
        tx.put('mf133-control',cfg)
    return cfg

def entry(tx,state,r,now):
    mid='mf133:'+state['identity']+':SEND'
    state['message_id']=mid
    code='MC' if state['side']==1 else 'SCMP'
    at=stamp(r['observed_at']).astimezone(DENVER).strftime('%H:%M:%S')
    text=f'TEST ⚡ {r["ticker"]} EARLY {code}\n${r["price"]:.2f} · 미확정\n감지 {at} 덴버'
    tx.enqueue(mid,dict(text=text,ticker=r['ticker'],action='SEND',detected_at=r['observed_at'],received_at=now.isoformat(),expires_at=min(now+timedelta(seconds=20),stamp(r['bar_end'])).isoformat()))

def reconcile(tx,now):
    day=now.astimezone(ET).date().isoformat()
    for ticker in sum(BANKS.values(),[]):
        state=tx.get(f'mf133-live-state:{day}:{ticker}',{})
        mid=state.get('message_id')
        if not mid:continue
        phase=state.get('phase')
        if phase not in ('confirmed','cancelled','expired'):continue
        # A setup resolved before delivery must never become a late entry.
        tx.execute("UPDATE mf_outbox SET status='expired' WHERE id=? AND status='pending'",(mid,))
        parent=tx.execute('SELECT status FROM mf_outbox WHERE id=?',(mid,)).fetchone()
        if not parent or parent[0]!='sent':continue
        reason=state.get('reason','')
        labels={'confirmed':'✅ 봉 마감 확인','cancelled':'✖ 후보 취소','expired':'⌛ 후보 만료'}
        detail={'bar_confirmed':'5분봉 확정 · 새 진입 아님','bar_failed':'마감 시 조건 불충족','observed_rejection':'진행 중 조건 해제','close_missing':'마감 확인 누락','cadence_gap':'관측 간격 초과','chart_exit':'X · 차트 종료 신호'}.get(reason,reason)
        code='MC' if state['side']==1 else 'SCMP'
        text=f'TEST {ticker} {code} · {labels[phase]}\n{detail}\n처리 {now.astimezone(DENVER).strftime("%H:%M:%S")} 덴버'
        action=phase+':'+reason
        payload=dict(text=text,ticker=ticker,action=action,parent=mid,received_at=now.isoformat(),expires_at=(now+timedelta(minutes=3)).isoformat())
        nid=mid+':'+action
        tx.enqueue(nid,payload)
        tx.event(nid,day,dict(source='mf133_notification',id=nid,**payload))

def delivery(store,send,now,clock=None):
    with store.transaction() as tx:
        if not active(tx.get('mf133-control',{}),now):
            tx.execute("UPDATE mf_outbox SET status='expired' WHERE status='pending' AND id LIKE ?",('mf133:%',))
            return []
        reconcile(tx,now)
    results=drain(store,send,limit=40,prefix='mf133:',clock=clock)
    # Handle resolution racing with an in-flight Telegram request.
    with store.transaction() as tx:reconcile(tx,now)
    results+=drain(store,send,limit=40,prefix='mf133:',clock=clock)
    with store.transaction() as tx:
        for r in results:tx.event('mf133-delivery:'+r['id'],now.astimezone(ET).date().isoformat(),dict(source='mf133_delivery',**r))
    return results

def status(store,now):
    day=now.astimezone(ET).date().isoformat()
    with store.transaction() as tx:
        cfg=tx.get('mf133-control',{})
        states={t:tx.get(f'mf133-live-state:{day}:{t}',{}) for t in sum(BANKS.values(),[])}
        return dict(version=VERSION,control=cfg,sending_now=active(cfg,now,True),states=states,banks={str(b):tx.get('mf133-bank:'+str(b),{}) for b in BANKS},legacy={p:tx.get(p+'-control',{}).get('enabled',False) for p in ('mf130','mf131','mf132')})
