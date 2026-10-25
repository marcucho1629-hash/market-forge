"""Isolated, authenticated swing paper-comparison notifications. No orders."""
import hashlib
import hmac
import math
import os
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks, HTTPException
from scanner.store import drain

TICKERS = set('SPY QQQ NVDA TSLA AVGO AMD META AAPL MSFT AMZN PLTR PANW SNDK GOOG RDDT HOOD COIN JPM AXP V'.split())
PREFIX = 'swing-compare-v1:'
ACTIONS = {'PLAN': '후보 · 다음 시가 대기', 'MODEL_ENTRY': '모의 진입 확인',
           'MODEL_EXIT': '모의 종료 확인', 'REJECTED': '후보 제외 · 시가 조건 미충족'}

def prepare(data, now):
    if data.get('schema') != 'mf.swing.compare.v1' or data.get('ticker') not in TICKERS:
        raise ValueError('schema or ticker')
    k = data.get('model')
    if type(k) is not int or not 0 <= k < 6:
        raise ValueError('model')
    if data.get('horizon') != ('2-5' if k < 3 else '5-10') or data.get('room') != ['1.5R','1.0R','0.5R'][k%3]:
        raise ValueError('model identity')
    if data.get('action') not in ACTIONS or type(data.get('side')) is not int or data['side'] not in (-1,1):
        raise ValueError('action')
    feed = data.get('feed','')
    if not isinstance(feed,str) or feed.split(':')[-1] != data['ticker'] or len(feed)>60:
        raise ValueError('feed')
    for f in ('price','stop','target'):
        v=data.get(f)
        if type(v) not in (int,float) or not math.isfinite(v) or v<=0:
            raise ValueError('price')
    for f in ('event_ms','bar_end_ms','emitted_ms'):
        if type(data.get(f)) is not int: raise ValueError('clock')
    emitted, end, event = [datetime.fromtimestamp(data[f]/1000,timezone.utc) for f in ('emitted_ms','bar_end_ms','event_ms')]
    if not -10 <= (now-emitted).total_seconds() <= 180 or not 0 <= (emitted-end).total_seconds() <= 180:
        raise ValueError('stale')
    if not 0 <= (end-event).total_seconds() <= 3600: raise ValueError('event time')
    if data['action']=='PLAN' and event!=end: raise ValueError('plan time')
    identity='|'.join(str(data[f]) for f in ('schema','feed','model','action','event_ms','bar_end_ms'))
    order={'MODEL_ENTRY':0,'MODEL_EXIT':1,'REJECTED':2,'PLAN':3}[data['action']]
    key=PREFIX+str(data['bar_end_ms'])+f':{k}:{order}:'+hashlib.sha256(identity.encode()).hexdigest()
    direction='상승' if data['side']==1 else '하락'
    local=lambda t:t.astimezone(ZoneInfo('America/Denver')).strftime('%m/%d %H:%M')
    label='기준가' if data['action']=='PLAN' else '모델 가격'
    msg=(f"SWING TEST | {data['ticker']} {direction} · {data['horizon']}일 / {data['room']}\n"
         f"{ACTIONS[data['action']]}\n{label} ${data['price']:.2f} · 스톱 ${data['stop']:.2f} · 목표 ${data['target']:.2f}\n"
         f"기준 {local(event)} · 관측 {local(end)} · 발송 {local(now)} 덴버")
    if data['action']!='PLAN': msg+='\n시간봉 마감 후 모의 기록 · 현재 진입 지시 아님'
    else: msg+='\n미체결 후보 · 다음 시가에서 제외될 수 있음'
    clean={k:v for k,v in data.items() if k!='secret'}
    return key,msg,dict(clean,server_received_at=now.isoformat())

def ingest(store,data,now):
    key,msg,record=prepare(data,now)
    with store.transaction() as tx:
        if tx.get(key): return {'status':'duplicate'}
        tx.put(key,record)
        tx.event(key,now.date().isoformat(),record)
        tx.enqueue(key,{'text':msg,'expires_at':(now+timedelta(seconds=180)).isoformat()})
        tx.put(PREFIX+'latest',record)
    return {'status':'queued','event_id':key}

def install(app,storage,send):
    @app.post('/webhook/swing-compare-v1')
    def intake(data:dict,background_tasks:BackgroundTasks):
        secret=os.getenv('CHART_NEWS_WEBHOOK_SECRET') or os.getenv('TRADINGVIEW_WEBHOOK_SECRET','')
        if not secret or not isinstance(data.get('secret'),str) or not hmac.compare_digest(data['secret'],secret):
            raise HTTPException(401,'Unauthorized')
        try: result=ingest(storage(),data,datetime.now(timezone.utc))
        except (ValueError,TypeError,OverflowError): raise HTTPException(422,'Invalid swing comparison event') from None
        background_tasks.add_task(drain,storage(),send,limit=12,prefix=PREFIX)
        return result
