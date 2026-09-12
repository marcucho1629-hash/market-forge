from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
from .core import (ET, VERSION, Config, State, bars_from, evaluate, rank_and_activate,
                   select_universe, stamp, market_context)


def session_info(payload, now):
    """Calendar supplied by the provider; holiday closed dates are explicit."""
    day = now.astimezone(ET).date().isoformat()
    if payload['date'] != day:
        raise ValueError('session date is not today in New York')
    if not payload['is_open']:
        return day,None,None
    op,cl=stamp(payload['open']),stamp(payload['close'])
    if op.astimezone(ET).date().isoformat()!=day or not op<cl or cl-op>timedelta(hours=7):
        raise ValueError('invalid session calendar')
    if op.astimezone(ET).weekday()>=5:
        raise ValueError('weekend cannot be an equity session')
    return day,op,cl


class Scanner:
    def __init__(self,store,cfg=None,silent=True):
        self.store,self.cfg,self.silent=store,cfg or Config(),silent

    def premarket(self,payload,now):
        day,op,cl=session_info(payload['session'],now)
        if op is None: return {'status':'closed'}
        if not op-timedelta(minutes=45)<=now<op:
            return {'status':'outside_premarket_window'}
        asof=stamp(payload['asof'])
        if asof>now or now-asof>timedelta(minutes=5): raise ValueError('stale premarket payload')
        selected=select_universe(payload['universe'],asof,self.cfg)
        with self.store.transaction() as tx:
            key='universe:'+day
            existing=tx.get(key)
            if existing: return {'status':'already_selected',**existing}
            if not selected['bullish'] and not selected['bearish']:
                return {'status':'no_qualified_candidates',**selected}
            record={**selected,'asof':asof.isoformat(),'version':VERSION,
                    'data_source':payload.get('data_source',{}),'excluded':payload.get('excluded',{})}
            tx.put(key,record)
            tx.event(key,day,{'source':'selector',**record})
        return {'status':'selected',**record}

    def scan(self,payload,now):
        day,op,cl=session_info(payload['session'],now)
        if op is None: return {'status':'closed'}
        if not op<now<=cl+timedelta(minutes=2): return {'status':'outside_session'}
        asof=stamp(payload['asof'])
        if asof>now or now-asof>timedelta(seconds=120): raise ValueError('stale scan payload')
        fast=now<op+timedelta(minutes=5)
        step=1 if fast else 5
        elapsed=int((min(asof,cl)-op).total_seconds()//60)
        target=op+timedelta(minutes=(elapsed//step)*step)
        if target<=op: return {'status':'awaiting_closed_bar'}
        key=f'batch:{day}:{target.isoformat()}'
        with self.store.transaction() as tx:
            if tx.get(key): return {'status':'duplicate_batch'}
            mode = 'silent' if self.silent else 'live'
            prior_mode = tx.get('mode:'+day)
            if prior_mode and prior_mode!=mode:
                raise ValueError('mode changes require a new session; shadow positions are isolated')
            tx.put('mode:'+day,mode)
            u=tx.get('universe:'+day)
            if not u: raise ValueError('today premarket universe missing')
            provenance=payload.get('data_source',{})
            original=u.get('data_source',{})
            if original and any(original.get(k)!=provenance.get(k) for k in ('provider','feed','adjustment')):
                raise ValueError('Cannot switch data provider/feed/adjustment within a selected session')
            symbols=[x['ticker'] for x in u['bullish']+u['bearish']]
            items=payload['symbols']
            if set(items)!=set(symbols):
                raise ValueError('batch must contain entire selected universe (global Top K)')
            states={s:State(**tx.get(f'state:{day}:{s}',{})) for s in symbols}
            decisions=[]
            for symbol in symbols:
                item=items[symbol]
                bars=bars_from(item['bars_5m'],asof,5)
                opening=bars_from(item.get('bars_1m',[]),asof,1) if fast else None
                active=opening if fast else bars
                if not active or active[-1].end!=target:
                    raise ValueError(f'{symbol}: missing target closed bar')
                if fast and bars and bars[-1].end>op:
                    raise ValueError('fast lane warmup must precede session open')
                if any(b.end>cl for b in active): raise ValueError('bar after session close')
                context=item.get('context') or market_context(payload.get('benchmarks',{}),target,op,item.get('sector_etf'))
                d=evaluate(symbol,bars,states[symbol],op,self.cfg,item.get('catalyst'),
                           context,opening,item.get('opening_volume_baseline'))
                decisions.append(d)
            # A shared 5m bucket caps the union of fast-lane and normal entries at Top K.
            bucket=f'quota:{day}:{int((target-op).total_seconds()-1)//300}'
            used=tx.get(bucket,0)
            remaining=max(0,self.cfg.top_k-used)
            if remaining:
                local=Config(**{**asdict(self.cfg),'top_k':remaining})
                rank_and_activate(decisions,states,local)
            else:
                for d in decisions:
                    if d.eligible: d.reasons.append('bucket_delivery_cap')
            selected=sum(d.selected for d in decisions)
            tx.put(bucket,used+selected)
            for d in decisions:
                event={'source':'scanner','silent':self.silent,'observed_at':now.isoformat(),
                       'data_source':provenance,'data_asof':asof.isoformat(),
                       'config':asdict(self.cfg),**asdict(d)}
                id=f'scanner:{day}:{d.end}:{d.ticker}'
                tx.event(id,day,event)
                tx.put(f'state:{day}:{d.ticker}',asdict(states[d.ticker]))
                if not self.silent and (d.selected or d.action in ('X','HX','SX')):
                    tx.enqueue(id,{'text':format_signal(d)})
                # Store closed bars for point-in-time 15/30m comparison, excluding premarket.
                for b in bars:
                    if op < b.end <= cl:
                        tx.event(f'bar:{day}:{d.ticker}:{b.end.isoformat()}',day,
                                 {'source':'bar','ticker':d.ticker,'end':b.end.isoformat(),
                                  'open':b.open,'high':b.high,'low':b.low,'close':b.close})
            tx.put(key,{'processed_at':now.isoformat()})
        return {'status':'ok','silent':self.silent,'asof':target.isoformat(),
                'decisions':[asdict(d) for d in decisions]}


def format_signal(d):
    if d.action in ('X','SX','HX'):
        return f'{d.ticker} | {d.action} | ${d.price:.2f}\nTrend structure weakened\nMarket Forge AI'
    return (f'{d.ticker} | {d.action} | ${d.price:.2f}\nMF Score: {d.final:.1f}/100\n'
            f'Technical: {d.technical:.1f} | Catalyst: {d.catalyst:.1f} | Context: {d.context:.1f}\n'
            f'Market: {d.market}\nMarket Forge AI')


def log_tradingview(store,data,now):
    symbol=str(data.get('ticker','N/A')).upper()
    # Never log webhook secrets or the raw untrusted payload.
    event={'source':'tradingview','ticker':symbol,'action':str(data.get('action','ALERT')).upper(),
           'price':str(data.get('price','N/A')),'observed_at':now.isoformat(),
           'end':now.isoformat(),'timestamp_quality':'receipt_time'}
    if data.get('bar_end'):
        try:
            end=stamp(data['bar_end'])
            if end<=now:
                event['end']=end.isoformat()
                event['timestamp_quality']='provided_bar_end'
        except (ValueError,TypeError): pass
    id='tv:'+hashlib.sha256(json.dumps(event,sort_keys=True).encode()).hexdigest()
    with store.transaction() as tx:
        tx.event(id,now.astimezone(ET).date().isoformat(),event)
