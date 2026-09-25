"""Auditable news inbox: preserve first-seen time; never infer score or timezone.

This is a server ingestion contract, not a Bigdata polling/streaming client.
Authenticated upstream workers deliver search records and fetched document text.
"""
from datetime import datetime
from urllib.parse import urlparse
import re
from .core import stamp, finite, ET


def required_text(value, name, maximum=2000):
    if not isinstance(value, str) or not value.strip() or len(value)>maximum:
        raise ValueError(f'invalid {name}')
    return value.strip()


def import_documents(store, records, now):
    if not isinstance(records, list) or not 1<=len(records)<=100:
        raise ValueError('1..100 documents required')
    clean=[]
    for item in records:
        identity=required_text(item['id'],'document id',128)
        url=required_text(item['url'],'url')
        if urlparse(url).scheme not in ('http','https') or not urlparse(url).netloc:
            raise ValueError('invalid document URL')
        title=required_text(item['title'],'title')
        raw=required_text(item['timestamp'],'timestamp',80)
        parsed=datetime.fromisoformat(raw.replace('Z','+00:00'))
        # Bigdata search can return offset-free times. Do not silently assume UTC.
        when=stamp(raw) if parsed.tzinfo is not None else None
        text=item.get('text')
        if text is not None: text=required_text(text,'document body',200000)
        status='ready_for_review'
        if when is None: status='timezone_unverified'
        elif when>now: status='future_publication'
        elif not text or text.strip()==title.strip(): status='headline_only'
        clean.append(dict(id=identity,url=url,title=title,raw_timestamp=raw,
                          published_at=when.isoformat() if when else None,
                          text=text,status=status,source='Bigdata.com'))
    if len({r['id'] for r in clean})!=len(clean): raise ValueError('duplicate document ids in batch')
    day=now.astimezone(ET).date().isoformat()
    results=[]
    with store.transaction() as tx:
        for item in clean:
            key='quality-news-document:'+item['id']
            old=tx.get(key)
            if old:
                # Immutable raw receipt prevents later text from silently changing a review.
                results.append({'id':item['id'],'status':old['status'],'duplicate':True})
                continue
            item['first_seen_at']=now.isoformat()
            tx.put(key,item)
            tx.event(key,day,{'source':'quality_news_inbox',**item})
            results.append({'id':item['id'],'status':item['status'],'duplicate':False})
    return {'mode':'shadow','documents':results,'telegram_sent':0}


def review_document(store, data, now):
    """Store an explicit evidence review, never manufacture a score from a headline."""
    doc_id=required_text(data['document_id'],'document id',128)
    ticker=required_text(data['ticker'],'ticker',15)
    if not re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,14}',ticker): raise ValueError('uppercase ticker required')
    category=data['category'];direction=data['direction']
    if category not in ('news','catalyst') or direction not in ('LONG','SHORT'):
        raise ValueError('invalid category or direction')
    score=finite(data['score'],0,100)
    event_id=required_text(data['event_id'],'canonical event id',200)
    rationale=required_text(data['rationale'],'rationale',4000)
    excerpt=required_text(data['supporting_excerpt'],'supporting excerpt',2000)
    reviewer=required_text(data['reviewer'],'reviewer',200)
    day=now.astimezone(ET).date().isoformat()
    with store.transaction() as tx:
        doc=tx.get('quality-news-document:'+doc_id)
        if not doc or doc['status']!='ready_for_review':
            raise ValueError('document is missing or not ready for review')
        if excerpt not in doc['text']:
            raise ValueError('supporting excerpt must occur in fetched body')
        published=stamp(doc['published_at'])
        if stamp(doc['first_seen_at'])>now or published>now:
            raise ValueError('future evidence')
        max_age=3600 if category=='news' else 10800
        if (now-published).total_seconds()>max_age:
            raise ValueError('publication too old; re-review does not refresh news')
        # Reviewed evidence becomes available now, never retroactively at publication.
        evidence=dict(source='Bigdata.com',url=doc['url'],event_id=event_id,
                      document_id=doc_id,published_at=published.isoformat(),known_at=now.isoformat(),
                      first_seen_at=doc['first_seen_at'],category=category,direction=direction,
                      score=score,rationale=rationale,supporting_excerpt=excerpt,reviewer=reviewer)
        key=f'quality-evidence:{ticker}:{direction}:{category}'
        old=tx.get(key)
        if old and stamp(old['known_at'])>now: raise ValueError('out-of-order review')
        # A repeated review of the same document cannot renew its known-at lifetime.
        if old and old['document_id']==doc_id:
            if any(old[k]!=evidence[k] for k in ('event_id','score','rationale','supporting_excerpt','reviewer')):
                raise ValueError('review already recorded; conflicting update rejected')
            return {'mode':'shadow','duplicate':True,'evidence':old,'telegram_sent':0}
        pool=tx.get(key+':pool',[old] if old else [])
        pool=[e for e in pool if e['document_id']!=doc_id and (now-stamp(e['published_at'])).total_seconds()<=10800]
        pool.append(evidence)
        tx.put(key+':pool',pool[-20:])
        if not old or published>=stamp(old['published_at']):
            tx.put(key,evidence)
        tx.event(key+':'+now.isoformat(),day,{'source':'quality_news_review','ticker':ticker,'evidence':evidence})
    return {'mode':'shadow','duplicate':False,'evidence':evidence,'telegram_sent':0}


def attach_evidence(store, payload, now):
    """Attach only evidence known by the observation, not merely request arrival."""
    observations=[]
    with store.transaction() as tx:
        for row in payload['observations']:
            merged=dict(row)
            observed=stamp(row['observed_at'])
            for category in ('news','catalyst'):
                if merged.get(category) is not None: continue
                item=tx.get(f'quality-evidence:{row["ticker"]}:{row["direction"]}:{category}')
                if item and stamp(item['known_at'])<=min(now,observed): merged[category]=item
            observations.append(merged)
    return {**payload,'observations':observations}
