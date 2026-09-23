"""TradingView-only information gate. Never substitutes technical for composite."""
import hashlib
import re
from datetime import timedelta
from .core import ET, stamp, finite


def ingest(store, data, now, live=False):
    symbol = data['ticker']
    if not isinstance(symbol, str) or not re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,14}', symbol):
        raise ValueError('Invalid ticker')
    direction = data['direction']
    if direction not in ('LONG', 'SHORT'):
        raise ValueError('Invalid direction')
    observed = stamp(data['observed_at'])
    if not 0 <= (now-observed).total_seconds() <= 15:
        raise ValueError('Stale or future observation')
    event_id = data['event_id']
    if not isinstance(event_id, str) or not 1 <= len(event_id) <= 200:
        raise ValueError('event_id required')
    for key in ('confirmed', 'source_whipsaw'):
        if type(data.get(key)) is not bool:
            raise ValueError(key+' must be boolean')
    action = data['action']
    if action not in ('C','MC','P','MP','CMP','SCMP','X','SX','HX','CTN_UP','CTN_DOWN'):
        raise ValueError('Unknown action')
    price = finite(data['price'], .000001)
    technical = finite(data['momentum_score'], 0, 100)
    rsi = finite(data['rsi'], 0, 100)
    rv = finite(data['relative_volume'], 0)
    momentum = finite(data['directional_momentum_atr'], 0)
    extension = finite(data['extension_atr'], 0)
    if data.get('market_condition') not in ('CHOPPY','ENGINE'):
        raise ValueError('Explicit market condition required')
    day = now.astimezone(ET).date().isoformat()
    digest = hashlib.sha256((symbol+':'+event_id).encode()).hexdigest()
    key = 'tv-info:'+day+':'+digest
    with store.transaction() as tx:
        old = tx.get(key)
        if old:
            return {**old, 'duplicate':True}
        # Calendar is trusted server configuration, never supplied by a webhook.
        session = tx.get('tv-session:'+day)
        active = bool(session and session.get('is_open') is True and
                      stamp(session['open']) <= now < stamp(session['close']))
        blocks = []
        if not active: blocks.append('session_not_verified_or_closed')
        if data['market_condition'] == 'CHOPPY': blocks.append('choppy')
        if data['source_whipsaw']: blocks.append('whipsaw')
        if extension >= 2: blocks.append('chasing')
        if action in ('X','SX','HX'): blocks.append('exit_requires_position_context')
        expected = 'LONG' if action in ('C','MC','CTN_UP') else 'SHORT'
        if action not in ('X','SX','HX') and direction != expected:
            raise ValueError('Action direction mismatch')
        high = technical >= 70 and rv >= 1.5 and momentum >= .5
        if not high: blocks.append('momentum_not_confirmed')
        state_key = 'tv-info-cooldown:'+day+':'+symbol
        last = tx.get(state_key)
        if last and (now-stamp(last)).total_seconds() < 1200: blocks.append('cooldown')
        quota_key = 'tv-info-quota:'+str(int(now.timestamp())//300)
        count = tx.get(quota_key, 0)
        if count >= 3: blocks.append('budget')
        selected = not blocks
        result = dict(ticker=symbol, action=action, mode='live' if live else 'shadow',
                      composite_score=None, entry_selected=False,
                      entry_block='missing_quote_and_composite_evidence',
                      information_selected=selected, blocks=blocks, duplicate=False,
                      received_at=now.isoformat(), observed_at=observed.isoformat(),
                      delivery='not_queued', technical=technical, rsi=rsi)
        if selected:
            tx.put(state_key, now.isoformat())
            tx.put(quota_key, count+1)
            if live:
                text = (f'Market Forge | 모멘텀 정보 | {symbol} {direction}\n'
                        f'가격 {price:g} | 기술 {technical:g}/100 | RSI {rsi:g}\n'
                        f'{"봉마감" if data["confirmed"] else "봉 진행 중·변동 가능"}\n'
                        '종합 80점 진입 신호 아님 · 호가/스프레드 미확인')
                tx.enqueue(key, {'text':text, 'expires_at':(now+timedelta(seconds=60)).isoformat()})
                result['delivery'] = 'queued'
        tx.put(key, result)
        tx.event(key, day, {'source':'tradingview_information', **result})
        return result
