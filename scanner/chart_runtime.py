"""One-session test controls and durable delivery, separate from legacy Alpaca."""
import hashlib
from datetime import timedelta
from .core import ET,stamp
from .chart_news import UNIVERSE
from .store import drain
from .bigdata import refresh
from .news_review import review_latest


def active_control(tx,now):
    control=tx.get('chart-news-control',{})
    return control if control.get('enabled') and stamp(control['expires_at'])>now else {}


def after_intake(store,payload,result,now):
    day=now.astimezone(ET).date().isoformat()
    with store.transaction() as tx:
        control=active_control(tx,now)
        for row,d in zip(payload['observations'],result['decisions']):
            if not result['duplicate']:
                tx.put('chart-news-latest:'+row['ticker'],{'observed_at':row['observed_at'],'chart_score':d['chart_score']})
            if not control or not (d['entry_selected'] or d['information_selected']):continue
            identity='chart-news-live:'+hashlib.sha256((row['ticker']+row['observed_at']+row['direction']).encode()).hexdigest()
            kind='80점 이상 후보' if d['entry_selected'] else '모멘텀 정보 · 80점 후보 아님'
            total=f"{d['total_score']:g}/100" if d['total_score'] is not None else '뉴스 검증 대기'
            text=(f"Market Forge TEST | {kind}\n{d['ticker']} {d['direction']} | 차트 {d['chart_score']:g}/80\n"
                  f"종합 {total} | 관측 가격 {row['price']:g}\n5분봉 확정 | {d['observed_at']}\n호가·현재 체결가 미확인")
            if d['evidence']:text+='\n뉴스: '+d['evidence']['url']
            tx.enqueue(identity,{'text':text,'expires_at':min(stamp(control['expires_at']),stamp(row['observed_at'])+timedelta(seconds=60)).isoformat()})
    return {**result,'mode':'live_test' if control else 'shadow','delivery':'queued_if_selected' if control else 'disabled'}


def worker(store,send,now):
    with store.transaction() as tx:
        control=active_control(tx,now)
        if not control:return {'status':'test_disabled_or_expired'}
        day=now.astimezone(ET).date().isoformat()
        session=tx.get('tv-session:'+day)
        if not session or not session.get('is_open') or not stamp(session['open'])<=now<stamp(session['close']):
            return {'status':'outside_verified_session'}
        count=tx.get('chart-news-paid:'+day,0)
        candidates=[]
        if count<70:
            for ticker in UNIVERSE:
                latest=tx.get('chart-news-latest:'+ticker)
                state=tx.get('chart-news-refresh-state:'+ticker)
                if latest and (now-stamp(latest['observed_at'])).total_seconds()<1800 and (not state or now.timestamp()-state>600):
                    candidates.append((latest['chart_score'],ticker))
        ticker=max(candidates)[1] if candidates else None
        if ticker:
            tx.put('chart-news-paid:'+day,count+1)
            tx.put('chart-news-refresh-state:'+ticker,now.timestamp())
    delivery=drain(store,send,prefix='chart-news-live:',now=now)
    news=None
    if ticker:
        news=refresh(store,ticker,now)
        review=review_latest(store,ticker,[r['id'] for r in news.get('documents',[])],now)
        news['review']=review
    return {'status':'ok','ticker':ticker,'news':news,'delivery':delivery,'daily_refresh_limit':70}
