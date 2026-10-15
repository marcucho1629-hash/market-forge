"""Pure closed-bar signal routing; no independent score or timing-confirm gate."""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json, math

VERSION='MF_V13_35'

def parse(value):
    t=datetime.fromisoformat(value.replace('Z','+00:00'))
    if t.tzinfo is None: raise ValueError('timezone required')
    return t.astimezone(timezone.utc)

def identity(row,side):
    return sha256(json.dumps([VERSION,row['ticker'],row['feed'],row['bucket_start'],side],separators=(',',':')).encode()).hexdigest()

def normalize(raw,now,session):
    r=dict(raw)
    if r.get('timeframe') not in ('1','5'):raise ValueError('unsupported timeframe')
    start,end=parse(r['bar_start']),parse(r['bar_end'])
    opening,closing=parse(session['open']),parse(session['close'])
    if end-start!=timedelta(minutes=int(r['timeframe'])):raise ValueError('bar duration')
    if not opening<=start<end<=closing:raise ValueError('outside session')
    if (start-opening).total_seconds()%(60*int(r['timeframe'])):raise ValueError('unaligned bar')
    if not 0<=(now-end).total_seconds()<=90:raise ValueError('bar not closed or stale')
    if r.get('closed') is not True:raise ValueError('unconfirmed bar')
    if any(type(r.get(k)) is not bool for k in ('mc','scmp','choppy')):raise ValueError('boolean required')
    if r['mc'] and r['scmp']:raise ValueError('conflicting signals')
    if r.get('exit') not in ('','X','HX','SX'):raise ValueError('invalid exit')
    if r.get('exit_side') not in (-1,0,1):raise ValueError('invalid exit side')
    if isinstance(r.get('price'),bool) or not isinstance(r.get('price'),(int,float)) or not math.isfinite(r['price']) or r['price']<=0:raise ValueError('invalid price')
    bucket=opening+timedelta(seconds=int((start-opening).total_seconds()//300)*300)
    r.update(bucket_start=bucket.isoformat(),bucket_end=(bucket+timedelta(minutes=5)).isoformat(),bar_start=start.isoformat(),bar_end=end.isoformat(),opening=(start-opening).total_seconds()<1800)
    return r

def process(state,r):
    """One ticker/feed/day state. Caller persists and de-duplicates transactionally."""
    out=[]; side=1 if r['mc'] else -1 if r['scmp'] else 0
    pending=state.setdefault('pending',{})
    if r['timeframe']=='1':
        # At the 5m boundary, await the 5m result instead of emitting a stale EARLY.
        if not r['opening'] or not side or r['bar_end']==r['bucket_end']:return out
        if r['bucket_start'] in state.setdefault('closed_buckets',[]):return out
        cid=identity(r,side)
        if cid in pending:return out
        pending[cid]=dict(side=side,bucket=r['bucket_start'],end=r['bucket_end'],phase='EARLY',price=r['price'])
        out.append(dict(action='EARLY',candidate_id=cid,side=side,price=r['price'],bar_end=r['bar_end'],bucket_end=r['bucket_end']))
        return out
    # Confirmations and exits are driven by 5m bars only. CHOP applies to new
    # regular-session entries after the opening window, not to exits.
    allowed=bool(side and (r['opening'] or not r['choppy']))
    for cid,p in pending.items():
        if p['phase']=='EARLY' and p['bucket']==r['bucket_start']:
            p['phase']='CONFIRMED' if allowed and side==p['side'] else 'UNCONFIRMED'
            if p['phase']=='UNCONFIRMED':out.append(dict(action='UNCONFIRMED',candidate_id=cid,side=p['side'],price=r['price'],bar_end=r['bar_end']))
    # Explicitly track previously confirmed candidate, never match by ticker alone.
    active=state.get('active')
    if r['exit'] and active and r['exit_side']==active['side']:
        out.append(dict(action='X',candidate_id=active['candidate_id'],side=active['side'],price=r['price'],bar_end=r['bar_end'],chart_exit=r['exit']))
        state['active']=None
    if allowed:
        cid=identity(r,side)
        out.append(dict(action='CONFIRM',candidate_id=cid,side=side,price=r['price'],bar_end=r['bar_end'],had_early=cid in pending))
        state['active']=dict(candidate_id=cid,side=side)
    state.setdefault('closed_buckets',[]).append(r['bucket_start'])
    return out

def expire(state,now):
    out=[]
    for cid,p in state.get('pending',{}).items():
        if p['phase']=='EARLY' and now>parse(p['end'])+timedelta(seconds=90):
            p['phase']='CLOSE_MISSING'
            out.append(dict(action='CLOSE_MISSING',candidate_id=cid,side=p['side'],price=p['price'],bar_end=p['end']))
    return out
