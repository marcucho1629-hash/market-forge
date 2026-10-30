import os,tempfile,unittest
from unittest.mock import patch,Mock
from fastapi import FastAPI
from fastapi.testclient import TestClient
from scanner.store import Store
from scanner.v136_live.web import install
class Routes(unittest.TestCase):
 def setUp(self):
  self.f=tempfile.NamedTemporaryFile();self.store=Store(self.f.name);self.send=Mock()
  self.app=FastAPI();install(self.app,lambda:self.store,self.send)
  self.client=TestClient(self.app)
 def tearDown(self):self.f.close()
 def test_shadow_read_requires_auth(self):
  with patch.dict(os.environ,{'CHART_NEWS_WEBHOOK_SECRET':'test-only'}):
   self.assertEqual(self.client.post('/research-v136-live/control',data={'action':'shadow_run','secret':'wrong'}).status_code,401)
 def test_shadow_run_never_sends_or_enables(self):
  with patch.dict(os.environ,{'CHART_NEWS_WEBHOOK_SECRET':'test-only'}):
   r=self.client.post('/research-v136-live/control',data={'action':'shadow_run','secret':'test-only'})
   self.assertEqual(r.status_code,200);self.assertIn('shadow_no_telegram',r.text)
  self.send.assert_not_called()
  with self.store.transaction() as tx:
   self.assertIsNone(tx.get('research136live:control'));self.assertEqual(tx.execute('SELECT COUNT(*) FROM mf_outbox').fetchone()[0],0)
if __name__=='__main__':unittest.main()
