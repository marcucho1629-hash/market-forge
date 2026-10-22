"""Fixed synthetic scenario only. No live feed, orders, database or production imports."""
import hashlib
import hmac
import json
import os
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.request import Request, urlopen
from research_engine import Bar, Engine

BRANCH = 'codex/v136-telegram-smoke-20261021'
KEY_SHA256 = '3418a9e16cc9ae0213d34621c27074242050154dde09aa93c053daf2b2c453b9'
EXPIRES = datetime(2026, 10, 23, tzinfo=timezone.utc)
TEST_ID = 'V136-SYNTHETIC-20261021-01'
_lock = threading.Lock()
_attempted = False

def scenario():
    e = Engine()
    start = datetime(2026, 10, 16, 9, 30, tzinfo=ZoneInfo('America/New_York'))
    previous = 368.0
    events = []
    for minute in range(1, 33):
        close = 368 + (.012 if minute % 2 else -.012) if minute <= 30 else (368.35 if minute == 31 else 367.20)
        volume = 100 if minute <= 30 else (400 if minute == 31 else 250)
        wick = .09 if minute <= 30 else .04
        events.extend(e.on_bar(Bar('TSLA', start + timedelta(minutes=minute), previous,
                                 max(previous, close) + wick, min(previous, close) - wick, close, volume)))
        previous = close
    if [x.action for x in events] != ['MC', 'FAST_X']:
        raise RuntimeError('Synthetic scenario failed; no message sent')
    return events

def message():
    lines = ['[새 엔진 연결 TEST — 인공 데이터]', 'V13.36 Research / 실제 매매 신호 아님',
             '아래 가격·시간은 가상 시나리오이며 실거래 결과가 아닙니다.']
    for event in scenario():
        local = event.time.astimezone(ZoneInfo('America/Denver'))
        lines.append(f'TSLA {event.action} | ${event.price:.2f} | 가상 봉 마감 {local:%Y-%m-%d %H:%M} 덴버')
    lines.extend(['엔진 계산 → Telegram 전달 테스트입니다.',
                  '실시간 장중 데이터 연결은 아직 활성화되지 않았습니다.', f'테스트 ID: {TEST_ID}'])
    return '\n'.join(lines)

def authorize(key, environ=None, now=None):
    env = os.environ if environ is None else environ
    if env.get('VERCEL_ENV') != 'preview' or env.get('VERCEL_GIT_COMMIT_REF') != BRANCH:
        raise PermissionError('Preview test branch only')
    if (now or datetime.now(timezone.utc)) >= EXPIRES:
        raise PermissionError('Test expired')
    if not hmac.compare_digest(hashlib.sha256(key.encode()).hexdigest(), KEY_SHA256):
        raise PermissionError('Unauthorized')
    if not env.get('TELEGRAM_BOT_TOKEN') or not env.get('TELEGRAM_CHAT_ID'):
        raise RuntimeError('Telegram configuration missing')

def send_once(key, opener=urlopen):
    global _attempted
    authorize(key)
    text = message()
    with _lock:
        if _attempted:
            raise RuntimeError('Already attempted in this instance. No automatic retry.')
        _attempted = True
    # Instance guard is not durable across cold starts. Operator must send only once.
    token = os.environ['TELEGRAM_BOT_TOKEN']
    payload = json.dumps({'chat_id': os.environ['TELEGRAM_CHAT_ID'], 'text': text}).encode()
    req = Request('https://api.telegram.org/bot' + token + '/sendMessage', data=payload,
                  headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with opener(req, timeout=20) as response:
            result = json.load(response)
        if result.get('ok') is not True or not isinstance(result.get('result', {}).get('message_id'), int):
            raise ValueError('Rejected')
    except Exception:
        raise RuntimeError('Delivery not confirmed. Check Telegram before any manual retry.') from None
    return {'status': 'telegram_api_accepted', 'test_id': TEST_ID,
            'message_id': result['result']['message_id'], 'recipient_read_verified': False,
            'live_feed_enabled': False}
