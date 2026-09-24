"""Bounded REST ingestion. API success never implies that news has been scored."""
import os
from datetime import datetime, timedelta, timezone
import requests
from .chart_news import UNIVERSE
from .news import import_documents


class BigdataError(Exception):
    pass


def api_timestamp(raw):
    # Official bigdata-client 2.21.0 document.py: all returned timestamps are UTC.
    value=datetime.fromisoformat(raw.replace('Z','+00:00'))
    return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value).isoformat()


def search(ticker, now):
    if ticker not in UNIVERSE:
        raise ValueError('unknown ticker')
    key=os.getenv('BIGDATA_API_KEY','')
    if not key:
        raise BigdataError('bigdata_key_missing')
    body={'search_mode':'fast','query':{
        'text':f'Latest material company news, earnings, guidance and regulatory catalysts for US listed ticker {ticker}',
        'filters':{'timestamp':{'start':(now-timedelta(hours=3)).isoformat(),'end':now.isoformat()}},
        'max_chunks':3}}
    try:
        response=requests.post('https://api.bigdata.com/v1/search',
            headers={'X-API-KEY':key,'Content-Type':'application/json'},json=body,timeout=(5,20))
    except requests.RequestException:
        raise BigdataError('bigdata_network_error') from None
    if response.status_code!=200:
        status=response.status_code
        label={401:'authentication_failed',403:'access_denied',402:'credit_required',429:'rate_limited'}.get(status,'upstream_error')
        raise BigdataError(f'bigdata_{label}_{status}')
    try:
        data=response.json()
        rows=data['results']
        if not isinstance(rows,list): raise ValueError()
        documents=[]
        for r in rows[:3]:
            chunks=r.get('chunks',[])
            text='\n\n'.join(c['text'] for c in chunks if isinstance(c.get('text'),str))
            documents.append({'id':r['id'],'url':r['url'],'title':r['headline'],
                              'timestamp':api_timestamp(r['timestamp']),'text':text or None})
        return documents
    except (ValueError,KeyError,TypeError,AttributeError):
        raise BigdataError('bigdata_invalid_response') from None


def refresh(store,ticker,now):
    if ticker not in UNIVERSE: raise ValueError('unknown ticker')
    cache_key='bigdata-refresh:'+ticker
    # Reserve before the paid call, so concurrent requests cannot fan out.
    with store.transaction() as tx:
        old=tx.get(cache_key)
        if old and now.timestamp()-old['requested_at']<300:
            return {**old,'cached':True}
        tx.put(cache_key,{'status':'pending','ticker':ticker,'requested_at':now.timestamp()})
    try:
        documents=search(ticker,now)
        # Repair only this adapter's existing offset-free receipt; retain first-seen.
        with store.transaction() as tx:
            for doc in documents:
                key='quality-news-document:'+doc['id']
                old=tx.get(key)
                if old and old.get('status')=='timezone_unverified' and api_timestamp(old['raw_timestamp'])==doc['timestamp']:
                    old['published_at']=doc['timestamp']
                    old['status']='ready_for_review' if old.get('text') and datetime.fromisoformat(doc['timestamp'])<=now else 'future_publication'
                    old['timezone_basis']='official bigdata-client 2.21.0 document.py: UTC'
                    tx.put(key,old)
        imported=import_documents(store,documents,now) if documents else {'documents':[]}
        result={'status':'documents_received' if documents else 'no_results',
                'authenticated':True,'ticker':ticker,'requested_at':now.timestamp(),
                'documents':imported['documents'],'scoring_status':'review_required',
                'telegram_sent':0,'cached':False}
    except BigdataError as exc:
        result={'status':'error','error':str(exc),'ticker':ticker,'requested_at':now.timestamp(),'cached':False}
    except (ValueError,KeyError,TypeError):
        result={'status':'error','error':'bigdata_document_validation_failed','ticker':ticker,'requested_at':now.timestamp(),'cached':False}
    with store.transaction() as tx: tx.put(cache_key,result)
    return result
