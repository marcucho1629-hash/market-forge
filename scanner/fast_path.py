"""V13.33 shadow-first early path. Never modifies the chart engine.

State belongs to a ticker AND source bar. Missing observations expire candidates;
only observed failure cancels them. Parameters are research defaults, not fitted.
"""
import hashlib
from datetime import timedelta
from .core import stamp, finite, ET
from .engine_relay import normalize as chart_normalize
from .early_test import BANKS

VERSION = 'MF_V13_33'
MAX_GAP = 20
CLOSE_GRACE = 60

def identity(r):
    return hashlib.sha256((r['feed']+'|'+r['bar_start']).encode()).hexdigest()

def gates(r, side):
    atr=r['previous_atr']; elapsed=r['elapsed_seconds']
    opening=stamp(r['bar_start'])==stamp(r['session_start'])
    trigger=r['session_open'] if opening else r['previous_high'] if side==1 else r['previous_low']
    distance=side*(r['price']-trigger)/atr
    body=side*(r['price']-r['open'])/atr
    wick=(r['high']-r['price'] if side==1 else r['price']-r['low'])/max(r['high']-r['low'],1e-9)
    # This is an elapsed-time rate proxy, NOT same-time-of-day RVOL.
    volume_rate=r['relative_volume']*300/max(elapsed,30)
    trend=all(side*(r['price']-r[k])>0 for k in ('ema9','ema21','vwap'))
    quality=not (r['choppy'] or r['wide_whipsaw'])
    prep=(30<=elapsed<270 and quality and trend and body>=.25 and
          side*(r['rsi']-50)>=2 and side*(r['rsi']-r['previous_rsi'])>=1 and
          volume_rate>=1.2 and -.1<=distance<=.75 and wick<=.4)
    fast=(prep and body>=.5 and side*(r['rsi']-50)>=5 and
          side*(r['rsi']-r['previous_rsi'])>=2 and volume_rate>=1.5 and
          0<=distance<=.75 and wick<=.25 and abs(r['price']-r['ema9'])/atr<=1.25)
    hold=(side*(r['price']-trigger)>=-.1*atr and side*(r['price']-r['ema9'])>=0 and
          side*(r['rsi']-50)>0 and wick<=.5)
    return dict(prep=prep,fast=fast,hold=hold,trigger=trigger,distance_atr=distance,
                body_atr=body,wick=wick,volume_rate_proxy=volume_rate,
                gap_from_prev_close=(r['session_open']/r['previous_session_close']-1)*100,
                extension_since_open=side*(r['price']-r['session_open'])/atr)

def expire(state,now):
    s=dict(state)
    if s.get('phase')!='pending':return s,None
    end=stamp(s['bar_end'])
    missing_close=now>end+timedelta(seconds=CLOSE_GRACE)
    stale_prep=not s.get('sent') and now<end and (now-stamp(s['last_observed'])).total_seconds()>MAX_GAP
    if missing_close or stale_prep:
        s.update(phase='expired',reason='close_missing' if missing_close else 'cadence_gap')
        return s,'EXPIRED'
    return s,None

def step(state,r,kind,now):
    s=dict(state);t=stamp(r['observed_at']);bar=identity(r)
    if s.get('phase')=='pending':
        expired,event=expire(s,now)
        if event:return expired,event
    if s.get('identity')==bar and s.get('phase') in ('cancelled','expired','confirmed'):
        # A confirmed lifecycle may end only on a subsequent observed chart exit.
        if s['phase']=='confirmed' and r['chart_exit'] and r['chart_exit_side']==s['side']:
            s.update(phase='cancelled',reason='chart_exit');return s,'CHART_X'
        return s,None
    if s.get('phase')=='confirmed':
        if r['chart_exit'] and r['chart_exit_side']==s['side']:
            s.update(phase='cancelled',reason='chart_exit');return s,'CHART_X'
        return s,None
    if s.get('phase')=='pending' and s['identity']!=bar:return s,None
    if s.get('last_observed') and t<=stamp(s['last_observed']):return s,None
    if kind=='closed':
        if s.get('phase')=='pending' and s['identity']==bar:
            ok=r['chart_mc'] if s['side']==1 else r['chart_scmp']
            s.update(last_observed=r['observed_at'],phase='confirmed' if s.get('sent') and ok else 'cancelled',reason='bar_confirmed' if ok else 'bar_failed')
            return s,('CONFIRM' if ok else 'CANCEL') if s.get('sent') else 'PREP_CANCEL'
        return s,None  # Never turn a missed EARLY into a late entry.
    if s.get('phase')=='pending':
        g=gates(r,s['side']);gap=(t-stamp(s['last_observed'])).total_seconds()
        age=(t-stamp(s['prep_at'])).total_seconds()
        s['last_observed']=r['observed_at']
        if s.get('sent'):
            if not g['hold']:
                s.update(phase='cancelled',reason='observed_rejection');return s,'CANCEL'
            return s,None
        if gap>MAX_GAP or age>30:
            s.update(phase='expired',reason='cadence_gap');return s,'EXPIRED'
        if not g['prep'] or s['side']*(r['price']-s['prep_price'])<-.05*s['atr']:
            s.update(phase='cancelled',reason='prep_failed');return s,'PREP_CANCEL'
        if age>=15 and g['fast'] and s['side']*(r['price']-s['prep_price'])<=.5*s['atr']:
            s.update(sent=True,stage='FAST_CONFIRM',entry_price=r['price']);return s,'SEND'
        return s,None
    options=[side for side in (1,-1) if gates(r,side)['prep']]
    if len(options)!=1:return s,None
    side=options[0]
    return dict(identity=bar,ticker=r['ticker'],phase='pending',stage='PREP',side=side,
                prep_at=r['observed_at'],last_observed=r['observed_at'],prep_price=r['price'],
                atr=r['previous_atr'],bar_end=r['bar_end'],sent=False), 'PREP'

def normalize_row(raw,kind,bank,now):
    # Keep the established OHLC/feed/time validation; each row fails independently.
    _,rows=chart_normalize(dict(source='MF_V13_31',schema_version=3,kind=kind,observations=[raw]),now)
    r=rows[0]
    if r['ticker'] not in BANKS[bank]:raise ValueError('outside bank')
    for k in ('previous_atr','session_open','previous_session_close'):r[k]=finite(r[k],.000001)
    r['previous_rsi']=finite(r['previous_rsi'],0,100)
    start=stamp(r['session_start'])
    if start.astimezone(ET).date()!=stamp(r['bar_start']).astimezone(ET).date() or start>stamp(r['bar_start']):raise ValueError('invalid session anchor')
    return r

def ingest(store,data,now):
    """Audit-only until a separately approved live cutover. No Telegram side effects."""
    if data.get('source')!=VERSION or data.get('schema_version')!=5:raise ValueError('wrong fast-path version')
    bank=data.get('bank')
    if type(bank) is not int or bank not in BANKS:raise ValueError('wrong bank')
    if not isinstance(data.get('observations'),list) or len(data['observations'])>20:raise ValueError('oversized batch')
    day=now.astimezone(ET).date().isoformat();decisions=[]
    with store.transaction() as tx:
        # Clock-based expiry runs even when no further packet arrives for a ticker.
        sweep_tx(tx,now)
        for raw in data['observations']:
            kind=raw.get('kind') if isinstance(raw,dict) else None
            try:r=normalize_row(raw,kind,bank,now)
            except (ValueError,TypeError,KeyError,OverflowError):
                decisions.append(dict(ticker=raw.get('ticker') if isinstance(raw,dict) else None,event='REJECTED_ROW'));continue
            key=f'mf133-state:{day}:{r["ticker"]}';prior=tx.get(key,{})
            old_seen=tx.get(f'mf133-terminal:{day}:{r["ticker"]}:{identity(r)}')
            if old_seen and prior.get('identity')!=identity(r):
                decisions.append(dict(ticker=r['ticker'],event='TERMINAL_BAR'));continue
            state,event=step(prior,r,kind,now);tx.put(key,state)
            if state.get('phase') in ('cancelled','expired','confirmed'):
                tx.put(f'mf133-terminal:{day}:{r["ticker"]}:{state["identity"]}',state['phase'])
            seenkey=f'mf133-seen:{day}:{r["ticker"]}:{kind}';previous=tx.get(seenkey)
            gap=(stamp(r['observed_at'])-stamp(previous)).total_seconds() if previous else None
            if previous is None or stamp(r['observed_at'])>stamp(previous):tx.put(seenkey,r['observed_at'])
            decisions.append(dict(ticker=r['ticker'],event=event,state=state,observation=r,gates={str(s):gates(r,s) for s in (1,-1)},observed_gap_seconds=gap))
        digest=hashlib.sha256(str(data).encode()).hexdigest()
        tx.event('mf133:'+digest,day,dict(source='mf133',received_at=now.isoformat(),bank=bank,decisions=decisions,coverage=data.get('coverage',[]),emitted_at=data.get('emitted_at'),execution_gap_ms=data.get('execution_gap_ms')))
    return dict(mode='shadow',decisions=decisions)

def sweep_tx(tx,now):
    day=now.astimezone(ET).date().isoformat();events=[]
    for ticker in sum(BANKS.values(),[]):
        key=f'mf133-state:{day}:{ticker}';s=tx.get(key,{})
        nxt,event=expire(s,now)
        if event:
            tx.put(key,nxt);tx.put(f'mf133-terminal:{day}:{ticker}:{nxt["identity"]}','expired')
            row=dict(source='mf133_expiry',ticker=ticker,state=nxt,at=now.isoformat())
            tx.event('mf133-expiry:'+nxt['identity'],day,row);events.append(row)
    return events

def sweep(store,now):
    with store.transaction() as tx:return sweep_tx(tx,now)
