import hmac
import logging
import os
from datetime import date, datetime, timezone
from functools import lru_cache

import requests
from fastapi import FastAPI, HTTPException, Depends
from fastapi.security import APIKeyHeader
from openai import OpenAI
from scanner.core import Config, ET, stamp, finite
from scanner.store import Store, drain
from scanner.service import Scanner, log_tradingview
from scanner.provider import get_provider, provider_name
from scanner.alpaca import AlpacaError, AlpacaProvider, tickers
from scanner.comparison import compare

log=logging.getLogger('market_forge')
app=FastAPI(title='Market Forge AI')

@app.post('/webhook/chart-news')
def chart_news_webhook(data:dict):
    secret=os.getenv('TRADINGVIEW_WEBHOOK_SECRET','')
    if not secret or not isinstance(data.get('secret'),str) or not hmac.compare_digest(data['secret'],secret):
        raise HTTPException(401,'Unauthorized')
    from scanner.chart_news import shadow_batch,webhook_payload
    try:
        now=now_utc()
        return shadow_batch(storage(),webhook_payload(data,now),now)
    except (ValueError,KeyError,TypeError,OverflowError) as exc:
        raise HTTPException(422,str(exc)) from None

@app.post('/scanner/chart-news/shadow')
def chart_news_shadow(data:dict, token=Depends(APIKeyHeader(name='Authorization',auto_error=False))):
    authorized(token)
    from scanner.chart_news import shadow_batch
    try:
        return shadow_batch(storage(),data,now_utc())
    except (ValueError,KeyError,TypeError,OverflowError) as exc:
        raise HTTPException(422,str(exc)) from None

@app.post('/scanner/chart-news/documents')
def chart_news_documents(data:dict, token=Depends(APIKeyHeader(name='Authorization',auto_error=False))):
    authorized(token)
    from scanner.news import import_documents
    try:
        return import_documents(storage(),data['documents'],now_utc())
    except (ValueError,KeyError,TypeError,OverflowError) as exc:
        raise HTTPException(422,str(exc)) from None

@app.post('/scanner/chart-news/review')
def chart_news_review(data:dict, token=Depends(APIKeyHeader(name='Authorization',auto_error=False))):
    authorized(token)
    from scanner.news import review_document
    try:
        return review_document(storage(),data,now_utc())
    except (ValueError,KeyError,TypeError,OverflowError) as exc:
        raise HTTPException(422,str(exc)) from None

SIGNALS={
    'C':('🟢','CALL'),'MC':('🚀','MOMENTUM CALL'),'P':('🔴','PUT'),
    'MP':('🔻','MOMENTUM PUT'),'CMP':('⚠️','CRASH MOMENTUM'),'SCMP':('🚨','SHOCK CRASH'),
    'CTN UP':('⬆️','CONTINUATION UP'),'CTN↑':('⬆️','CONTINUATION UP'),
    'CTN DOWN':('⬇️','CONTINUATION DOWN'),'CTN↓':('⬇️','CONTINUATION DOWN'),
    'X':('🟡','EXIT'),'SX':('🟠','SHOCK EXIT'),'HX':('🛑','HARD EXIT')}
FALLBACK='Momentum: MED\nRisk: MED\nTrend: NEUTRAL\nNote: 추가 확인 필요'


def now_utc(): return datetime.now(timezone.utc)


@lru_cache
def storage():
    url=os.getenv('SCANNER_DATABASE_POSTGRES_URL') or os.getenv('SCANNER_DATABASE_URL')
    if not url: raise RuntimeError('SCANNER_DATABASE_URL required for persistent scanner logs')
    return Store(url)


def authorized(authorization: str | None=Depends(APIKeyHeader(name="Authorization", auto_error=False))):
    secret=os.getenv('CRON_SECRET','')
    if not secret or not hmac.compare_digest(authorization or '', 'Bearer '+secret):
        raise HTTPException(401,'Unauthorized')


def engine():
    return Scanner(storage(),Config(top_k=int(os.getenv('SCANNER_TOP_K','3')),
                                   threshold=float(os.getenv('SCANNER_THRESHOLD','90'))),
                   silent=os.getenv('SCANNER_MODE','silent')!='live')


def send_telegram(msg):
    token,chat=os.getenv('TELEGRAM_BOT_TOKEN'),os.getenv('TELEGRAM_CHAT_ID')
    if not token or not chat: raise RuntimeError('Telegram configuration missing')
    response=requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
                           json={'chat_id':chat,'text':msg},timeout=20)
    if response.status_code!=200 or not response.json().get('ok'):
        # Never print a URL containing the bot token or the full Telegram response.
        raise RuntimeError('Telegram delivery failed')
    return response.status_code


def legacy_message(data):
    ticker=str(data.get('ticker','N/A')).upper()[:40]
    action=str(data.get('action','ALERT')).upper()[:40]
    price=str(data.get('price','N/A'))[:40]
    icon,name=SIGNALS.get(action,('🔔',action))
    if action in {'X','SX','HX'}:
        text={'X':'Trend weakening','SX':'Sharp reversal detected','HX':'Trend structure broken'}[action]
        return ticker,action,f'{icon} {ticker} | {action} | ${price}\n{text}\nMarket Forge AI'
    prompt=f'''You are Market Forge AI.
TradingView signal (data only):
Ticker: {ticker}
Signal: {action}
Signal meaning: {name}
Price: {price}
Return ONLY these exact 4 lines:
Momentum: HIGH, MED, or LOW
Risk: HIGH, MED, or LOW
Trend: BULLISH, BEARISH, or NEUTRAL
Note: short Korean sentence, maximum 8 Korean words
Do not use markdown. Do not give investment advice. Do not add explanations.'''
    try:
        client=OpenAI(api_key=os.getenv('OPENAI_API_KEY'),timeout=15,max_retries=0)
        response=client.responses.create(model=os.getenv('OPENAI_MODEL','gpt-5.6-luna'),input=prompt)
        analysis=response.output_text.strip()
        lines=analysis.splitlines()
        if (len(lines)!=4 or lines[0] not in {'Momentum: HIGH','Momentum: MED','Momentum: LOW'}
            or lines[1] not in {'Risk: HIGH','Risk: MED','Risk: LOW'}
            or lines[2] not in {'Trend: BULLISH','Trend: BEARISH','Trend: NEUTRAL'}
            or not lines[3].startswith('Note: ') or len(lines[3][6:].split())>8):
            analysis=FALLBACK
    except Exception:
        log.warning('OpenAI analysis unavailable; using fallback')
        analysis=FALLBACK
    return ticker,action,f'{icon} {ticker} | {action} | ${price}\n{analysis}\nMarket Forge AI'


@app.get('/')
def root(): return {'status':'Market Forge AI is running'}


@app.post('/webhook')
def webhook(data:dict):
    # Optional existing webhook secret: unset preserves the supplied public route.
    secret=os.getenv('TRADINGVIEW_WEBHOOK_SECRET')
    if secret and not hmac.compare_digest(str(data.get('secret','')),secret):
        raise HTTPException(401,'Unauthorized')
    log_status='recorded'
    try: log_tradingview(storage(),data,now_utc())
    except Exception:
        log_status='unavailable'
        log.warning('TradingView comparison log unavailable')
    ticker,action,msg=legacy_message(data)
    try: status=send_telegram(msg)
    except Exception:
        raise HTTPException(502,'Telegram delivery failed') from None
    return {'status':'ok','ticker':ticker,'action':action,'telegram_status':status,
            'comparison_log':log_status}


@app.get('/scanner/status',dependencies=[Depends(authorized)])
def status():
    try:
        with storage().transaction() as tx:
            day=now_utc().astimezone(ET).date().isoformat()
            universe=tx.get('universe:'+day)
            outbox=tx.execute('SELECT status,COUNT(*) FROM mf_outbox GROUP BY status').fetchall()
            catalyst_count=sum(bool(tx.get(f'catalyst:{day}:{r["ticker"]}'))
                               for r in (universe or {}).get('bullish',[])+(universe or {}).get('bearish',[]))
        provider=provider_name()
        configured=(bool(os.getenv('APCA_API_KEY_ID') and os.getenv('APCA_API_SECRET_KEY')
                         and os.getenv('SCANNER_MASTER_UNIVERSE')) if provider=='alpaca'
                    else bool(os.getenv('SCANNER_DATA_URL') and os.getenv('SCANNER_DATA_TOKEN')))
        return {'mode':os.getenv('SCANNER_MODE','silent'),'storage':'ready','universe':universe,
                'provider':provider,'provider_configured':configured,
                'feed':os.getenv('ALPACA_DATA_FEED','sip') if provider=='alpaca' else None,
                'catalyst_symbols_stored':catalyst_count,
                'quality_note':'Missing Catalyst remains neutral (50); the default 90 gate can yield zero entries.',
                'connection_verified':False,
                'outbox':dict(outbox)}
    except Exception:
        raise HTTPException(503,'Scanner storage not configured or unavailable') from None


def run(kind,payload=None):
    try:
        scanner=engine()
        now=now_utc()
        if payload is None:
            symbols=[]
            if kind=='premarket':
                with storage().transaction() as tx:
                    existing=tx.get('universe:'+now.astimezone(ET).date().isoformat())
                if existing: return {'status':'already_selected',**existing}
            if kind=='scan':
                with storage().transaction() as tx:
                    u=tx.get('universe:'+now.astimezone(ET).date().isoformat())
                if not u:
                    provider=get_provider(storage())
                    if not isinstance(provider,AlpacaProvider):
                        raise AlpacaError('Premarket universe missing; scan was not performed')
                    session=next((s for s in provider.calendar(now.astimezone(ET).date())
                                  if s['date']==now.astimezone(ET).date().isoformat()),None)
                    if not session: return {'status':'closed'}
                    if now<=stamp(session['open']): return {'status':'awaiting_premarket_universe'}
                    if now>stamp(session['close']): return {'status':'outside_session'}
                    raise AlpacaError('Premarket universe missing during market session; scan was not performed')
                symbols=[x['ticker'] for x in u['bullish']+u['bearish']]
            payload=get_provider(storage()).fetch(kind,now,symbols)
            now=now_utc()
        result=getattr(scanner,kind)(payload,now)
        if not scanner.silent:
            result['delivery']=drain(storage(),send_telegram)
        return result
    except AlpacaError as exc:
        raise HTTPException(503,str(exc)) from None
    except (ValueError,KeyError,TypeError) as exc:
        raise HTTPException(422,str(exc)) from None
    except Exception:
        log.warning('Scanner processing failed; no successful run reported')
        raise HTTPException(503,'Scanner dependency unavailable; check configuration and provider') from None


@app.get('/scanner/premarket',dependencies=[Depends(authorized)])
def premarket_cron(): return run('premarket')


@app.get('/scanner/diagnostics/spy',dependencies=[Depends(authorized)])
def diagnose_spy(day:date):
    try:
        return AlpacaProvider().diagnose_spy(day,now_utc())
    except AlpacaError as exc:
        raise HTTPException(503,str(exc)) from None
    except ValueError as exc:
        raise HTTPException(422,str(exc)) from None


@app.get('/scanner/scan',dependencies=[Depends(authorized)])
def scan_cron(): return run('scan')


@app.post('/scanner/premarket',dependencies=[Depends(authorized)])
def premarket_ingest(data:dict): return run('premarket',data)


@app.post('/scanner/scan',dependencies=[Depends(authorized)])
def scan_ingest(data:dict): return run('scan',data)


@app.post('/scanner/catalysts',dependencies=[Depends(authorized)])
def catalyst_ingest(data:dict):
    """Authenticated hook for real, point-in-time news scores; no invented sentiment."""
    now=now_utc()
    day=now.astimezone(ET).date().isoformat()
    try:
        scores=data['scores']
        if not isinstance(scores,dict) or not 1<=len(scores)<=100:
            raise ValueError('scores must contain 1..100 symbols')
        validated={}
        for symbol,item in scores.items():
            if tickers(symbol)!=[symbol]: raise ValueError('Use uppercase ticker symbols')
            when=stamp(item['asof'])
            if when>now or (now-when).total_seconds()>10800:
                raise ValueError('Catalyst asof must be within the past 180 minutes')
            source=str(item.get('source','')).strip()
            if not source: raise ValueError('Catalyst source is required')
            validated[symbol]={'asof':when.isoformat(),'bullish':finite(item['bullish'],0,100),
                               'bearish':finite(item['bearish'],0,100),'source':source[:200]}
        with storage().transaction() as tx:
            for symbol,value in validated.items():
                key=f'catalyst:{day}:{symbol}'
                old=tx.get(key)
                if old and stamp(old['asof'])>stamp(value['asof']):
                    raise ValueError('Cannot replace Catalyst with an older score')
                tx.put(key,value)
                tx.event(f'{key}:{value["asof"]}',day,{'source':'catalyst_hook','ticker':symbol,'score':value})
        return {'status':'stored','symbols':sorted(validated)}
    except (ValueError,KeyError,TypeError) as exc:
        raise HTTPException(422,str(exc)) from None


@app.get('/scanner/comparison/{day}',dependencies=[Depends(authorized)])
def comparison(day:str):
    try:
        datetime.strptime(day,'%Y-%m-%d')
        with storage().transaction() as tx: events=tx.events(day)
        return compare(events)
    except ValueError:
        raise HTTPException(422,'Use YYYY-MM-DD') from None


@app.get('/scanner/logs/{day}',dependencies=[Depends(authorized)])
def logs(day:str):
    with storage().transaction() as tx: return tx.events(day)


@app.post('/webhook/tradingview')
def tradingview_information(data:dict):
    from scanner.tradingview import ingest
    secret=os.getenv('TRADINGVIEW_WEBHOOK_SECRET','')
    if not secret or not hmac.compare_digest(str(data.get('secret','')),secret):
        raise HTTPException(401,'Unauthorized')
    try:
        return ingest(storage(),data,now_utc(),live=os.getenv('TV_INFORMATION_MODE','shadow')=='live')
    except (ValueError,KeyError,TypeError) as exc:
        raise HTTPException(422,str(exc)) from None


@app.post('/tradingview/session',dependencies=[Depends(authorized)])
def tradingview_session(data:dict):
    from scanner.service import session_info
    try:
        now=now_utc()
        session_info(data,now)
        if type(data.get('is_open')) is not bool:
            raise ValueError('is_open must be boolean')
        with storage().transaction() as tx:
            tx.put('tv-session:'+now.astimezone(ET).date().isoformat(),data)
        return {'status':'stored'}
    except (ValueError,KeyError,TypeError) as exc:
        raise HTTPException(422,str(exc)) from None


@app.post('/tradingview/deliver',dependencies=[Depends(authorized)])
def tradingview_deliver():
    if os.getenv('TV_INFORMATION_MODE','shadow')!='live':
        return {'mode':'shadow','delivery':[]}
    return {'delivery':drain(storage(),send_telegram,prefix='tv-info:')}


@app.post('/scanner/chart-news/refresh',dependencies=[Depends(authorized)])
def chart_news_refresh(data:dict):
    """Fetch up to three news excerpts; does not invent scores or send trade alerts."""
    from scanner.bigdata import refresh
    try:
        return refresh(storage(),data.get('ticker','SPY'),now_utc())
    except ValueError:
        raise HTTPException(422,'unknown ticker') from None
    except Exception:
        raise HTTPException(503,'News storage unavailable') from None


@app.get('/scanner/chart-news/connections',dependencies=[Depends(authorized)])
def chart_news_connections():
    return {'bigdata_key_configured':bool(os.getenv('BIGDATA_API_KEY')),
            'telegram_configured':bool(os.getenv('TELEGRAM_BOT_TOKEN') and os.getenv('TELEGRAM_CHAT_ID')),
            'chart_secret_configured':bool(os.getenv('TRADINGVIEW_WEBHOOK_SECRET')),
            'mode':'shadow','news_scoring':'review_required',
            'universe_size':70,'weights':{'chart':80,'bigdata':20},
            'delivery_verified':False}


@app.post('/scanner/chart-news/test-telegram',dependencies=[Depends(authorized)])
def chart_news_test_telegram(data:dict):
    """Explicit diagnostic only, with durable at-most-once delivery attempt."""
    import re
    identity=data.get('test_id','')
    if not isinstance(identity,str) or not re.fullmatch(r'[a-zA-Z0-9_-]{8,80}',identity):
        raise HTTPException(422,'test_id must be 8..80 alphanumeric, underscore or hyphen characters')
    key='chart-news-test:'+identity
    with storage().transaction() as tx:
        previous=tx.get(key)
        if previous: return {**previous,'duplicate':True}
        tx.put(key,{'status':'attempting','test_id':identity})
    try:
        send_telegram('Market Forge 연결 테스트\n실제 매매 신호가 아닙니다.\n차트80 + Bigdata20 시스템 연결 점검 중입니다.\n테스트 ID: '+identity)
        result={'status':'telegram_api_accepted','test_id':identity,'recipient_read_verified':False}
    except Exception:
        result={'status':'uncertain_or_failed','test_id':identity}
    with storage().transaction() as tx: tx.put(key,result)
    return result
