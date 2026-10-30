"""Isolated, authenticated swing paper-comparison notifications. No orders."""
import hashlib
import hmac
import math
import os
import html
import json
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks, HTTPException, Request
from fastapi.responses import HTMLResponse
from urllib.parse import parse_qs
from scanner.store import drain

TICKERS = set('SPY QQQ NVDA TSLA AVGO AMD META AAPL MSFT AMZN PLTR PANW SNDK GOOG RDDT HOOD COIN JPM AXP V XOM CVX UNH JNJ CAT AVAV TGT SHOP UAL ENPH DDOG IREN ARM ANET IBM QCOM CRWD SNOW MSTR CRCL ORCL MRVL DELL TSM LITE NOW CRM GLW SMCI ADBE TWLO NET ASTS MRNA HPE HIMS ON WMT NBIS INTC AAOI MU DIS NFLX COST BA GS BAC UBER AMAT IWM DIA XLK XLF XLE XLV XLI XLY XLP XLU XLC XLB XLRE TLT GLD SLV GDX LLY ABBV MRK PFE HD LOW GE RTX LMT MS C WFC PYPL'.split())
BANKS = {1: ['SPY', 'QQQ', 'NVDA', 'TSLA', 'AVGO', 'AMD', 'META', 'AAPL'], 2: ['MSFT', 'AMZN', 'PLTR', 'PANW', 'SNDK', 'GOOG', 'RDDT', 'HOOD'], 3: ['COIN', 'JPM', 'AXP', 'V', 'XOM', 'CVX', 'UNH', 'JNJ'], 4: ['CAT', 'AVAV', 'TGT', 'SHOP', 'UAL', 'ENPH', 'DDOG', 'IREN'], 5: ['ARM', 'ANET', 'IBM', 'QCOM', 'CRWD', 'SNOW', 'MSTR', 'CRCL'], 6: ['ORCL', 'MRVL', 'DELL', 'TSM', 'LITE', 'NOW', 'CRM', 'GLW'], 7: ['SMCI', 'ADBE', 'TWLO', 'NET', 'ASTS', 'MRNA', 'HPE', 'HIMS'], 8: ['ON', 'WMT', 'NBIS', 'INTC', 'AAOI', 'MU', 'DIS', 'NFLX'], 9: ['COST', 'BA', 'GS', 'BAC', 'UBER', 'AMAT', 'IWM', 'DIA'], 10: ['XLK', 'XLF', 'XLE', 'XLV', 'XLI', 'XLY', 'XLP', 'XLU'], 11: ['XLC', 'XLB', 'XLRE', 'TLT', 'GLD', 'SLV', 'GDX', 'LLY'], 12: ['ABBV', 'MRK', 'PFE', 'HD', 'LOW', 'GE', 'RTX', 'LMT'], 13: ['MS', 'C', 'WFC', 'PYPL']}
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

def ingest_bank(store,data,now):
    if data.get('schema')!='mf.swing.bank.v1' or type(data.get('bank')) is not int or data['bank'] not in BANKS:
        raise ValueError('bank identity')
    for f in ('emitted_ms','bar_end_ms'):
        if type(data.get(f)) is not int: raise ValueError('clock')
    emitted=datetime.fromtimestamp(data['emitted_ms']/1000,timezone.utc)
    end=datetime.fromtimestamp(data['bar_end_ms']/1000,timezone.utc)
    if not -10 <= (now-emitted).total_seconds() <= 180 or not 0 <= (emitted-end).total_seconds() <= 180:
        raise ValueError('stale bank')
    health,events=data.get('health'),data.get('events')
    if not isinstance(health,list) or not isinstance(events,list) or len(events)>64: raise ValueError('batch shape')
    allowed=set(BANKS[data['bank']])
    if len(health)!=len(allowed) or any(not isinstance(h,dict) for h in health): raise ValueError('health shape')
    if {h.get('ticker') for h in health}!=allowed: raise ValueError('wrong symbols')
    for h in health:
        if type(h.get('bar_end_ms')) is not int or type(h.get('ready')) is not bool: raise ValueError('health fields')
    prepared=[]
    for event in events:
        if not isinstance(event,dict) or event.get('ticker') not in allowed or event.get('bar_end_ms')!=data['bar_end_ms']:
            raise ValueError('event identity')
        # request.security calculations carry a per-symbol calculation clock.
        # The authenticated outer envelope is the actual webhook emission clock.
        computed=event.get('emitted_ms')
        if type(computed) is not int or computed > data['emitted_ms']+10000:
            raise ValueError('event clock')
        normalized=dict(event,source_computed_ms=computed,emitted_ms=data['emitted_ms'])
        prepared.append(prepare(normalized,now))
    # Validate the complete batch before any state/queue writes.
    with store.transaction() as tx:
        queued=0
        for key,msg,record in prepared:
            if tx.get(key): continue
            tx.put(key,record);tx.event(key,now.date().isoformat(),record)
            tx.enqueue(key,{'text':msg,'expires_at':(now+timedelta(seconds=180)).isoformat()})
            tx.put(PREFIX+'latest',record);queued+=1
        tx.put(PREFIX+'bank:'+str(data['bank']),{'received_at':now.isoformat(),'bar_end_ms':data['bar_end_ms'],'health':health,'events':len(events),'queued':queued})
        trace={'bank':data['bank'],'received_at':now.isoformat(),'bar_end_ms':data['bar_end_ms'],'emitted_ms':data['emitted_ms'],'health':health,'events':len(events),'queued':queued}
        tx.put('swing-diagnostic:hour:'+str(data['bank'])+':'+str(data['bar_end_ms']),trace)
    return {'status':'accepted','bank':data['bank'],'queued':queued}

def install(app,storage,send):
    def authenticate(value):
        secret=os.getenv('CHART_NEWS_WEBHOOK_SECRET') or os.getenv('TRADINGVIEW_WEBHOOK_SECRET','')
        if not secret or not isinstance(value,str) or not hmac.compare_digest(value,secret):
            raise HTTPException(401,'Unauthorized')

    @app.get('/swing-compare',response_class=HTMLResponse)
    def panel():
        return '''<!doctype html><meta charset="utf-8"><h1>SWING TEST 연결</h1>
<p>1시간봉 비교용 모의 기록 · 주문 없음</p>
<form method="post" action="/swing-compare/control"><label>웹훅 키 <input type="password" name="secret" autocomplete="off" required></label>
<button name="action" value="status">상태 확인</button><button name="action" value="diagnostics">100종목 진단 이력</button><button name="action" value="test">Telegram 연결 테스트</button></form>'''

    @app.post('/swing-compare/control',response_class=HTMLResponse)
    async def control(request:Request):
        raw=await request.body()
        if len(raw)>1024:raise HTTPException(400,'Too large')
        fields=parse_qs(raw.decode());authenticate(fields.get('secret',[''])[0])
        now=datetime.now(timezone.utc);store=storage()
        if fields.get('action',[''])[0]=='test':
            key=PREFIX+'connect:'+now.date().isoformat()
            with store.transaction() as tx:
                tx.enqueue(key,{'text':'SWING TEST 연결 테스트\n실제 매매 신호가 아닙니다.\n1시간봉 스윙 비교 알림 전용\n테스트 ID: swing-v17-'+now.date().isoformat(),'expires_at':(now+timedelta(seconds=180)).isoformat()})
            result={'delivery':drain(store,send,prefix=PREFIX,limit=12)}
        elif fields.get('action',[''])[0]=='diagnostics':
            result=diagnostics(store,now)
        else:
            with store.transaction() as tx:
                result={'latest':tx.get(PREFIX+'latest'), 'banks':{i:tx.get(PREFIX+'bank:'+str(i)) for i in BANKS}, 'outbox':tx.execute("SELECT status,COUNT(*) FROM mf_outbox WHERE id LIKE ? GROUP BY status",(PREFIX+'%',)).fetchall()}
        return '<!doctype html><meta charset="utf-8"><h1>SWING TEST 결과</h1><pre>'+html.escape(json.dumps(result,ensure_ascii=False,indent=2))+'</pre>'

    @app.post('/webhook/swing-compare-v1')
    def intake(data:dict,background_tasks:BackgroundTasks):
        authenticate(data.get('secret'))
        try: result=ingest(storage(),data,datetime.now(timezone.utc))
        except (ValueError,TypeError,OverflowError): raise HTTPException(422,'Invalid swing comparison event') from None
        background_tasks.add_task(drain,storage(),send,limit=12,prefix=PREFIX)
        return result

    @app.post('/webhook/swing-bank-v1')
    def bank_intake(data:dict,background_tasks:BackgroundTasks):
        authenticate(data.get('secret'))
        try: result=ingest_bank(storage(),data,datetime.now(timezone.utc))
        except (ValueError,TypeError,OverflowError,KeyError) as exc:
            record_rejection(storage(),data,datetime.now(timezone.utc),exc)
            raise HTTPException(422,'Invalid swing bank event') from None
        background_tasks.add_task(drain,storage(),send,limit=64,prefix=PREFIX)
        return result


def record_rejection(store,data,now,exc):
    # Authenticated validation failures only. Never save payloads, keys or exception text.
    try:
        with store.transaction() as tx:
            bank=data.get('bank')
            bank=bank if type(bank) is int and bank in BANKS else 'unknown'
            key='swing-diagnostic:reject:'+now.date().isoformat()+':'+str(bank)
            old=tx.get(key,{'count':0})
            allowed={'event identity','event clock','stale bank','clock','health fields','health shape','wrong symbols','batch shape','bank identity','stale','event time','plan time','model identity','action','feed','price','schema or ticker','model'}
            reason=str(exc) if str(exc) in allowed else 'invalid_payload'
            tx.put(key,{'count':old['count']+1,'last_at':now.isoformat(),'type':type(exc).__name__,'reason':reason})
    except Exception:
        pass

def diagnostics(store,now):
    day=now.astimezone(ZoneInfo('America/New_York')).date().isoformat()
    start=int(datetime.fromisoformat(day).replace(tzinfo=ZoneInfo('America/New_York')).timestamp()*1000)
    end=start+86400000
    with store.transaction() as tx:
        hours=[json.loads(r[0]) for r in tx.execute("SELECT value FROM mf_kv WHERE key LIKE ?",('swing-diagnostic:hour:%',)).fetchall()]
        hours=[v for v in hours if start<=v['bar_end_ms']<end]
        rejected=[json.loads(r[0]) for r in tx.execute("SELECT value FROM mf_kv WHERE key LIKE ?",('swing-diagnostic:reject:'+day+':%',)).fetchall()]
        events=[v for v in tx.events(day) if v.get('schema')=='mf.swing.compare.v1']
        deliveries=[{'id':r[0],'status':r[1]} for r in tx.execute("SELECT id,status FROM mf_outbox WHERE id LIKE ?",(PREFIX+'%',)).fetchall()]
        banks={i:tx.get(PREFIX+'bank:'+str(i)) for i in BANKS}
    return {'day':day,'banks':banks,'hourly_receipts':hours,'authenticated_rejections':rejected,'trade_events':events,'deliveries':deliveries,
        'limits':['Hourly receipt history starts with this diagnostic deployment; prior missing hours unknown.',
                  'Legacy Pine health lacks gate reasons. ready=true is NOT proof that PLAN conditions passed.',
                  'Authentication failures are not counted by this authenticated diagnostic path.']}
