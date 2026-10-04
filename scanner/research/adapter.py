"""Translate V13.34 Pine observations to isolated shared-core research input.
Authentication and exchange-session admission belong to the HTTP boundary.
This module neither enables delivery nor sends Telegram messages.
"""
from datetime import datetime
from .replay import normalize
BANKS={1:'SPY QQQ NVDA TSLA AVGO AMD META AAPL MSFT AMZN'.split(),2:'PLTR PANW SNDK GOOG RDDT HOOD COIN JPM AXP V'.split()}
CORE_VERSION='v134-core-1'
def aware(value):
 if not isinstance(value,str):raise ValueError('timestamp string required')
 t=datetime.fromisoformat(value.replace('Z','+00:00'))
 if t.tzinfo is None:raise ValueError('timezone required')
 return t

def adapt(data,received_at):
 if data.get('source')!='MF_V13_34_SHADOW' or data.get('schema_version')!=6:raise ValueError('wrong producer version')
 if type(data.get('bank')) is not int or data['bank'] not in BANKS:raise ValueError('invalid bank')
 if received_at.tzinfo is None:raise ValueError('server timezone required')
 emitted=aware(data['emitted_at'])
 if not 0<=(received_at-emitted).total_seconds()<=15:raise ValueError('stale envelope')
 raw=data.get('observations')
 if not isinstance(raw,list) or len(raw)>20:raise ValueError('invalid observation count')
 result=[];rejected=[]
 for i,r in enumerate(raw):
  try:
   if r['ticker'] not in BANKS[data['bank']] or r['feed']!='BATS:'+r['ticker']:raise ValueError('wrong ticker/feed/bank')
   for key in ('confirmed_parity','preview_state_unchanged'):
    if r.get(key) is not True:raise ValueError('core audit failed: '+key)
   for key in ('core_preview_mc','core_preview_scmp','chart_mc','chart_scmp'):
    if type(r.get(key)) is not bool:raise ValueError('missing boolean: '+key)
   if r['core_preview_mc'] and r['core_preview_scmp']:raise ValueError('conflicting preview directions')
   if r['kind'] not in ('pulse','closed'):raise ValueError('invalid observation kind')
   observed=aware(r['observed_at'])
   if not observed<=emitted:raise ValueError('clock ordering')
   # Both directions are emitted as observations: false invalidates an existing
   # candidate but cannot create a new one. Never substitute server score gates.
   for side,preview,confirmed in ((1,'core_preview_mc','chart_mc'),(-1,'core_preview_scmp','chart_scmp')):
    row=dict(engine='shared_core',ticker=r['ticker'],feed=r['feed'],timeframe='5',source_bar_start=r['bar_start'],bar_end=r['bar_end'],direction=side,core_version=CORE_VERSION,observed_at=r['observed_at'],webhook_emitted_at=data['emitted_at'],server_received_at=received_at.isoformat(),core_ok=r[preview],price=r['price'],previous_confirmed_atr=r['previous_atr'],kind=r['kind'])
    if r['kind']=='closed':row['confirmed']=r[confirmed]
    result.append(normalize(row))
  except (KeyError,ValueError,TypeError) as exc:rejected.append(dict(index=i,reason=str(exc)))
 return dict(rows=result,rejected=rejected,bank=data['bank'])
