import os
import unittest
from unittest.mock import patch,Mock
import requests
import test_tv_routes
from scanner.bigdata import search,refresh,BigdataError

class Bigdata(unittest.TestCase):
    setUp=test_tv_routes.Routes.setUp
    tearDown=test_tv_routes.Routes.tearDown
    def test_auth_required(self):
        for path in ['refresh','test-telegram']:
            self.assertEqual(self.client.post('/scanner/chart-news/'+path,json={}).status_code,401)
        self.assertEqual(self.client.get('/scanner/chart-news/connections').status_code,401)
    @patch.dict(os.environ,{'BIGDATA_API_KEY':'private-key'})
    @patch('scanner.bigdata.requests.post')
    def test_import_and_cache(self,post):
        from index import now_utc
        now=now_utc()
        post.return_value=Mock(status_code=200,json=lambda:{'results':[{'id':'doc1','headline':'News','url':'https://example.com/n','timestamp':now.isoformat(),'chunks':[{'text':'Body excerpt.'}]}]})
        first=refresh(self.db,'NVDA',now)
        self.assertTrue(first['authenticated'])
        self.assertEqual(first['scoring_status'],'review_required')
        self.assertTrue(refresh(self.db,'NVDA',now)['cached'])
        post.assert_called_once()
        self.assertEqual(post.call_args.kwargs['headers']['X-API-KEY'],'private-key')
    @patch.dict(os.environ,{'BIGDATA_API_KEY':'private-key'})
    @patch('scanner.bigdata.requests.post')
    def test_errors_redacted(self,post):
        from index import now_utc
        for status in [401,402,403,429,500]:
            post.return_value=Mock(status_code=status,text='private-key')
            with self.assertRaises(BigdataError) as e:search('SPY',now_utc())
            self.assertNotIn('private-key',str(e.exception))
        post.side_effect=requests.Timeout('private-key')
        with self.assertRaises(BigdataError) as e:search('SPY',now_utc())
        self.assertNotIn('private-key',str(e.exception))
    def test_telegram_test_deduplicated(self):
        with patch('index.send_telegram') as send:
            for _ in range(2):
                r=self.client.post('/scanner/chart-news/test-telegram',json={'test_id':'test-20260924'},headers={'Authorization':'Bearer testadmin'})
                self.assertEqual(r.status_code,200)
            send.assert_called_once()
            self.assertTrue(r.json()['duplicate'])
