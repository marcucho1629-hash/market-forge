"""Experimental 80 chart / 20 Bigdata gate. Shadow only; never queues messages."""
import hashlib
import json
from .core import stamp, finite, ET

VERSION = 'chart80-bigdata20-v1'
UNIVERSE = tuple('SPY QQQ NVDA TSLA AVGO AMD META AAPL MSFT AMZN PLTR PANW SNDK GOOG RDDT HOOD COIN JPM AXP V XOM CVX UNH JNJ CAT AVAV TGT SHOP UAL ENPH DDOG IREN ARM ANET IBM QCOM CRWD SNOW MSTR CRCL ORCL MRVL DELL TSM LITE NOW CRM GLW SMCI ADBE TWLO NET ASTS MRNA HPE HIMS ON WMT NBIS INTC AAOI MU DIS NFLX COST BA GS BAC UBER AMAT'.split())


def number(row, key, low=None, high=None):
    if isinstance(row[key], bool):
        raise ValueError(key + ' must be numeric')
    return finite(row[key], low, high)


def assess(tx, row, now):
    ticker, direction = row['ticker'], row['direction']
    if ticker not in UNIVERSE or direction not in ('LONG', 'SHORT'):
        raise ValueError('unknown ticker or direction')
    observed = stamp(row['observed_at'])
    if not 0 <= (now-observed).total_seconds() <= 15:
        raise ValueError('stale or future observation')
    if row.get('sampling') not in ('intrabar', 'closed_1m', 'closed_5m'):
        raise ValueError('explicit sampling required')
    for key in ('choppy', 'wide_whipsaw', 'breakout_valid', 'pullback_valid'):
        if type(row.get(key)) is not bool:
            raise ValueError(key + ' must be boolean')
    side = 1 if direction == 'LONG' else -1
    price, e9, e21, vwap = [number(row, k, .000001) for k in ('price','ema9','ema21','vwap')]
    slope = number(row, 'ema9_slope_atr') * side
    relative = number(row, 'relative_return_pct') * side
    rv = number(row, 'relative_volume', 0)
    move = number(row, 'directional_momentum_atr', 0)
    rsi = number(row, 'rsi', 0, 100)
    extension = number(row, 'extension_atr', 0)
    trend = (8 if side*(e9-e21)>0 else 0) + (6 if side*(price-vwap)>0 else 0) + (5 if slope>0 else 0) + (6 if relative>0 else 0)
    momentum = 10*min(rv/3,1) + 10*min(move,1) + 5*min(max(side*(rsi-50)/20,0),1)
    structure = 20 if row['breakout_valid'] or row['pullback_valid'] else 0
    location = 10*max(0,1-extension/2)
    components = dict(trend_relative=trend, volume_momentum=momentum, structure=structure, location=location)
    chart = sum(components.values())
    usable = []
    # Evidence is read from the authenticated server inbox, never the alert payload.
    for category in ('news','catalyst'):
        item = tx.get(f'quality-evidence:{ticker}:{direction}:{category}')
        if not item or item.get('source') != 'Bigdata.com' or item.get('direction') != direction:
            continue
        doc = tx.get('quality-news-document:'+item['document_id'])
        if not doc or doc.get('status') != 'ready_for_review':
            continue
        published, known = stamp(item['published_at']), stamp(item['known_at'])
        age = (observed-published).total_seconds()
        if not published <= stamp(doc['first_seen_at']) <= known <= observed or not 0 <= age <= 10800:
            continue
        if not item.get('supporting_excerpt') or item['supporting_excerpt'] not in doc.get('text',''):
            continue
        freshness = 1 if age<=3600 else .5
        usable.append((finite(item['score'],0,100)*.2*freshness,item))
    # Maximum, never sum: duplicate news/catalyst events cannot inflate the 20-point allocation.
    best = max(usable,key=lambda pair:pair[0]) if usable else None
    news = best[0] if best else None
    total = chart+news if news is not None else None
    blocks = []
    if row['choppy']: blocks.append('choppy')
    if row['wide_whipsaw']: blocks.append('wide_whipsaw')
    if extension>=2: blocks.append('chasing')
    if structure==0: blocks.append('setup_not_ready')
    if chart<60: blocks.append('chart_below_60')
    if news is None: blocks.append('bigdata_missing_or_unverified')
    if total is not None and total<80: blocks.append('total_below_80')
    return dict(ticker=ticker,direction=direction,observed_at=observed.isoformat(),sampling=row['sampling'],
                components={k:round(v,3) for k,v in components.items()},chart_score=round(chart,3),
                bigdata_score=round(news,3) if news is not None else None,
                total_score=round(total,3) if total is not None else None,
                evidence=best[1] if best else None,blocks=blocks,entry_selected=False,
                momentum_high=rv>=1.5 and move>=.5 and side*(rsi-50)>=15,
                information_selected=False,quote_verified=False)


def shadow_batch(store,payload,now):
    rows=payload['observations']
    if not isinstance(rows,list) or not 1<=len(rows)<=70 or len({r['ticker'] for r in rows})!=len(rows):
        raise ValueError('1..70 distinct tickers required')
    day=now.astimezone(ET).date().isoformat()
    digest=hashlib.sha256(json.dumps(payload,sort_keys=True,allow_nan=False).encode()).hexdigest()
    key=f'{VERSION}:{day}:{digest}'
    with store.transaction() as tx:
        old=tx.get(key)
        if old: return {**old,'duplicate':True}
        session=tx.get('tv-session:'+day)
        active=bool(session and session.get('is_open') is True and stamp(session['open'])<=now<stamp(session['close']))
        decisions=[assess(tx,row,now) for row in rows]
        quota_key=f'{VERSION}:quota:{int(now.timestamp())//300}'
        quota=tx.get(quota_key,0)
        for d in sorted(decisions,key=lambda d:(-(d['total_score'] if d['total_score'] is not None else d['chart_score']),d['ticker'])):
            if not active: d['blocks'].append('session_unverified_or_closed')
            state_key=f'{VERSION}:state:{day}:{d["ticker"]}'
            state=tx.get(state_key,{})
            if state.get('observed_at') and stamp(d['observed_at'])<=stamp(state['observed_at']):
                d['blocks'].append('duplicate_or_out_of_order')
                continue
            recent=state.get('selected_at') and (now-stamp(state['selected_at'])).total_seconds()<1200
            if recent: d['blocks'].append('cooldown')
            if quota>=3: d['blocks'].append('budget')
            d['entry_selected']=not d['blocks']
            hard={'choppy','wide_whipsaw','chasing','session_unverified_or_closed','cooldown','budget'}
            d['information_selected']=not d['entry_selected'] and d['momentum_high'] and not hard.intersection(d['blocks'])
            if d['entry_selected'] or d['information_selected']:
                quota+=1
                state['selected_at']=now.isoformat()
            state['observed_at']=d['observed_at']
            tx.put(state_key,state)
        result=dict(version=VERSION,mode='shadow',decisions=decisions,duplicate=False,
                    telegram_sent=0,stream_connected=False,ranking_scope='submitted_batch',
                    note='Experimental score, not win probability; no live quote/spread verification.')
        tx.put(quota_key,quota)
        tx.put(key,result)
        tx.event(key,day,{'source':'chart_news_shadow',**result})
        return result
