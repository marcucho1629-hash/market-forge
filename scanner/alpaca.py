"""Read-only Alpaca REST adapter. No broker order endpoints or implicit feed fallback."""
import hashlib
import json
import os
import re
import time as clock
from datetime import date, datetime, time, timedelta, timezone
from statistics import mean
import requests
from .core import Bar, ET, finite, stamp

DATA_URL = 'https://data.alpaca.markets'
CALENDAR_HOSTS = {'https://paper-api.alpaca.markets', 'https://api.alpaca.markets'}


def tickers(value):
    result = sorted(set(x.strip().upper() for x in value.split(',') if x.strip()))
    if any(not re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,14}', x) for x in result):
        raise ValueError('Invalid ticker in scanner configuration')
    return result


class AlpacaError(RuntimeError):
    """Message is safe to return; never contains keys, response text or auth headers."""


class AlpacaProvider:
    def __init__(self, store=None, http=None):
        key = os.getenv('APCA_API_KEY_ID', '')
        secret = os.getenv('APCA_API_SECRET_KEY', '')
        if not key or not secret:
            raise AlpacaError('Set APCA_API_KEY_ID and APCA_API_SECRET_KEY')
        self.feed = os.getenv('ALPACA_DATA_FEED', 'sip')
        if self.feed not in {'sip', 'iex'}:
            raise AlpacaError('ALPACA_DATA_FEED must be sip or iex; delayed feeds are not supported')
        self.base = os.getenv('APCA_API_BASE_URL', 'https://paper-api.alpaca.markets').rstrip('/')
        if self.base not in CALENDAR_HOSTS:
            raise AlpacaError('APCA_API_BASE_URL must be an official Alpaca paper or live host')
        self.master = tickers(os.getenv('SCANNER_MASTER_UNIVERSE', ''))
        if len(self.master)>100:
            raise AlpacaError('This serverless adapter supports at most 100 Master Universe symbols')
        self.sectors = json.loads(os.getenv('SCANNER_SECTOR_MAP', '{}'))
        if not isinstance(self.sectors, dict) or any(
            not isinstance(k,str) or not isinstance(v,str) or tickers(k)!=[k] or tickers(v)!=[v]
            for k,v in self.sectors.items()):
            raise AlpacaError('SCANNER_SECTOR_MAP must map uppercase stock tickers to sector ETF tickers')
        self.lookback = int(os.getenv('ALPACA_BASELINE_SESSIONS', '5'))
        if not 3<=self.lookback<=20: raise AlpacaError('Baseline sessions must be 3..20')
        self.lag = int(os.getenv('ALPACA_BAR_SETTLE_SECONDS', '15'))
        if not 0<=self.lag<=60: raise AlpacaError('Bar settle seconds must be 0..60')
        self.store, self.http = store, http or requests.Session()
        self.headers = {'APCA-API-KEY-ID':key, 'APCA-API-SECRET-KEY':secret}
        self.deadline = None

    def get(self, url, params):
        if self.deadline and clock.monotonic()>=self.deadline:
            raise AlpacaError('Alpaca collection exceeded the request time budget; retry the scheduled run')
        try:
            r = self.http.get(url, params=params, headers=self.headers,
                              timeout=(5,15), allow_redirects=False)
        except requests.RequestException:
            raise AlpacaError('Alpaca connection failed') from None
        if r.status_code != 200:
            if r.status_code==403:
                raise AlpacaError(f'Alpaca denied access to the configured {self.feed} feed or calendar; verify entitlement')
            if r.status_code==429: raise AlpacaError('Alpaca rate limit reached; retry on the next scheduled run')
            raise AlpacaError(f'Alpaca request failed with HTTP {r.status_code}')
        try: return r.json()
        except ValueError: raise AlpacaError('Alpaca returned invalid JSON') from None

    def cache(self, key, factory):
        if self.store:
            with self.store.transaction() as tx: value=tx.get('alpaca:'+key)
            if value is not None: return value
        value=factory()
        if self.store:
            with self.store.transaction() as tx: tx.put('alpaca:'+key,value)
        return value

    def calendar(self, day):
        def load():
            raw=self.get(self.base+'/v2/calendar',{'start':(day-timedelta(days=60)).isoformat(),'end':day.isoformat()})
            if not isinstance(raw,list): raise AlpacaError('Invalid Alpaca calendar')
            result=[]
            for row in raw:
                dt=date.fromisoformat(row['date'])
                op=datetime.combine(dt,time.fromisoformat(row['open']),ET).astimezone(timezone.utc)
                cl=datetime.combine(dt,time.fromisoformat(row['close']),ET).astimezone(timezone.utc)
                if not op<cl: raise AlpacaError('Invalid calendar session boundaries')
                result.append({'date':row['date'],'is_open':True,'open':op.isoformat(),'close':cl.isoformat()})
            return sorted(result,key=lambda x:x['date'])
        return self.cache('calendar:'+day.isoformat(),load)

    def bars(self, symbols, minutes, start, cutoff, asof_day):
        """Consume every page, convert interval starts to ends, drop unfinished bars."""
        if not symbols: return {}
        params={'symbols':','.join(sorted(set(symbols))), 'timeframe':f'{minutes}Min',
                'start':start.isoformat(),'end':cutoff.isoformat(), 'limit':10000,
                'feed':self.feed,'adjustment':'split','asof':asof_day,'sort':'asc'}
        result={s:[] for s in symbols}
        tokens=set()
        for _ in range(100):
            raw=self.get(DATA_URL+'/v2/stocks/bars',params)
            if not isinstance(raw.get('bars'),dict): raise AlpacaError('Alpaca bars response missing bars object')
            for symbol,rows in raw['bars'].items():
                if symbol not in result: raise AlpacaError('Unexpected symbol in Alpaca bars')
                for r in rows:
                    begin=stamp(r['t'])
                    end=begin+timedelta(minutes=minutes)
                    if begin<start or end>cutoff: continue
                    b={'end':end.isoformat(),'open':r['o'],'high':r['h'],'low':r['l'],
                       'close':r['c'],'volume':r['v']}
                    Bar.parse(b)
                    if end.second or end.microsecond or end.minute%minutes:
                        raise AlpacaError('Misaligned Alpaca bar timestamp')
                    result[symbol].append(b)
            token=raw.get('next_page_token')
            if not token: break
            if token in tokens: raise AlpacaError('Repeated Alpaca pagination token')
            tokens.add(token)
            params['page_token']=token
        else: raise AlpacaError('Alpaca pagination exceeded limit')
        for symbol,rows in result.items():
            rows.sort(key=lambda b:b['end'])
            if len({b['end'] for b in rows})!=len(rows):
                raise AlpacaError('Duplicate bars across Alpaca pages')
        return result

    def history(self, symbols, previous, day):
        """Prior-session 5m cache, reusable when master selection narrows to Top20."""
        tag=f'history-v1:{day}:{self.feed}:{self.lookback}:'
        cached={}
        if self.store:
            with self.store.transaction() as tx:
                cached={s:tx.get('alpaca:'+tag+s) for s in symbols}
        missing=[s for s in symbols if cached.get(s) is None]
        for offset in range(0,len(missing),20):
            chunk=missing[offset:offset+20]
            start=datetime.combine(date.fromisoformat(previous[0]['date']),time(4),ET).astimezone(timezone.utc)
            rows=self.bars(chunk,5,start,stamp(previous[-1]['close']),day)
            cached.update(rows)
            if self.store:
                with self.store.transaction() as tx:
                    for symbol in chunk: tx.put('alpaca:'+tag+symbol,rows[symbol])
        return cached

    @staticmethod
    def regular(rows, sessions):
        bounds={s['date']:(stamp(s['open']),stamp(s['close'])) for s in sessions}
        return [b for b in rows if (bound:=bounds.get(stamp(b['end']).astimezone(ET).date().isoformat()))
                and bound[0]<stamp(b['end'])<=bound[1]]

    def catalysts(self, symbols, day, cutoff):
        if not self.store: return {}
        with self.store.transaction() as tx:
            return {s:v for s in symbols if (v:=tx.get(f'catalyst:{day}:{s}')) and stamp(v['asof'])<=cutoff}

    def opening_baselines(self, symbols, previous, day, slot):
        key=f'opening-v1:{day}:{self.feed}:{self.lookback}:'+hashlib.sha256(','.join(symbols).encode()).hexdigest()
        def load():
            values={s:{} for s in symbols}
            for sess in previous:
                op=stamp(sess['open'])
                rows=self.bars(symbols,1,op,op+timedelta(minutes=5),day)
                for symbol,seq in rows.items():
                    for b in seq:
                        n=int((stamp(b['end'])-op).total_seconds()/60)
                        values[symbol].setdefault(str(n),[]).append(b['volume'])
            return values
        values=self.cache(key,load)
        return {s:mean(v) for s in symbols if len(v:=values[s].get(str(slot),[]))>=3 and mean(v)>0}

    def fetch(self,kind,now,symbols=None):
        self.deadline=clock.monotonic()+50
        day=now.astimezone(ET).date()
        sessions=self.calendar(day)
        today=next((s for s in sessions if s['date']==day.isoformat()),None)
        payload={'asof':(now-timedelta(seconds=self.lag)).isoformat(),
                 'session':today or {'date':day.isoformat(),'is_open':False},
                 'data_source':{'provider':'alpaca','feed':self.feed,'adjustment':'split',
                                'settle_seconds':self.lag,'baseline_sessions':self.lookback}}
        if today is None: return payload
        op,cl=stamp(today['open']),stamp(today['close'])
        cutoff=min(stamp(payload['asof']),cl)
        previous=[s for s in sessions if s['date']<day.isoformat()][-self.lookback:]
        if kind not in {'premarket','scan'}: raise ValueError('Unknown Alpaca operation')
        if kind=='premarket' and not op-timedelta(minutes=45)<=now<op: return payload
        if kind=='scan' and not op<now<=cl+timedelta(minutes=2): return payload
        if len(previous)<self.lookback: raise AlpacaError('Insufficient calendar history for volume baseline')
        if kind=='premarket':
            if not self.master: raise AlpacaError('Set SCANNER_MASTER_UNIVERSE before automatic selection')
            return self.premarket(payload,previous,cutoff,day)
        selected=tickers(','.join(symbols or []))
        if not selected or len(selected)>20: raise AlpacaError('Scan requires today selected 1..20 symbols')
        if cutoff<=op:
            return payload  # service returns awaiting_closed_bar before accessing symbols
        return self.scan(payload,selected,previous,cutoff,day,now)

    def diagnose_spy(self, day, now):
        """Read-only historical check; bypass cache and never select or send signals."""
        if self.store is not None:
            raise ValueError('Diagnostics must use an uncached provider')
        if day > now.astimezone(ET).date():
            raise ValueError('Diagnostic date cannot be in the future')
        self.deadline = clock.monotonic()+25
        sessions = self.calendar(day)
        today = next((s for s in sessions if s['date']==day.isoformat()), None)
        previous = [s for s in sessions if s['date']<day.isoformat()]
        if not today or not previous:
            return {'status':'no_session_or_previous_session','date':day.isoformat()}
        prior = previous[-1]
        old = self.bars(['SPY'],5,stamp(prior['open']),stamp(prior['close']),day.isoformat())['SPY']
        start = datetime.combine(day,time(4),ET).astimezone(timezone.utc)
        cutoff = min(datetime.combine(day,time(9,20),ET).astimezone(timezone.utc),
                     now-timedelta(seconds=self.lag))
        cutoff = cutoff.replace(minute=cutoff.minute//5*5,second=0,microsecond=0)
        current = self.bars(['SPY'],5,start,cutoff,day.isoformat())['SPY'] if cutoff>start else []
        close_ok = bool(old and stamp(old[-1]['end'])==stamp(prior['close']))
        fresh = bool(current and cutoff-stamp(current[-1]['end'])<=timedelta(minutes=5))
        return {'status':'ok' if close_ok and fresh else 'missing_benchmark',
                'date':day.isoformat(),'feed':self.feed,'cache_bypassed':True,
                'previous_session':prior['date'],'expected_close_end':prior['close'],
                'previous_bar_count':len(old),'previous_last_end':old[-1]['end'] if old else None,
                'previous_close_ok':close_ok,'premarket_cutoff':cutoff.isoformat(),
                'premarket_bar_count':len(current),
                'premarket_last_end':current[-1]['end'] if current else None,
                'premarket_fresh':fresh,
                'note':'Historical availability now does not prove availability at the original run time.'}

    def premarket(self,payload,previous,cutoff,day):
        # Match cumulative volumes at the same completed 5m boundary each prior day.
        cutoff=cutoff.replace(minute=cutoff.minute//5*5,second=0,microsecond=0)
        symbols=sorted(set(self.master+['SPY']+[self.sectors[s] for s in self.master if s in self.sectors]))
        history=self.history(symbols,previous,day.isoformat())
        start=datetime.combine(day,time(4),ET).astimezone(timezone.utc)
        current=self.bars(symbols,5,start,cutoff,day.isoformat())
        changes={}
        last_close={}
        for s in symbols:
            old=self.regular(history[s],[previous[-1]])
            seq=current[s]
            if not old or stamp(old[-1]['end'])!=stamp(previous[-1]['close']): continue
            if not seq or cutoff-stamp(seq[-1]['end'])>timedelta(minutes=5): continue
            last_close[s]=finite(old[-1]['close'],.00001)
            changes[s]=100*(seq[-1]['close']/last_close[s]-1)
        if 'SPY' not in changes:
            old = self.regular(history['SPY'],[previous[-1]])
            seq = current['SPY']
            raise AlpacaError('SPY benchmark unavailable: '+json.dumps({
                'feed':self.feed,'expected_close_end':previous[-1]['close'],
                'previous_last_end':old[-1]['end'] if old else None,
                'premarket_cutoff':cutoff.isoformat(),
                'premarket_last_end':seq[-1]['end'] if seq else None},separators=(',',':')))
        catalysts=self.catalysts(self.master,day.isoformat(),cutoff)
        rows=[]
        excluded={}
        cutoff_time=cutoff.astimezone(ET).timetz().replace(tzinfo=None)
        for s in self.master:
            if s not in changes:
                excluded[s]='missing_or_stale_premarket_or_previous_close'
                continue
            volumes=[]
            for sess in previous:
                dt=date.fromisoformat(sess['date'])
                a=datetime.combine(dt,time(4),ET).astimezone(timezone.utc)
                b=datetime.combine(dt,cutoff_time,ET).astimezone(timezone.utc)
                seq=[x for x in history[s] if a<stamp(x['end'])<=b]
                if seq: volumes.append(sum(x['volume'] for x in seq))
            if len(volumes)<3 or mean(volumes)<=0:
                excluded[s]='insufficient_same_time_premarket_baseline'
                continue
            sector=self.sectors.get(s)
            row={'ticker':s,'asof':current[s][-1]['end'],'price':current[s][-1]['close'],
                 'previous_close':last_close[s],'premarket_volume':sum(x['volume'] for x in current[s]),
                 'average_premarket_volume':mean(volumes),'relative_strength_pct':changes[s]-changes['SPY'],
                 'sector_change_pct':changes.get(sector,0), 'sector_available':sector in changes}
            if s in catalysts: row['catalyst']=catalysts[s]
            rows.append(row)
        payload['universe']=rows
        payload['excluded']=excluded
        return payload

    def scan(self,payload,selected,previous,cutoff,day,now):
        op,cl=stamp(payload['session']['open']),stamp(payload['session']['close'])
        symbols=sorted(set(selected+['SPY','QQQ']+[self.sectors[s] for s in selected if s in self.sectors]))
        history=self.history(symbols,previous,day.isoformat())
        current=self.bars(symbols,5,op,cutoff,day.isoformat())
        bars5={s:self.regular(history[s],previous)[-40:]+current[s] for s in symbols}
        for s in selected:
            if len(self.regular(history[s],previous))<30:
                raise AlpacaError(f'{s}: insufficient prior regular-session warmup')
        catalysts=self.catalysts(selected,day.isoformat(),cutoff)
        payload['symbols']={s:{'bars_5m':bars5[s]} for s in selected}
        payload['benchmarks']={s:bars5[s] for s in symbols if s in {'SPY','QQQ'} or s in self.sectors.values()}
        for s,item in payload['symbols'].items():
            if s in self.sectors: item['sector_etf']=self.sectors[s]
            if s in catalysts: item['catalyst']=catalysts[s]
        if now<op+timedelta(minutes=5):
            slot=int((cutoff-op).total_seconds()//60)
            if slot<=0: return payload
            minute=self.bars(selected,1,op,cutoff,day.isoformat())
            baselines=self.opening_baselines(selected,previous,day.isoformat(),slot)
            for s,item in payload['symbols'].items():
                item['bars_1m']=minute[s]
                if s in baselines: item['opening_volume_baseline']=baselines[s]
        return payload
        if self.base not in CALENDAR_HOSTS:
            raise AlpacaError('APCA_API_BASE_URL must be an official Alpaca paper or live host')
        self.master = tickers(os.getenv('SCANNER_MASTER_UNIVERSE', ''))
        if len(self.master)>100:
            raise AlpacaError('This serverless adapter supports at most 100 Master Universe symbols')
        self.sectors = json.loads(os.getenv('SCANNER_SECTOR_MAP', '{}'))
        if not isinstance(self.sectors, dict) or any(
            not isinstance(k,str) or not isinstance(v,str) or tickers(k)!=[k] or tickers(v)!=[v]
            for k,v in self.sectors.items()):
            raise AlpacaError('SCANNER_SECTOR_MAP must map uppercase stock tickers to sector ETF tickers')
        self.lookback = int(os.getenv('ALPACA_BASELINE_SESSIONS', '5'))
        if not 3<=self.lookback<=20: raise AlpacaError('Baseline sessions must be 3..20')
        self.lag = int(os.getenv('ALPACA_BAR_SETTLE_SECONDS', '15'))
        if not 0<=self.lag<=60: raise AlpacaError('Bar settle seconds must be 0..60')
        self.store, self.http = store, http or requests.Session()
        self.headers = {'APCA-API-KEY-ID':key, 'APCA-API-SECRET-KEY':secret}
        self.deadline = None

    def get(self, url, params):
        if self.deadline and clock.monotonic()>=self.deadline:
            raise AlpacaError('Alpaca collection exceeded the request time budget; retry the scheduled run')
        try:
            r = self.http.get(url, params=params, headers=self.headers,
                              timeout=(5,15), allow_redirects=False)
        except requests.RequestException:
            raise AlpacaError('Alpaca connection failed') from None
        if r.status_code != 200:
            if r.status_code==403:
                raise AlpacaError(f'Alpaca denied access to the configured {self.feed} feed or calendar; verify entitlement')
            if r.status_code==429: raise AlpacaError('Alpaca rate limit reached; retry on the next scheduled run')
            raise AlpacaError(f'Alpaca request failed with HTTP {r.status_code}')
        try: return r.json()
        except ValueError: raise AlpacaError('Alpaca returned invalid JSON') from None

    def cache(self, key, factory):
        if self.store:
            with self.store.transaction() as tx: value=tx.get('alpaca:'+key)
            if value is not None: return value
        value=factory()
        if self.store:
            with self.store.transaction() as tx: tx.put('alpaca:'+key,value)
        return value

    def calendar(self, day):
        def load():
            raw=self.get(self.base+'/v2/calendar',{'start':(day-timedelta(days=60)).isoformat(),'end':day.isoformat()})
            if not isinstance(raw,list): raise AlpacaError('Invalid Alpaca calendar')
            result=[]
            for row in raw:
                dt=date.fromisoformat(row['date'])
                op=datetime.combine(dt,time.fromisoformat(row['open']),ET).astimezone(timezone.utc)
                cl=datetime.combine(dt,time.fromisoformat(row['close']),ET).astimezone(timezone.utc)
                if not op<cl: raise AlpacaError('Invalid calendar session boundaries')
                result.append({'date':row['date'],'is_open':True,'open':op.isoformat(),'close':cl.isoformat()})
            return sorted(result,key=lambda x:x['date'])
        return self.cache('calendar:'+day.isoformat(),load)

    def bars(self, symbols, minutes, start, cutoff, asof_day):
        """Consume every page, convert interval starts to ends, drop unfinished bars."""
        if not symbols: return {}
        params={'symbols':','.join(sorted(set(symbols))), 'timeframe':f'{minutes}Min',
                'start':start.isoformat(),'end':cutoff.isoformat(), 'limit':10000,
                'feed':self.feed,'adjustment':'split','asof':asof_day,'sort':'asc'}
        result={s:[] for s in symbols}
        tokens=set()
        for _ in range(100):
            raw=self.get(DATA_URL+'/v2/stocks/bars',params)
            if not isinstance(raw.get('bars'),dict): raise AlpacaError('Alpaca bars response missing bars object')
            for symbol,rows in raw['bars'].items():
                if symbol not in result: raise AlpacaError('Unexpected symbol in Alpaca bars')
                for r in rows:
                    begin=stamp(r['t'])
                    end=begin+timedelta(minutes=minutes)
                    if begin<start or end>cutoff: continue
                    b={'end':end.isoformat(),'open':r['o'],'high':r['h'],'low':r['l'],
                       'close':r['c'],'volume':r['v']}
                    Bar.parse(b)
                    if end.second or end.microsecond or end.minute%minutes:
                        raise AlpacaError('Misaligned Alpaca bar timestamp')
                    result[symbol].append(b)
            token=raw.get('next_page_token')
            if not token: break
            if token in tokens: raise AlpacaError('Repeated Alpaca pagination token')
            tokens.add(token)
            params['page_token']=token
        else: raise AlpacaError('Alpaca pagination exceeded limit')
        for symbol,rows in result.items():
            rows.sort(key=lambda b:b['end'])
            if len({b['end'] for b in rows})!=len(rows):
                raise AlpacaError('Duplicate bars across Alpaca pages')
        return result

    def history(self, symbols, previous, day):
        """Prior-session 5m cache, reusable when master selection narrows to Top20."""
        tag=f'history-v1:{day}:{self.feed}:{self.lookback}:'
        cached={}
        if self.store:
            with self.store.transaction() as tx:
                cached={s:tx.get('alpaca:'+tag+s) for s in symbols}
        missing=[s for s in symbols if cached.get(s) is None]
        for offset in range(0,len(missing),20):
            chunk=missing[offset:offset+20]
            start=datetime.combine(date.fromisoformat(previous[0]['date']),time(4),ET).astimezone(timezone.utc)
            rows=self.bars(chunk,5,start,stamp(previous[-1]['close']),day)
            cached.update(rows)
            if self.store:
                with self.store.transaction() as tx:
                    for symbol in chunk: tx.put('alpaca:'+tag+symbol,rows[symbol])
        return cached

    @staticmethod
    def regular(rows, sessions):
        bounds={s['date']:(stamp(s['open']),stamp(s['close'])) for s in sessions}
        return [b for b in rows if (bound:=bounds.get(stamp(b['end']).astimezone(ET).date().isoformat()))
                and bound[0]<stamp(b['end'])<=bound[1]]

    def catalysts(self, symbols, day, cutoff):
        if not self.store: return {}
        with self.store.transaction() as tx:
            return {s:v for s in symbols if (v:=tx.get(f'catalyst:{day}:{s}')) and stamp(v['asof'])<=cutoff}

    def opening_baselines(self, symbols, previous, day, slot):
        key=f'opening-v1:{day}:{self.feed}:{self.lookback}:'+hashlib.sha256(','.join(symbols).encode()).hexdigest()
        def load():
            values={s:{} for s in symbols}
            for sess in previous:
                op=stamp(sess['open'])
                rows=self.bars(symbols,1,op,op+timedelta(minutes=5),day)
                for symbol,seq in rows.items():
                    for b in seq:
                        n=int((stamp(b['end'])-op).total_seconds()/60)
                        values[symbol].setdefault(str(n),[]).append(b['volume'])
            return values
        values=self.cache(key,load)
        return {s:mean(v) for s in symbols if len(v:=values[s].get(str(slot),[]))>=3 and mean(v)>0}

    def fetch(self,kind,now,symbols=None):
        self.deadline=clock.monotonic()+50
        day=now.astimezone(ET).date()
        sessions=self.calendar(day)
        today=next((s for s in sessions if s['date']==day.isoformat()),None)
        payload={'asof':(now-timedelta(seconds=self.lag)).isoformat(),
                 'session':today or {'date':day.isoformat(),'is_open':False},
                 'data_source':{'provider':'alpaca','feed':self.feed,'adjustment':'split',
                                'settle_seconds':self.lag,'baseline_sessions':self.lookback}}
        if today is None: return payload
        op,cl=stamp(today['open']),stamp(today['close'])
        cutoff=min(stamp(payload['asof']),cl)
        previous=[s for s in sessions if s['date']<day.isoformat()][-self.lookback:]
        if kind not in {'premarket','scan'}: raise ValueError('Unknown Alpaca operation')
        if kind=='premarket' and not op-timedelta(minutes=45)<=now<op: return payload
        if kind=='scan' and not op<now<=cl+timedelta(minutes=2): return payload
        if len(previous)<self.lookback: raise AlpacaError('Insufficient calendar history for volume baseline')
        if kind=='premarket':
            if not self.master: raise AlpacaError('Set SCANNER_MASTER_UNIVERSE before automatic selection')
            return self.premarket(payload,previous,cutoff,day)
        selected=tickers(','.join(symbols or []))
        if not selected or len(selected)>20: raise AlpacaError('Scan requires today selected 1..20 symbols')
        if cutoff<=op:
            return payload  # service returns awaiting_closed_bar before accessing symbols
        return self.scan(payload,selected,previous,cutoff,day,now)

    def premarket(self,payload,previous,cutoff,day):
        # Match cumulative volumes at the same completed 5m boundary each prior day.
        cutoff=cutoff.replace(minute=cutoff.minute//5*5,second=0,microsecond=0)
        symbols=sorted(set(self.master+['SPY']+[self.sectors[s] for s in self.master if s in self.sectors]))
        history=self.history(symbols,previous,day.isoformat())
        start=datetime.combine(day,time(4),ET).astimezone(timezone.utc)
        current=self.bars(symbols,5,start,cutoff,day.isoformat())
        changes={}
        last_close={}
        for s in symbols:
            old=self.regular(history[s],[previous[-1]])
            seq=current[s]
            if not old or stamp(old[-1]['end'])!=stamp(previous[-1]['close']): continue
            if not seq or cutoff-stamp(seq[-1]['end'])>timedelta(minutes=5): continue
            last_close[s]=finite(old[-1]['close'],.00001)
            changes[s]=100*(seq[-1]['close']/last_close[s]-1)
        if 'SPY' not in changes: raise AlpacaError('Fresh SPY benchmark or previous session close missing')
        catalysts=self.catalysts(self.master,day.isoformat(),cutoff)
        rows=[]
        excluded={}
        cutoff_time=cutoff.astimezone(ET).timetz().replace(tzinfo=None)
        for s in self.master:
            if s not in changes:
                excluded[s]='missing_or_stale_premarket_or_previous_close'
                continue
            volumes=[]
            for sess in previous:
                dt=date.fromisoformat(sess['date'])
                a=datetime.combine(dt,time(4),ET).astimezone(timezone.utc)
                b=datetime.combine(dt,cutoff_time,ET).astimezone(timezone.utc)
                seq=[x for x in history[s] if a<stamp(x['end'])<=b]
                if seq: volumes.append(sum(x['volume'] for x in seq))
            if len(volumes)<3 or mean(volumes)<=0:
                excluded[s]='insufficient_same_time_premarket_baseline'
                continue
            sector=self.sectors.get(s)
            row={'ticker':s,'asof':current[s][-1]['end'],'price':current[s][-1]['close'],
                 'previous_close':last_close[s],'premarket_volume':sum(x['volume'] for x in current[s]),
                 'average_premarket_volume':mean(volumes),'relative_strength_pct':changes[s]-changes['SPY'],
                 'sector_change_pct':changes.get(sector,0), 'sector_available':sector in changes}
            if s in catalysts: row['catalyst']=catalysts[s]
            rows.append(row)
        payload['universe']=rows
        payload['excluded']=excluded
        return payload

    def scan(self,payload,selected,previous,cutoff,day,now):
        op,cl=stamp(payload['session']['open']),stamp(payload['session']['close'])
        symbols=sorted(set(selected+['SPY','QQQ']+[self.sectors[s] for s in selected if s in self.sectors]))
        history=self.history(symbols,previous,day.isoformat())
        current=self.bars(symbols,5,op,cutoff,day.isoformat())
        bars5={s:self.regular(history[s],previous)[-40:]+current[s] for s in symbols}
        for s in selected:
            if len(self.regular(history[s],previous))<30:
                raise AlpacaError(f'{s}: insufficient prior regular-session warmup')
        catalysts=self.catalysts(selected,day.isoformat(),cutoff)
        payload['symbols']={s:{'bars_5m':bars5[s]} for s in selected}
        payload['benchmarks']={s:bars5[s] for s in symbols if s in {'SPY','QQQ'} or s in self.sectors.values()}
        for s,item in payload['symbols'].items():
            if s in self.sectors: item['sector_etf']=self.sectors[s]
            if s in catalysts: item['catalyst']=catalysts[s]
        if now<op+timedelta(minutes=5):
            slot=int((cutoff-op).total_seconds()//60)
            if slot<=0: return payload
            minute=self.bars(selected,1,op,cutoff,day.isoformat())
            baselines=self.opening_baselines(selected,previous,day.isoformat(),slot)
            for s,item in payload['symbols'].items():
                item['bars_1m']=minute[s]
                if s in baselines: item['opening_volume_baseline']=baselines[s]
        return payload
