"""Isolated preview application. Never merge into production."""
import html
from urllib.parse import parse_qs
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from smoke import send_once, message

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

@app.get('/', response_class=HTMLResponse)
def home():
    return '<!doctype html><meta charset="utf-8"><title>Market Forge synthetic test</title><h1>인공 데이터 Telegram 연결 테스트</h1><pre>' + html.escape(message()) + '</pre><form method="post" action="/send"><label>테스트 키 <input name="key" type="password" autocomplete="off" required></label><button type="submit">인공 테스트 메시지 1건 보내기</button></form><p>실시간 신호 아님. 전송 후 반복 클릭하지 마세요.</p>'

@app.post('/send')
async def send(request: Request):
    if request.headers.get('origin') != str(request.base_url).rstrip('/'):
        raise HTTPException(403, 'Same-origin form required')
    body = await request.body()
    if len(body) > 1024:
        raise HTTPException(400, 'Request too large')
    try:
        key = parse_qs(body.decode()).get('key', [''])[0]
        return send_once(key)
    except PermissionError:
        raise HTTPException(403, 'Unauthorized or expired test') from None
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from None
