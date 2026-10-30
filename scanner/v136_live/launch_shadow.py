"""Read-only five-symbol experiment. Uses production gates/risk; NEVER queues messages.
Only completed 1m bars enter this module. 5m context is the last completed candle,
including labelled prior-session warmup at the open. No live-engine state is shared.
"""
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from statistics import mean, median
from collections import Counter
from .engine import Engine, Bar, Position, Config, ET

VERSION = 'LAUNCH_SHADOW_20261029_A'
PREFIX = 'launch-shadow-a:'
SYMBOLS = ('SPY','QQQ','NVDA','TSLA','AMD')
CONFIG = {'candidate_ttl_seconds':120, 'efficiency_min':0.30,
          'max_chase_atr':2.25, 'market':'SPY and QQQ last completed 5m agree',
          'entry':'existing 1m candidate + existing 2m entry gate; 3m validation only',
          'atr':'fixed prior-completed 1m ATR at entry', 'telegram':False}

def iso(t): return t.isoformat() if t else None

def context(engine, ticker, side, now):
    s=engine.states.get(ticker)
    if not s: return False, 'context_missing', None
    w=s.windows[5]
    if not w.latest or w.latest.end>now or len(w.history)<4 or not s.total_volume:
        return False,'context_warmup',None
    # Previous-session context is allowed at the opening, but never a future 5m high/low.
    vwap=s.total_vwap_notional/s.total_volume
    trend=engine._trend(w,vwap)
    closes=[b.close for b in list(w.history)[-4:]]
    distance=sum(abs(b-a) for a,b in zip(closes,closes[1:]))
    efficiency=abs(closes[-1]-closes[0])/distance if distance else 0.0
    detail={'bar_end':iso(w.latest.end),'prior_session':w.latest.end.astimezone(ET).date()!=now.astimezone(ET).date(),'efficiency':efficiency,'trend':trend}
    if trend!=side: return False,'5m_trend',detail
    if efficiency<CONFIG['efficiency_min']: return False,'5m_chop',detail
    return True,'pass',detail

class FastPreview(Engine):
    """Reuse base aggregation, position management and exits. Replace entry scheduling only."""
    def __init__(self,cfg=None):
        super().__init__(cfg); self.candidates={};self.funnel=Counter();self.audit=[];self.entry_meta={};self.market=None
    def _entry_side(self,w,vwap):
        return 0 # base on_bar must not create a second 3m entry; explicit shared gates below
    def recover_gap(self,bar):
        self.candidates.pop(bar.ticker,None);super().recover_gap(bar)
    def on_bar(self,bar,catalyst=None):
        events=super().on_bar(bar,catalyst)
        s=self.states[bar.ticker]; w=s.windows; vwap=s.total_vwap_notional/s.total_volume
        existing=self.candidates.get(bar.ticker)
        if existing and (bar.end-existing[1]).total_seconds()>120:
            self.funnel['candidate_expired']+=1;self.candidates.pop(bar.ticker,None)
        if s.position:return events
        side=Engine._entry_side(self,w[1],vwap)
        if side:
            old=self.candidates.get(bar.ticker)
            if not old or old[0]!=side:self.candidates[bar.ticker]=(side,bar.end)
            self.funnel['1m_candidate_observations']+=1
        candidate=self.candidates.get(bar.ticker)
        if not candidate or not w[2].latest or w[2].latest.end!=bar.end:return events
        direction,first=candidate
        if not bar.end-timedelta(minutes=2)<first<=bar.end:return events
        self.funnel['2m_evaluations']+=1
        reason=None; details={}
        if Engine._entry_side(self,w[2],vwap)!=direction:reason='2m_gate'
        elif s.recovery_until and bar.end<s.recovery_until:reason='data_gap_recovery'
        elif s.cooldown_end and bar.end<s.cooldown_end:reason='exit_cooldown'
        elif bar.end.astimezone(ET).time()>=self.cfg.entry_cutoff_et:reason='entry_cutoff'
        else:
            ok,why,detail=context(self,bar.ticker,direction,bar.end);details['ticker_5m']=detail
            if not ok:reason=why
            if not reason:
                for symbol in ('SPY','QQQ'):
                    ok,why,detail=context(self.market,symbol,direction,bar.end)
                    details[symbol]=detail
                    if not ok:reason='market_'+symbol+'_'+why;break
        self.audit.append({'ticker':bar.ticker,'candidate_time':iso(first),'time':iso(bar.end),'side':direction,'reason':reason or 'would_send','context':details})
        self.funnel[reason or 'would_send']+=1
        if reason:return events
        s.episode+=1;s.position=Position(direction,bar.close,bar.end,s.episode,bar.close);s.opposites.clear()
        self.entry_meta[(bar.ticker,s.episode)]={'candidate_time':iso(first),'context':details}
        events.append(self._event(bar,s,'MC' if direction==1 else 'SCMP',2,'FAST shadow: shared 1m/2m gates + completed 5m context',catalyst))
        self.candidates.pop(bar.ticker,None)
        return events

def to_bar(row):return Bar(row[0],datetime.fromtimestamp(row[1]/1000,timezone.utc),*row[2:])

def feed(engine,bar):
    s=engine.states.get(bar.ticker)
    minute=bar.end.astimezone(ET).hour*60+bar.end.astimezone(ET).minute-570
    if s and s.last_end and s.date==bar.end.astimezone(ET).date():
        if bar.end-s.last_end!=timedelta(minutes=1):engine.recover_gap(bar)
    elif minute!=1:engine.recover_gap(bar)
    return engine.on_bar(bar)

def compare(snapshot, observations=None):
    observations=observations or {};cfg=Config()
    session=snapshot.get('session',{})
    if session.get('close'):
        close=datetime.fromisoformat(session['close'])
        cfg=Config(entry_cutoff_et=min(cfg.entry_cutoff_et,(close-timedelta(minutes=30)).astimezone(ET).time()),flat_alert_et=min(cfg.flat_alert_et,(close-timedelta(minutes=15)).astimezone(ET).time()))
    base=Engine(cfg);fast=FastPreview(cfg);market=Engine(cfg);fast.market=market
    rows=[];by_symbol={};quality={}
    for ticker in SYMBOLS:
        data=snapshot.get('symbols',{}).get(ticker,{})
        bars=data.get('bars',[]); ignored=set(data.get('ignored_backfills',[]))
        rows.extend(r for r in bars if r[1] not in ignored)
        by_symbol[ticker]={r[1]:r for r in bars if r[1] not in ignored}
        quality[ticker]={'bars':len(bars),'ignored_backfills':len(ignored),'seed_bars':len(data.get('seed_bars',[]))}
        seed=[to_bar(r) for r in data.get('seed_bars',[])]
        for engine in (base,fast,market):engine.seed(seed,allow_gaps=True)
    rows.sort(key=lambda r:(r[1],SYMBOLS.index(r[0])))
    entries=[];events=[];active={};first_candidates={};validation=[]
    actual={(e['ticker'],e['time'],e['action']):e for e in snapshot.get('events',[]) if e.get('engine','').startswith('MF_V13_')}
    primed=None
    for row in rows:
        bar=to_bar(row);ticker=bar.ticker
        if primed!=row[1]:
            for market_row in rows:
                if market_row[1]==row[1]:feed(market,to_bar(market_row))
            primed=row[1]
        for label,engine in (('baseline',base),('fast',fast)):
            generated=feed(engine,bar);s=engine.states[ticker];w=s.windows[1]
            vwap=s.total_vwap_notional/s.total_volume
            direction=Engine._entry_side(engine,w,vwap)
            minute=bar.end.astimezone(ET).hour*60+bar.end.astimezone(ET).minute-570
            cycle=(minute-1)//3
            if direction:first_candidates.setdefault((ticker,cycle,direction),bar.end)
            current=active.get((label,ticker))
            if current and bar.end>datetime.fromisoformat(current['condition_met_at']):
                favorable=current['side']*((bar.high if current['side']==1 else bar.low)-current['signal_price'])
                adverse=-current['side']*((bar.low if current['side']==1 else bar.high)-current['signal_price'])
                current['mfe_atr']=max(current['mfe_atr'],favorable/current['atr'])
                current['mae_atr']=max(current['mae_atr'],adverse/current['atr'])
            if label=='fast' and current and s.windows[3].latest and s.windows[3].latest.end==bar.end and not current.get('3m_checked_at'):
                current['3m_checked_at']=iso(bar.end)
                current['3m_same_direction']=Engine._entry_side(engine,s.windows[3],vwap)==current['side']
            for ev in generated:
                key=(label,ticker)
                if ev.action in ('MC','SCMP'):
                    atr=w.previous_atr
                    if not atr or atr<=0:continue
                    meta=fast.entry_meta.get((ticker,ev.episode),{}) if label=='fast' else {}
                    first=meta.get('candidate_time') or iso(first_candidates.get((ticker,cycle,ev.side),bar.end))
                    record={'signal_id':f'{VERSION}:{snapshot["day"]}:BATS:{ticker}:{label}:{ev.episode}',
                        'ticker':ticker,'path':label,'side':ev.side,'candidate_detected_at':first,
                        'condition_met_at':iso(ev.time),'candidate_observed_at':observations.get(f'{ticker}:{int(datetime.fromisoformat(first).timestamp()*1000)}',{}).get('received_at'),'first_observed_at':observations.get(f'{ticker}:{row[1]}',{}).get('received_at'),
                        'telegram_sent_at':None,'telegram_status':'shadow_not_sent' if label=='fast' else 'not_available',
                        'signal_price':ev.price,'atr':atr,'chase_atr':abs(ev.price-w.ema9)/atr,
                        'mfe_atr':0.0,'mae_atr':0.0,'context':meta.get('context'),
                        'exit_at':None,'exit_price':None,'exit_pnl_atr':None,'giveback_atr':None}
                    source=actual.get((ticker,iso(ev.time),ev.action))
                    if source:record['live_received_at']=source.get('received_at')
                    if label=='fast' and s.windows[3].latest and s.windows[3].latest.end==bar.end:
                        record['3m_checked_at']=iso(bar.end)
                        record['3m_same_direction']=Engine._entry_side(engine,s.windows[3],vwap)==ev.side
                    entries.append(record);active[key]=record
                linked=active.get(key)
                record=asdict(ev);record['time']=iso(ev.time);record['path']=label;record['signal_id']=linked['signal_id'] if linked else None
                if linked:
                    progress=ev.side*(ev.price-linked['signal_price'])/linked['atr']
                    record['progress_atr']=progress
                    if ev.action=='CTN':record['meaning']='CONTINUATION_PROGRESS' if progress>=0.5 else 'TREND_ONLY_NOT_NEW_ENTRY'
                    if ev.action.endswith('_X'):
                        linked.update(exit_at=iso(ev.time),exit_price=ev.price,exit_pnl_atr=progress,giveback_atr=max(0,linked['mfe_atr']-progress))
                        active.pop(key,None)
                events.append(record)
    for record in entries:
        t=datetime.fromisoformat(record['condition_met_at']);series=by_symbol[record['ticker']]
        for n in (5,10):
            target=int((t+timedelta(minutes=n)).timestamp()*1000);r=series.get(target)
            record[f'change_{n}m_atr']=record['side']*(r[5]-record['signal_price'])/record['atr'] if r else None
            record[f'change_{n}m_price']=record['side']*(r[5]-record['signal_price']) if r else None
            window=[v for stamp,v in series.items() if t.timestamp()*1000<stamp<=target]
            record[f'observed_{n}m_minutes']=len(window)
            complete=len(window)==n
            record[f'mfe_{n}m_atr']=max([0]+[record['side']*((v[3] if record['side']==1 else v[4])-record['signal_price'])/record['atr'] for v in window]) if complete else None
            record[f'mae_{n}m_atr']=max([0]+[-record['side']*((v[4] if record['side']==1 else v[3])-record['signal_price'])/record['atr'] for v in window]) if complete else None
        record['candidate_to_entry_seconds']=(t-datetime.fromisoformat(record['candidate_detected_at'])).total_seconds()
    # Pair only same ticker/direction AND exact initial candidate; no nearest-time cherry-picking.
    pairs=[]
    for a in (r for r in entries if r['path']=='baseline'):
        matches=[b for b in entries if b['path']=='fast' and (b['ticker'],b['side'],b['candidate_detected_at'])==(a['ticker'],a['side'],a['candidate_detected_at'])]
        if len(matches)==1:
            b=matches[0];pairs.append({'baseline_id':a['signal_id'],'fast_id':b['signal_id'],'seconds_saved':(datetime.fromisoformat(a['condition_met_at'])-datetime.fromisoformat(b['condition_met_at'])).total_seconds()})
    savings=[p['seconds_saved'] for p in pairs]
    return {'version':VERSION,'day':snapshot['day'],'config':CONFIG,'quality':quality,'funnel':dict(fast.funnel),
        'entries':entries,'events':events,'candidate_audit':fast.audit,'pairs':pairs,
        'latency_reduction':{'matched_count':len(pairs),'mean_seconds':mean(savings) if savings else None,'median_seconds':median(savings) if savings else None},
        'limitations':['Completed 1m OHLCV only; no intraminute event ordering assumed.',
          'MFE/MAE until simulated exit or latest available bar; fixed prior 1m ATR, no fills/options PnL.',
          'Historical receipt/send timestamps unavailable unless explicitly recorded; no inferred send time.',
          'Cold-start and prior-session 5m context are retained and disclosed; opening reversals may be blocked.',
          'Condition equivalence is to server V13.39, not legacy Pine chart engine.']}

def capture(store,now,emitted_ms=None):
    """Best effort after live delivery; read source snapshot, compute OUTSIDE DB lock.
    No enqueue/send/control calls. Errors cannot change live response or engine state.
    """
    from .runtime import diagnostic_export, schedule
    try:
        snapshot=diagnostic_export(store,now);day=snapshot['day']
        snapshot['session']=schedule(day)
        with store.transaction() as tx:
            obs=tx.get(PREFIX+day+':observations',{})
            for ticker in SYMBOLS:
                for row in snapshot.get('symbols',{}).get(ticker,{}).get('bars',[]):
                    # Historical rows are never falsely labelled as live observations.
                    age=now.timestamp()-row[1]/1000
                    if 0<=age<=120:obs.setdefault(f'{ticker}:{row[1]}',{'received_at':iso(now),'webhook_emitted_ms':emitted_ms})
            tx.put(PREFIX+day+':observations',obs)
        result=compare(snapshot,obs)
        with store.transaction() as tx:
            journal=tx.get(PREFIX+day+':delivery',{})
        for entry in result['entries']:
            if entry['path']!='baseline':continue
            action='MC' if entry['side']==1 else 'SCMP'
            stamp=int(datetime.fromisoformat(entry['condition_met_at']).timestamp()*1000)
            matches=[v for k,v in journal.items() if f':{entry["ticker"]}:' in k and k.endswith(f':{action}:{stamp}')]
            if len(matches)==1:
                entry['telegram_status']=matches[0]['status']
                entry['telegram_sent_at']=matches[0].get('send_completed_at') if matches[0]['status']=='sent' else None
        result['computed_at']=iso(datetime.now(timezone.utc));result['mode']='shadow_no_telegram'
        with store.transaction() as tx:tx.put(PREFIX+day+':report',result)
        return {'ok':True,'day':day,'entries':len(result['entries'])}
    except Exception as exc:
        # No raw exception text: it may contain credentials/SQL parameters.
        with store.transaction() as tx:tx.put(PREFIX+'error',{'at':iso(now),'type':type(exc).__name__})
        return {'ok':False,'error_type':type(exc).__name__}

def status(store,now):
    day=now.astimezone(ET).date().isoformat()
    with store.transaction() as tx:return {'report':tx.get(PREFIX+day+':report'),'error':tx.get(PREFIX+'error')}


def deliver_and_record(store,send,now):
    from .runtime import delivery
    result=delivery(store,send,now) # exact existing behavior, once only
    try:
        with store.transaction() as tx:
            key=PREFIX+now.astimezone(ET).date().isoformat()+':delivery'
            journal=tx.get(key,{})
            for row in result:journal[row['id']]=row
            tx.put(key,journal)
    except Exception:
        pass # diagnostics never retries Telegram or changes the delivery result
    return result
