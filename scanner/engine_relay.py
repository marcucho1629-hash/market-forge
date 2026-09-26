"""Relay chart-engine events; never synthesize an entry from a score."""
import hashlib
from datetime import timedelta
from .core import stamp, ET
from .calendar import current_session
from .live_signals import normalize as legacy_normalize, VERSION as LEGACY, DENVER
from .store import drain

VERSION = 'MF_V13_31'
ACTIONS = {'MC': 'LONG', 'SCMP': 'SHORT', 'CTN_UP': 'LONG', 'CTN_DOWN': 'SHORT'}

def normalize(data, now):
    if data.get('source') != VERSION or data.get('schema_version') != 3:
        raise ValueError('unsupported relay version')
    kind, rows = legacy_normalize({**data, 'source': LEGACY, 'schema_version': 2}, now)
    for r in rows:
        # The new producer supplies actual plot booleans, not remapped C/MP/CMP.
        for key in ('chart_mc', 'chart_scmp', 'chart_ctn_up', 'chart_ctn_down'):
            if type(r.get(key)) is not bool: raise ValueError('missing chart flag')
        if r.get('chart_exit') not in ('', 'X', 'HX', 'SX'): raise ValueError('invalid chart exit')
        if r.get('chart_exit_side') not in (-1, 0, 1): raise ValueError('invalid exit side')
        if r.get('feed') != 'BATS:'+r['ticker']:
            raise ValueError('explicit feed required')
    return kind, rows

def signal_id(r, action):
    return 'mf131:' + hashlib.sha256((r['feed']+'|5|'+r['bar_start']+'|'+action).encode()).hexdigest()

def ingest(store, data, now):
    kind, rows = normalize(data, now)
    session, market_open = current_session(now)
    decisions = []
    with store.transaction() as tx:
        control = tx.get('mf131-control', {})
        enabled = control.get('enabled') is True and control.get('version') == VERSION
        for r in rows:
            actions = []
            if r['chart_exit']: actions.append(r['chart_exit'])
            for flag, code in [('chart_mc','MC'), ('chart_scmp','SCMP'), ('chart_ctn_up','CTN_UP'), ('chart_ctn_down','CTN_DOWN')]:
                if r[flag]: actions.append(code)
            # Keep every input field needed to explain absence/presence of a message.
            rowlog = {'ticker':r['ticker'], 'received_at':now.isoformat(), 'observation':r, 'events':[]}
            for action in actions:
                identity = signal_id(r, action)
                prior = tx.get(identity)
                if prior:
                    if kind == 'closed':
                        prior['confirmed_at'] = now.isoformat(); tx.put(identity, prior)
                    rowlog['events'].append({'id':identity,'action':action,'result':'already_observed'})
                    continue
                event = {'id':identity,'action':action,'ticker':r['ticker'],'feed':r['feed'],
                         'bar_start':r['bar_start'],'detected_at':r['observed_at'],
                         'received_at':now.isoformat(),'price':r['price'],'confirmed':kind=='closed',
                         'result':'queued' if enabled and market_open else 'shadow' if market_open else 'outside_session'}
                if enabled and market_open:
                    side = ACTIONS.get(action, 'LONG' if r['chart_exit_side']==1 else 'SHORT' if r['chart_exit_side']==-1 else '')
                    code = 'X' if action in ('X','HX','SX') else 'CTN' if action.startswith('CTN') else action
                    icon = '✖' if code=='X' else '↗' if code=='CTN' else '⚡'
                    direction = '상승' if side=='LONG' else '하락' if side=='SHORT' else ''
                    label = '5분봉 확정' if kind=='closed' else '미확정'
                    at = stamp(r['observed_at']).astimezone(DENVER).strftime('%H:%M:%S')
                    text = f'{icon} {r["ticker"]} {code} · {direction}\n${r["price"]:.2f} · 차트 엔진 {action}\n감지 {at} 덴버 · {label}'
                    tx.enqueue(identity, {'text':text, 'expires_at':(stamp(r['observed_at'])+timedelta(seconds=30)).isoformat(), **event})
                tx.put(identity,event)
                tx.event(identity,session['date'],{'source':'mf131_event',**event})
                rowlog['events'].append(event)
            # Record closed-bar rejection without turning it into a fictitious chart X.
            if kind=='closed':
                for action in ('MC','SCMP'):
                    identity = signal_id(r,action); prior = tx.get(identity)
                    if prior and action not in actions:
                        prior['closed_validation']='not_present_at_close';prior['validated_at']=now.isoformat();tx.put(identity,prior)
                        rowlog['events'].append({'id':identity,'action':action,'result':'not_present_at_close'})
            decisions.append(rowlog)
        digest=hashlib.sha256((str(data)+now.isoformat()).encode()).hexdigest()
        tx.event('mf131-observation:'+digest,session['date'],{'source':'mf131','received_at':now.isoformat(),'kind':kind,'decisions':decisions})
        if type(data.get('bank')) is int and 1<=data['bank']<=7:
            tx.put('mf131-bank:'+str(data['bank']),{'received_at':now.isoformat(),'kind':kind,'feeds':[r['feed'] for r in rows]})
    return {'status':'ok','mode':'live' if enabled else 'shadow','decisions':decisions}

def delivery(store, send, now, clock=None):
    results=drain(store,send,limit=70,prefix='mf131:',clock=clock)
    with store.transaction() as tx:
        for result in results:
            event=tx.get(result['id'],{})
            tx.event('mf131-delivery:'+result['id'],now.astimezone(ET).date().isoformat(),{'source':'mf131_delivery','detected_at':event.get('detected_at'),'received_at':event.get('received_at'),**result})
    return results
