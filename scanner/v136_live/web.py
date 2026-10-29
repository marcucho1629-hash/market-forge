import hmac
import os
import html
import json
from datetime import datetime, timezone
from urllib.parse import parse_qs
from fastapi import Request, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse
from . import runtime

def auth(value):
    secret=os.getenv('CHART_NEWS_WEBHOOK_SECRET') or os.getenv('TRADINGVIEW_WEBHOOK_SECRET','')
    if not secret or not isinstance(value,str) or not hmac.compare_digest(value,secret):
        raise HTTPException(401,'Unauthorized')

def install(app,storage,send):
    @app.post('/webhook/research-v136-live')
    def intake(data:dict, background_tasks:BackgroundTasks):
        auth(data.get('secret'))
        try:
            now=datetime.now(timezone.utc)
            result=runtime.ingest(storage(),{k:v for k,v in data.items() if k!='secret'},now)
            background_tasks.add_task(runtime.delivery,storage(),send,now)
            return result
        except (ValueError,TypeError,KeyError,OverflowError):
            raise HTTPException(422,'Invalid final one-minute bars') from None

    @app.get('/research-v136-live',response_class=HTMLResponse)
    def panel():
        return '''<!doctype html><meta charset="utf-8"><title>V13.36 live research</title>
<h1>새 연구 엔진 장중 테스트</h1><p>확정 1분봉 · 20종목 · 기존 엔진과 별도 기록 · 주문 없음</p>
<form method="post" action="/research-v136-live/control">
<label>기존 웹훅 키 <input name="secret" type="password" required autocomplete="off"></label>
<button name="action" value="status">수신 상태 확인</button>\n<button name="action" value="export">오늘 분석 데이터 보기</button>
<button name="action" value="enable">7일 테스트 활성화</button>
<button name="action" value="disable">테스트 중지</button>
<button name="action" value="deliver">대기 메시지 전달</button></form>'''

    @app.post('/research-v136-live/control',response_class=HTMLResponse)
    async def panel_control(request:Request):
        body=await request.body()
        if len(body)>1024: raise HTTPException(400,'Request too large')
        fields=parse_qs(body.decode())
        auth(fields.get('secret',[''])[0])
        now=datetime.now(timezone.utc)
        action=fields.get('action',['status'])[0]
        if action in ('enable','disable'):
            cfg=runtime.control(storage(),action=='enable',now)
            result={'control':cfg,'delivery':runtime.delivery(storage(),send,now)}
        elif action=='deliver': result={'delivery':runtime.delivery(storage(),send,now)}
        elif action=='status': result=runtime.status(storage(),now)
        elif action=='export': result=runtime.diagnostic_export(storage(),now)
        else: raise HTTPException(400,'Unknown action')
        return '<!doctype html><meta charset="utf-8"><h1>Research status</h1><pre>'+html.escape(json.dumps(result,ensure_ascii=False,indent=2))+'</pre><a href="/research-v136-live">Back</a>'

