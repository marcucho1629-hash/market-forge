"""Five-symbol IEX paper test. No news, SIP fallback, orders or paid endpoints."""
import hmac, html, importlib.util, json, os, uuid
from datetime import datetime,timedelta,timezone
from pathlib import Path
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo
import requests
from fastapi import HTTPException,Request
from fastapi.responses import HTMLResponse
from .store import drain

SYMBOLS=('SPY','QQQ','NVDA','TSLA','AMD')
PREFIX='free-iex-v1:'
ET=ZoneInfo('America/New_York'); DENVER=ZoneInfo('America/Denver')
# A private module instance: production context and states are never patched.
spec=importlib.util.spec_from_file_location('scanner.v136_live._free_iex_compare',Path(__file__).parent/'v136_live/launch_shadow.py')
comparison=importlib.util.module_from_spec(spec);spec.loader.exec_module(comparison)
comparison.context=lambda *args:(True,'IEX_B_context_not_required',None)

def utc():return datetime.now(timezone.utc)
def auth(value):
    secret=os.getenv('CHART_NEWS_WEBHOOK_SECRET') or os.getenv('TRADINGVIEW_WEBHOOK_SECRET','')
    if not secret or not isinstance(value,str) or not hmac.compare_digest(secret,value):raise HTTPException(401,'Unauthorized')

class DataError(Exception):pass

def api(path,params):
    key=os.getenv('APCA_API_KEY_ID');secret=os.getenv('APCA_API_SECRET_KEY')
    if not key or not secret:raise DataError('alpaca_credentials_missing')
    host='https://paper-api.alpaca.markets' if path=='/v2/calendar' else 'https://data.alpaca.markets'
    try:
        response=requests.get(host+path,params=params,headers={'APCA-API-KEY-ID':key,'APCA-API-SECRET-KEY':secret},timeout=(4,12),allow_redirects=False)
        if response.status_code!=200:raise DataError('alpaca_http_'+str(response.status_code))
        return response.json()
    except (requests.RequestException,ValueError):raise DataError('alpaca_connection_or_payload_error') from None

def calendar(now):
    day=now.astimezone(ET).date()
    rows=api('/v2/calendar',{'start':(day-timedelta(days=12)).isoformat(),'end':day.isoformat()})
    if not isinstance(rows,list):raise DataError('calendar_invalid')
    rows=sorted(rows,key=lambda r:r['date'])
    today=next((r for r in rows if r['date']==day.isoformat()),None)
    prior=[r for r in rows if r['date']<day.isoformat()]
    return today,prior[-1] if prior else None

def bounds(session):
    return tuple(datetime.fromisoformat(session['date']+'T'+session[k]).replace(tzinfo=ET).astimezone(timezone.utc) for k in ('open','close'))

def bars(start,end):
    result={s:[] for s in SYMBOLS}; token=None
    for page in range(4):
        params={'symbols':','.join(SYMBOLS),'timeframe':'1Min','start':start.isoformat(),'end':end.isoformat(),'feed':'iex','adjustment':'raw','sort':'asc','limit':10000}
        if token:params['page_token']=token
        data=api('/v2/stocks/bars',params)
        if not isinstance(data.get('bars'),dict):raise DataError('bars_invalid')
        for ticker,values in data['bars'].items():
            if ticker not in result:raise DataError('unexpected_symbol')
            for row in values:
                began=datetime.fromisoformat(row['t'].replace('Z','+00:00'));finished=began+timedelta(minutes=1)
                if not start<=began or finished>end:continue
                bar=comparison.Bar(ticker,finished,row['o'],row['h'],row['l'],row['c'],row['v']);bar.validate()
                result[ticker].append([ticker,int(finished.timestamp()*1000),row['o'],row['h'],row['l'],row['c'],row['v']])
        token=data.get('next_page_token')
        if not token:return result
    raise DataError('pagination_incomplete')

def connect(store,send,now):
    today,prior=calendar(now);session=today or prior
    if not session:raise DataError('no_market_session')
    opened,closed=bounds(session);end=min(closed,now-timedelta(seconds=5))
    if end<=opened:
        if not prior:raise DataError('no_completed_session')
        opened,closed=bounds(prior);end=closed
    sample=bars(max(opened,end-timedelta(minutes=10)),end)
    counts={s:len(sample[s]) for s in SYMBOLS}
    if any(v==0 for v in counts.values()):raise DataError('iex_probe_missing_symbols')
    identity=PREFIX+'connect:'+now.astimezone(ET).date().isoformat()
    with store.transaction() as tx:
        tx.put(PREFIX+'probe',{'at':now.isoformat(),'feed':'iex','counts':counts})
        tx.enqueue(identity,{'text':'Market Forge 무료 IEX 연결 테스트\nSPY · QQQ · NVDA · TSLA · AMD 조회 확인\n실제 매매 신호 아님 · Bigdata 호출 없음\nID: '+identity,'expires_at':(now+timedelta(minutes=2)).isoformat()})
    return {'feed':'iex','counts':counts,'delivery':drain(store,send,prefix=PREFIX+'connect:',now=now)}

def stage(tx,report,now,day,telegram):
    queued=0
    for ev in report['events']:
        if ev['path']!='fast':continue
        stamp=datetime.fromisoformat(ev['time']); age=(now-stamp).total_seconds()
        ident=PREFIX+day+':'+ev['ticker']+':'+str(ev['episode'])+':'+ev['action']+':'+str(int(stamp.timestamp()))
        entry_key=PREFIX+day+':notified:'+ev['ticker']+':'+str(ev['episode'])
        if tx.get(ident):continue
        status='record_only'
        is_entry=ev['action'] in ('MC','SCMP')
        prior_id=tx.get(entry_key)
        prior_sent=tx.execute('SELECT status FROM mf_outbox WHERE id=?',(prior_id,)).fetchone() if prior_id else None
        if telegram and 0<=age<=90 and (is_entry or (prior_sent and prior_sent[0]=='sent')):
            side='상승' if ev['side']==1 else '하락'
            action=ev['action']
            if action=='CTN' and ev.get('progress_atr',0)<.5:action='HOLD'
            text=f'TEST · IEX · 1분+2분\n{ev["ticker"]} {action} · {side}\n${ev["price"]:.2f} · {stamp.astimezone(DENVER):%H:%M:%S} 덴버\n연구용 · 주문 아님\nID: IEX-{day}-{ev["ticker"]}-{ev["episode"]}'
            tx.enqueue(ident,{'text':text,'expires_at':(stamp+timedelta(seconds=90)).isoformat()})
            if is_entry:tx.put(entry_key,ident)
            status='queued';queued+=1
        tx.put(ident,{'status':status,'observed_at':now.isoformat(),'event':ev})
    return queued

def worker(store,send,now):
    lease=str(uuid.uuid4())
    with store.transaction() as tx:
        config=tx.get(PREFIX+'control',{})
        if not config.get('enabled'):return {'status':'disabled','news_calls':0}
        old=tx.get(PREFIX+'lease',{})
        if old and (now-datetime.fromisoformat(old['at'])).total_seconds()<120:return {'status':'busy_or_throttled'}
        tx.put(PREFIX+'lease',{'id':lease,'at':now.isoformat()})
    try:
        session,prior=calendar(now)
        if not session:result={'status':'market_closed'}
        else:
            opened,closed=bounds(session)
            if not opened+timedelta(minutes=1)<=now<=closed+timedelta(minutes=2):result={'status':'outside_session'}
            else:
                day=session['date'];end=min(closed,now-timedelta(seconds=3))
                with store.transaction() as tx:seed=tx.get(PREFIX+day+':seed')
                if seed is None:
                    if not prior:raise DataError('prior_session_missing')
                    seed=bars(*bounds(prior))
                    if any(len(seed[s])<90 for s in SYMBOLS):raise DataError('iex_seed_insufficient')
                    with store.transaction() as tx:tx.put(PREFIX+day+':seed',seed)
                current=bars(opened,end)
                with store.transaction() as tx:previous=tx.get(PREFIX+day+':snapshot')
                ignored={s:[] for s in SYMBOLS}
                if previous:
                    for symbol in SYMBOLS:
                        saved=previous['symbols'][symbol]
                        prior_rows={r[1]:r for r in saved['bars']}
                        incoming={r[1]:r for r in current[symbol]}
                        if any(t in incoming and incoming[t]!=r for t,r in prior_rows.items()):raise DataError('iex_completed_bar_revision')
                        ignored[symbol]=saved.get('ignored_backfills',[])+[t for t in incoming if t not in prior_rows and t<max(prior_rows,default=0)]
                        prior_rows.update(incoming);current[symbol]=[prior_rows[t] for t in sorted(prior_rows)]
                snapshot={'day':day,'session':{'open':opened.isoformat(),'close':closed.isoformat()},'events':[],
                          'symbols':{s:{'bars':current[s],'seed_bars':seed[s],'ignored_backfills':ignored[s]} for s in SYMBOLS}}
                report=comparison.compare(snapshot)
                with store.transaction() as tx:
                    active=tx.get(PREFIX+'control',{}).get('enabled')
                    queued=stage(tx,report,utc(),day,active)
                    tx.put(PREFIX+day+':snapshot',snapshot)
                    tx.put(PREFIX+day+':report',report)
                result={'status':'scanned','day':day,'counts':{s:len(current[s]) for s in SYMBOLS},'queued':queued}
        result.update(at=now.isoformat(),feed='iex',symbols=list(SYMBOLS),news_calls=0)
        with store.transaction() as tx:tx.put(PREFIX+'status',result);tx.put(PREFIX+'lease',{})
        result['delivery']=drain(store,send,prefix=PREFIX,now=utc())
        return result
    except Exception as exc:
        error=str(exc) if isinstance(exc,DataError) else type(exc).__name__
        with store.transaction() as tx:tx.put(PREFIX+'status',{'at':now.isoformat(),'status':'error','error':error});tx.put(PREFIX+'lease',{})
        return {'status':'error','error':error,'news_calls':0}

def install(app,storage,send):
    @app.get('/free-iex',response_class=HTMLResponse)
    def panel():
        return '''<!doctype html><meta charset="utf-8"><h1>무료 IEX 5종목 테스트</h1><p>1분+2분 연구용 · Bigdata 없음 · 주문 없음</p><form method="post" action="/free-iex/control"><label>웹훅 키 <input type="password" name="secret" required></label><button name="action" value="status">상태 확인</button><button name="action" value="connect">IEX 및 Telegram 연결 테스트</button><button name="action" value="enable">테스트 알림 활성화</button><button name="action" value="disable">테스트 중지</button><button name="action" value="run">지금 점검</button></form>'''
    @app.post('/free-iex/control',response_class=HTMLResponse)
    async def control(request:Request):
        body=await request.body()
        if len(body)>2048:raise HTTPException(400,'Request too large')
        fields=parse_qs(body.decode());auth(fields.get('secret',[''])[0]);action=fields.get('action',['status'])[0];now=utc()
        try:
            if action=='connect':result=connect(storage(),send,now)
            elif action in ('enable','disable'):
                with storage().transaction() as tx:
                    if action=='enable' and not tx.get(PREFIX+'probe'):raise HTTPException(409,'First verify IEX connection')
                    result={'enabled':action=='enable','updated_at':now.isoformat()};tx.put(PREFIX+'control',result)
            elif action=='run':result=worker(storage(),send,now)
            elif action=='status':
                with storage().transaction() as tx:result={k:tx.get(PREFIX+k) for k in ('control','probe','status')}
            else:raise HTTPException(400,'Unknown action')
        except DataError as exc:result={'status':'blocked','error':str(exc)}
        return '<meta charset="utf-8"><pre>'+html.escape(json.dumps(result,ensure_ascii=False,indent=2))+'</pre><a href="/free-iex">Back</a>'
    @app.post('/free-iex/worker')
    def tick(data:dict):
        auth(data.get('secret'));return worker(storage(),send,utc())
