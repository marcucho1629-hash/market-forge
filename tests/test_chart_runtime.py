import unittest
from unittest.mock import patch,Mock
from datetime import timedelta
import json,os
import test_chart_news
from scanner.chart_news import shadow_batch
from scanner.chart_runtime import after_intake,worker
from scanner.bigdata import api_timestamp
from scanner.news_review import review_latest

class Runtime(unittest.TestCase):
    setUp=test_chart_news.ChartNewsTests.setUp
    tearDown=test_chart_news.ChartNewsTests.tearDown
    news=test_chart_news.ChartNewsTests.news
    def control(self):
        with self.db.transaction() as tx:tx.put('chart-news-control',{'enabled':True,'expires_at':(self.now+timedelta(hours=1)).isoformat()})
    def intake(self):
        p={'observations':[self.row]}
        return after_intake(self.db,p,shadow_batch(self.db,p,self.now),self.now)
    def test_adapter_utc(self):
        self.assertEqual(api_timestamp('2026-09-24T10:42:54'),'2026-09-24T10:42:54+00:00')
        self.assertTrue(api_timestamp('2026-09-24T10:42:54-04:00').endswith('-04:00'))
    def test_off_by_default(self):
        self.intake()
        with self.db.transaction() as tx:self.assertEqual(tx.execute('SELECT COUNT(*) FROM mf_outbox').fetchone()[0],0)
    def test_live_dedup_and_expiry(self):
        self.control();self.news();self.intake();self.intake()
        with self.db.transaction() as tx:self.assertEqual(tx.execute('SELECT COUNT(*) FROM mf_outbox').fetchone()[0],1)
        with patch('scanner.chart_runtime.refresh',return_value={'documents':[]}),patch('scanner.chart_runtime.review_latest',return_value={}),patch('scanner.chart_runtime.drain') as send:
            self.assertEqual(worker(self.db,lambda _:None,self.now+timedelta(hours=2))['status'],'test_disabled_or_expired')
            send.assert_not_called()
    def test_chop_never_queues(self):
        self.control();self.row['choppy']=True;self.intake()
        with self.db.transaction() as tx:self.assertEqual(tx.execute('SELECT COUNT(*) FROM mf_outbox').fetchone()[0],0)
    def test_daily_budget(self):
        self.control();self.intake()
        with self.db.transaction() as tx:tx.put('chart-news-paid:2026-09-23',70)
        with patch('scanner.chart_runtime.refresh') as call:
            self.assertIsNone(worker(self.db,lambda _:None,self.now)['ticker']);call.assert_not_called()
    @patch.dict(os.environ,{'OPENAI_API_KEY':'fake'})
    @patch('scanner.news_review.OpenAI')
    def test_no_direction_is_not_scored(self,client):
        self.news()
        client.return_value.responses.create.return_value=Mock(output_text=json.dumps({'relevant':True,'direction':'NEUTRAL'}))
        r=review_latest(self.db,'META',['METAnews'],self.now)
        self.assertEqual(r['reviews'][0]['status'],'no_directional_evidence')
    @patch.dict(os.environ,{'OPENAI_API_KEY':'fake'})
    @patch('scanner.news_review.OpenAI')
    def test_fabricated_excerpt_rejected(self,client):
        self.news()
        item=dict(relevant=True,direction='LONG',category='news',impact=5,novelty=5,certainty=5,directness=5,supporting_excerpt='fabricated quote',rationale='invalid')
        client.return_value.responses.create.return_value=Mock(output_text=json.dumps(item))
        r=review_latest(self.db,'META',['METAnews'],self.now)
        self.assertEqual(r['reviews'][0]['status'],'review_unavailable')

    def test_valid_machine_review(self):
        from scanner.news import import_documents
        import_documents(self.db,[dict(id='new-document',url='https://example.com/earnings',title='Synthetic earnings',timestamp=self.now.isoformat(),text='Synthetic earnings beat and guidance raise.')],self.now)
        item=dict(relevant=True,direction='LONG',category='news',impact=4,novelty=4,certainty=4,directness=4,supporting_excerpt='Synthetic earnings beat and guidance raise.',rationale='Synthetic fixture only')
        with patch.dict(os.environ,{'OPENAI_API_KEY':'fake'}),patch('scanner.news_review.OpenAI') as client,patch('scanner.news_review.datetime') as clock:
            clock.now.return_value=self.now
            client.return_value.responses.create.return_value=Mock(output_text=json.dumps(item))
            r=review_latest(self.db,'META',['new-document'],self.now)
        self.assertEqual(r['reviews'][0]['status'],'reviewed')
        with self.db.transaction() as tx:self.assertEqual(tx.get('quality-evidence:META:LONG:news')['score'],80)
