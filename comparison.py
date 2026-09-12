"""Descriptive signal comparison; no profitability claim or look-ahead in decisions."""
from datetime import timedelta
from .core import stamp, finite

SIDES={'C':1,'MC':1,'P':-1,'MP':-1,'CMP':-1,'SCMP':-1}


def compare(events):
    bars={}
    for e in events:
        if e['source']=='bar': bars.setdefault(e['ticker'],[]).append(e)
    for seq in bars.values(): seq.sort(key=lambda e:e['end'])
    signals=[e for e in events if e['source'] in ('scanner','tradingview') and e.get('action') in SIDES
             and (e['source']=='tradingview' or e.get('selected'))]
    rows=[]
    for e in signals:
        side=SIDES[e['action']]
        t=stamp(e['end'])
        row={k:e.get(k) for k in ('source','ticker','action','end','price','final','timestamp_quality')}
        row['follow_through']={}
        try: price=finite(e['price'],.00001)
        except (ValueError,TypeError):
            row['error']='invalid_price'
            rows.append(row)
            continue
        for horizon in (15,30):
            target=t+timedelta(minutes=horizon)
            seq=[b for b in bars.get(e['ticker'],[]) if t<stamp(b['end'])<=target]
            exact=next((b for b in seq if stamp(b['end'])==target),None)
            # For receipt-time TV or 1m fast entries use the first 5m close >= horizon.
            if exact is None:
                exact=next((b for b in bars.get(e['ticker'],[]) if target<=stamp(b['end'])<target+timedelta(minutes=5)),None)
            if exact is None:
                row['follow_through'][str(horizon)]={'status':'pending_or_missing'}
            else:
                # Exclude the first overlapping bar from excursions to avoid pre-entry extremes.
                window=[b for b in bars[e['ticker']] if stamp(b['end'])-timedelta(minutes=5)>=t and stamp(b['end'])<=stamp(exact['end'])]
                row['follow_through'][str(horizon)]={
                    'status':'observed','actual_minutes':(stamp(exact['end'])-t).total_seconds()/60,
                    'directional_return_pct':100*side*(exact['close']/price-1),
                    'mfe_pct':max([0]+[100*(b['high']/price-1) if side>0 else 100*(1-b['low']/price) for b in window]),
                    'mae_pct':min([0]+[100*(b['low']/price-1) if side>0 else 100*(1-b['high']/price) for b in window])}
        alternatives=[x for x in signals if x['source']!=e['source'] and x['ticker']==e['ticker']
                      and SIDES[x['action']]==side and abs((stamp(x['end'])-t).total_seconds())<=1800]
        nearest=min(alternatives,key=lambda x:abs((stamp(x['end'])-t).total_seconds()),default=None)
        row['nearest_other_engine_seconds']=(stamp(nearest['end'])-t).total_seconds() if nearest else None
        rows.append(row)
    rejected={}
    for e in events:
        if e['source']=='scanner' and not e.get('selected'):
            for r in e.get('reasons',[]): rejected[r]=rejected.get(r,0)+1
    return {'signals':rows,'rejection_counts':rejected,
            'notes':['Nearest matching is descriptive, not one-to-one attribution.',
                     'Follow-through excludes fees, spread and slippage; timestamps may be receipt-time.',
                     'Unselected tickers have no server OHLCV; their follow-through remains missing.']}
