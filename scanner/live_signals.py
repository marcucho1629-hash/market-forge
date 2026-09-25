"""V13.30: independent early observations, scored entries and sent-signal lifecycle."""
import hashlib,json
from datetime import timedelta
from zoneinfo import ZoneInfo
from .core import stamp,finite,ET
from .chart_news import assess,UNIVERSE
from .calendar import current_session
from .store import drain

VERSION='MF_V13_30'
DENVER=ZoneInfo('America/Denver')
ENTRY_ACTIONS={'C','MC','P','MP','CMP','SCMP'}
EXIT_ACTIONS={'X','HX','SX'}

def level(score):
    return 3 if score is not None and score>=80 else 2 if score is not None and score>=75 else 1 if score is not None and score>=70 else 0

def message(row,code,rank=0,reason='',early=False):
    side='상승' if row['direction']=='LONG' else '하락'
    icon={3:'🚀',2:'🟢',1:'🟡'}.get(rank,'⚡')
    first=f'{icon} {row["ticker"]} {code} · {side}'
    if code=='X':first=f'✖ {row["ticker"]} X · {side} 후보 종료'
    if code=='CTN':first=f'↗ {row["ticker"]} CTN {"↑" if row["direction"]=="LONG" else "↓"}'
    detail=f'종합 {row["total_score"]:.1f}/100' if rank else f'차트 {row.get("chart_score",0):.1f}/80 · 뉴스 미확인' if row.get('bigdata_score') is None else f'차트 {row.get("chart_score",0):.1f}/80'
    if code in ('X','CTN'):detail=reason
    label='미확정' if early else '5분봉 확정'
    if code=='X':label='실시간 감지' if row['sampling']=='intrabar' else '5분봉 확정'
    time=stamp(row['observed_at']).astimezone(DENVER).strftime('%H:%M:%S')
    return f'{first}\n${row["price"]:.2f} · {detail}\n{time} 덴버 · {label}'

def normalize(data,now):
    if data.get('source')!=VERSION or data.get('schema_version')!=2:raise ValueError('unsupported scanner version')
    kind=data.get('kind')
    if kind not in ('closed','pulse'):raise ValueError('invalid sampling')
    rows=data.get('observations')
    if not isinstance(rows,list) or not 1<=len(rows)<=10:raise ValueError('1..10 observations required')
    if len({r.get('ticker') for r in rows})!=len(rows):raise ValueError('duplicate ticker')
    clean=[]
    for raw in rows:
        r=dict(raw)
        if r.get('ticker') not in UNIVERSE:raise ValueError('unknown ticker')
        observed=stamp(r['observed_at']); start=stamp(r['bar_start']); end=stamp(r['bar_end'])
        if not 0<=(now-observed).total_seconds()<=15:raise ValueError('stale observation')
        if (end-start).total_seconds()!=300:raise ValueError('invalid 5m interval')
        if kind=='closed' and not 0<=(observed-end).total_seconds()<=60:raise ValueError('unconfirmed or stale bar')
        if kind=='pulse' and not start<=observed<end:raise ValueError('not a live source bar')
        if r.get('benchmark_start')!=r['bar_start']:raise ValueError('benchmark mismatch')
        r['sampling']='closed_5m' if kind=='closed' else 'intrabar'
        for k in ('price','open','high','low','atr','ema9','ema21','vwap','previous_ema9','previous_high','previous_low'):
            if isinstance(r[k],bool):raise ValueError('numeric required')
            r[k]=finite(r[k],.000001)
        if r['high']<max(r['open'],r['price']) or r['low']>min(r['open'],r['price']):raise ValueError('invalid OHLC')
        r['rsi']=finite(r['rsi'],0,100)
        r['relative_volume']=finite(r['relative_volume'],0)
        r['elapsed_seconds']=(observed-start).total_seconds()
        r['direction']='LONG' if r['price']>=r['open'] else 'SHORT'
        action=r.get('engine_action','')
        if kind=='closed' and action in ENTRY_ACTIONS:r['direction']='LONG' if action in ('C','MC') else 'SHORT'
        side=1 if r['direction']=='LONG' else -1
        r['directional_momentum_atr']=max(0,side*(r['price']-r['open'])/r['atr'])
        r['extension_atr']=abs(r['price']-r['ema9'])/r['atr']
        r['ema9_slope_atr']=(r['ema9']-r['previous_ema9'])/r['atr']
        r['relative_return_pct']=finite(r['symbol_return_pct'])-finite(r['benchmark_return_pct'])
        for k in ('choppy','wide_whipsaw','bull_breakout','bear_breakout','bull_pullback','bear_pullback'):
            if type(r.get(k)) is not bool:raise ValueError('boolean required: '+k)
        r['breakout_valid']=r['bull_breakout'] if side==1 else r['bear_breakout']
        r['pullback_valid']=r['bull_pullback'] if side==1 else r['bear_pullback']
        clean.append(r)
    return kind,clean

def early_ok(r):
    side=1 if r['direction']=='LONG' else -1
    # Partial volume is compared with prior completed 5m averages, not extrapolated.
    return (r['elapsed_seconds']>=15 and r['relative_volume']>=1.5 and
            r['directional_momentum_atr']>=.5 and side*(r['rsi']-50)>=15 and
            side*(r['price']-r['vwap'])>0 and side*(r['price']-r['ema9'])>0 and
            (r['price']>r['previous_high'] if side==1 else r['price']<r['previous_low']) and
            not (r['choppy'] or r['wide_whipsaw']) and r['extension_atr']<2 and r.get('chart_score',0)>=60)

def exit_reason(state,r,closed):
    side=1 if state['direction']=='LONG' else -1
    if closed and r.get('engine_exit') in EXIT_ACTIONS and r.get('engine_exit_side')==side:return '추세 종료'
    if side*(r['price']-state['entry_price'])<=-state['entry_atr']:return '손절 기준'
    retrace=side*(state['extreme']-r['price']);giveback=side*(state['extreme_rsi']-r['rsi'])
    counter=side*(r['price']-r['open'])<0;body=abs(r['price']-r['open']);atr=r['atr']
    damage=side*(r['price']-r['ema9'])<0 or (r['price']<r['previous_low'] if side==1 else r['price']>r['previous_high'])
    healthy=side*(r['ema9']-r['ema21'])>0 and side*(r['ema9']-r['previous_ema9'])>=0 and side*(r['price']-r['ema9'])>0 and side*(r['price']-r['vwap'])>0 and side*(r['rsi']-50)>=2
    score=sum([retrace>.42*atr,giveback>=5,counter and body>.24*atr,damage])
    if retrace>.65*atr and giveback>=6 and counter and body>.45*atr and (damage or side*(r['price']-r['vwap'])<0):return '급격한 모멘텀 약화'
    if retrace>.42*atr and giveback>=5 and score>=3 and (not healthy or retrace>.62*atr):return '모멘텀 약화'
    if closed and state.get('early') and not state.get('confirmed') and stamp(r['bar_end'])>stamp(state['observed_at']):
        if r['direction']!=state['direction'] or not early_ok({**r,'elapsed_seconds':300}):return '조기 조건 해제'
    return None

def reconcile(tx,key,state):
    pending=state.get('pending')
    if not pending:return state
    row=tx.execute('SELECT status FROM mf_outbox WHERE id=?',(pending['id'],)).fetchone()
    if not row or row[0] in ('pending','attempting'):return state
    if row[0]=='sent':state=pending['next']
    else:state={k:v for k,v in state.items() if k!='pending'}
    tx.put(key,state)
    return state

def queue(tx,key,state,r,code,rank,reason,early,now,session,next_state):
    identity='mf130:'+hashlib.sha256((key+r['observed_at']+code+str(rank)).encode()).hexdigest()
    expiry=min(stamp(session['close'])+timedelta(minutes=5),now+timedelta(seconds=60))
    tx.enqueue(identity,{'text':message(r,code,rank,reason,early),'expires_at':expiry.isoformat(),'ticker':r['ticker'],'code':code,'session':session['date'],'observed_at':r['observed_at']})
    state['pending']={'id':identity,'next':next_state}
    tx.put(key,state)
    return identity

def ingest(store,data,now):
    kind,rows=normalize(data,now);session,active=current_session(now)
    day=session['date'];closed=kind=='closed';out=[]
    digest=hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest()
    batch='mf130-batch:'+digest
    with store.transaction() as tx:
        if tx.get(batch):return {'status':'duplicate','decisions':[]}
        control=tx.get('mf130-control',{})
        enabled=control.get('enabled') is True and control.get('version')==VERSION
        quota_key=f'mf130-quota:{day}:{int(now.timestamp())//300}';quota=tx.get(quota_key,0)
        assessed=[]
        for r in rows:
            d=assess(tx,r,now);d['blocks']=[b for b in d['blocks'] if b!='total_below_80']
            r.update(d);r['rank']=level(r['total_score'])
            assessed.append(r)
        for r in sorted(assessed,key=lambda x:-(x['total_score'] if x['total_score'] is not None else x['chart_score'])):
            ticker=r['ticker'];key=f'mf130-state:{day}:{ticker}';state=reconcile(tx,key,tx.get(key,{}))
            d={k:r[k] for k in ('ticker','direction','observed_at','sampling','price','chart_score','bigdata_score','total_score','blocks','rank')}
            d.update(code=None,delivery_id=None)
            if not active or not enabled:
                d['blocks'].append('outside_session_or_disabled');out.append(d);continue
            seenkey=key+':'+kind+':seen';seen=tx.get(seenkey)
            if seen and stamp(r['observed_at'])<=stamp(seen):d['blocks'].append('duplicate_or_out_of_order');out.append(d);continue
            tx.put(seenkey,r['observed_at'])
            tx.put('mf130-latest:'+ticker,{'observed_at':r['observed_at'],'chart_score':r['chart_score'],'price':r['price'],'sampling':r['sampling']})
            if state.get('pending'):d['blocks'].append('delivery_pending');out.append(d);continue
            if state.get('active'):
                # High/low from before the alert is never used to seed extrema.
                side=1 if state['direction']=='LONG' else -1
                if side*(r['price']-state['extreme'])>0:state['extreme']=r['price']
                if side*(r['rsi']-state['extreme_rsi'])>0:state['extreme_rsi']=r['rsi']
                reason=exit_reason(state,r,closed)
                if reason:
                    original={**r,'direction':state['direction']}
                    nxt={'active':False,'last_alert':r['observed_at'],'closed_reason':reason}
                    d['code']='X';d['delivery_id']=queue(tx,key,state,original,'X',0,reason,False,now,session,nxt)
                elif closed and r['direction']==state['direction'] and r['rank']>state['rank'] and not r['blocks'] and quota<3:
                    nxt={**state,'rank':r['rank'],'confirmed':True,'last_alert':r['observed_at']}
                    d['code']='MC' if side==1 else 'SCMP';d['delivery_id']=queue(tx,key,state,r,d['code'],r['rank'],'',False,now,session,nxt);quota+=1
                elif closed and r.get('continuation') in ('CTN_UP','CTN_DOWN') and (r['continuation']=='CTN_UP')==(side==1) and not set(r['blocks'])&{'choppy','wide_whipsaw','chasing'} and (now-stamp(state['last_alert'])).total_seconds()>=1200 and quota<3:
                    nxt={**state,'last_alert':r['observed_at']};d['code']='CTN';d['delivery_id']=queue(tx,key,state,{**r,'direction':state['direction']},'CTN',0,'기존 방향 지속',False,now,session,nxt);quota+=1
                else:
                    if closed and state.get('early') and not state.get('confirmed'):
                        state['confirmed']=True
                    tx.put(key,state)
            else:
                cooldown=state.get('last_alert') and (now-stamp(state['last_alert'])).total_seconds()<1200
                entry=closed and r.get('engine_action') in ENTRY_ACTIONS and r['rank']>0 and not r['blocks']
                early=not closed and early_ok(r)
                if (entry or early) and not cooldown and quota<3:
                    nxt={'active':True,'direction':r['direction'],'entry_price':r['price'],'entry_atr':r['atr'],'extreme':r['price'],'extreme_rsi':r['rsi'],'rank':r['rank'] if entry else 0,'confirmed':bool(entry),'early':bool(early),'observed_at':r['observed_at'],'last_alert':r['observed_at']}
                    d['code']='MC' if r['direction']=='LONG' else 'SCMP';d['delivery_id']=queue(tx,key,state,r,d['code'],r['rank'] if entry else 0,'',early,now,session,nxt);quota+=1
                if cooldown:d['blocks'].append('cooldown')
                if quota>=3 and not d['code']:d['blocks'].append('five_minute_message_limit')
            out.append(d)
        tx.put(quota_key,quota);tx.put(batch,True)
        if type(data.get('bank')) is int and 1<=data['bank']<=7:
            tx.put('mf130-bank:'+str(data['bank']),{'observed_at':now.isoformat(),'kind':kind,'tickers':[r['ticker'] for r in rows]})
        tx.event(batch,day,{'source':'mf130','kind':kind,'received_at':now.isoformat(),'decisions':out})
    return {'status':'ok','decisions':out}

def delivery(store,send,now):
    result=drain(store,send,limit=20,prefix='mf130:',now=now)
    with store.transaction() as tx:
        for r in result:tx.event('mf130-delivery:'+r['id'],now.astimezone(ET).date().isoformat(),{'source':'mf130_delivery','observed_at':now.isoformat(),**r})
    return result
