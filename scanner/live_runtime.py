"""Recurring, calendar-gated operation with bounded news calls and sent-state cleanup."""
from datetime import timedelta
from .calendar import current_session
from .core import stamp,ET
from .chart_news import UNIVERSE
from .live_signals import VERSION,reconcile,delivery,DENVER
from .bigdata import refresh
from .news_review import review_latest

DAILY_NEWS_LIMIT=140

def worker(store,send,now):
    session,active=current_session(now);day=session['date'];ticker=None
    with store.transaction() as tx:
        control=tx.get('mf130-control',{})
        if not control.get('enabled') or control.get('version')!=VERSION:return {'status':'disabled','session':session}
        tx.put('mf130-worker-last',now.isoformat())
        if not session['is_open']:return {'status':'market_holiday','session':session}
        opens=stamp(session['open']);closes=stamp(session['close'])
        if now>=closes:
            for symbol in UNIVERSE:
                key=f'mf130-state:{day}:{symbol}';state=reconcile(tx,key,tx.get(key,{}))
                if not state.get('active') or state.get('pending'):continue
                latest=tx.get('mf130-latest:'+symbol,{})
                at=stamp(latest.get('observed_at',state['observed_at'])).astimezone(DENVER).strftime('%H:%M:%S')
                identity=f'mf130:close:{day}:{symbol}'
                text=f'✖ {symbol} X · 장 마감, 후보 추적 종료\n마지막 관측 ${latest.get("price",state["entry_price"]):.2f} ({at})\n{now.astimezone(DENVER).strftime("%H:%M:%S")} 덴버 · 체결 확인 아님'
                tx.enqueue(identity,{'text':text,'expires_at':(closes+timedelta(minutes=10)).isoformat(),'ticker':symbol,'code':'X','session':day,'observed_at':now.isoformat()})
                state['pending']={'id':identity,'next':{'active':False,'last_alert':now.isoformat(),'closed_reason':'session_close'}};tx.put(key,state)
        if opens-timedelta(minutes=90)<=now<closes:
            count=tx.get('mf130-news-count:'+day,0)
            intraday=max(0,(now-opens).total_seconds())
            allowed=70 if now<opens else min(DAILY_NEWS_LIMIT,71+int(intraday/max(1,(closes-opens).total_seconds())*69))
            minute=int(now.timestamp())//60
            if count<allowed and tx.get('mf130-news-minute')!=minute:
                candidates=[]
                for symbol in UNIVERSE:
                    refreshed=tx.get('mf130-news-refreshed:'+day+':'+symbol,0)
                    latest=tx.get('mf130-latest:'+symbol,{})
                    recent=latest and (now-stamp(latest['observed_at'])).total_seconds()<600
                    if now.timestamp()-refreshed>=1800:
                        candidates.append((refreshed==0, bool(recent and latest['chart_score']>=60),-refreshed,symbol))
                if candidates:
                    ticker=max(candidates)[3]
                    tx.put('mf130-news-minute',minute);tx.put('mf130-news-count:'+day,count+1)
                    tx.put('mf130-news-refreshed:'+day+':'+ticker,now.timestamp())
    sent=delivery(store,send,now)
    news=None
    if ticker:
        news=refresh(store,ticker,now,timeout=(3,10))
        with store.transaction() as tx:
            docs=[tx.get('quality-news-document:'+r['id']) for r in news.get('documents',[])]
        docs=sorted((d for d in docs if d and d.get('published_at')),key=lambda d:d['published_at'],reverse=True)
        news['review']=review_latest(store,ticker,[d['id'] for d in docs[:1]],now)
        with store.transaction() as tx:
            tx.event('mf130-news:'+now.isoformat(),day,{'source':'mf130_news','ticker':ticker,'observed_at':now.isoformat(),'result':news})
    return {'status':'ok','session':session,'market_open':active,'ticker':ticker,'news':news,'delivery':sent,'daily_news_limit':DAILY_NEWS_LIMIT}

def status(store,now):
    session,active=current_session(now)
    with store.transaction() as tx:
        control=tx.get('mf130-control',{})
        banks={str(i):tx.get('mf130-bank:'+str(i)) for i in range(1,8)}
        states={t:reconcile(tx,f'mf130-state:{session["date"]}:{t}',tx.get(f'mf130-state:{session["date"]}:{t}',{})) for t in UNIVERSE}
        worker=tx.get('mf130-worker-last');count=tx.get('mf130-news-count:'+session['date'],0)
        deliveries=tx.execute("SELECT status,payload FROM mf_outbox WHERE id LIKE ?",('mf130:%',)).fetchall()
    import json
    counts={}
    for st,payload in deliveries:
        if json.loads(payload).get('session')==session['date']:counts[st]=counts.get(st,0)+1
    return {'version':VERSION,'control':control,'session':session,'market_open':active,'banks':banks,'worker_last_at':worker,'news_calls_today':count,'daily_news_limit':DAILY_NEWS_LIMIT,'delivery_today':counts,'tracked_sent_candidates':[t for t,s in states.items() if s.get('active')],'pending_lifecycle':[t for t,s in states.items() if s.get('pending')],'weights':{'chart':80,'bigdata':20},'grades':[70,75,80],'source':'TradingView BATS 5m','pulse_seconds':15,'execution':'every_trading_day' if control.get('enabled') else 'disabled','universe':list(UNIVERSE)}
