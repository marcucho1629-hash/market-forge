import unittest,tempfile
from unittest.mock import patch
from datetime import datetime,timezone,timedelta
from scanner.store import Store
from scanner.simple.runtime import ingest,control,delivery,status
class IntegrationTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.db=Store(self.tmp.name+'/test.db');self.t=datetime(2026,10,14,13,30,tzinfo=timezone.utc)
  self.p=patch('scanner.simple.runtime.current_session',return_value=({'is_open':True,'open':self.t.isoformat(),'close':(self.t+timedelta(hours=6,minutes=30)).isoformat()},True));self.p.start()
 def tearDown(self):self.p.stop();self.tmp.cleanup()
 def packet(self,tf='1',mc=True):
  end=self.t+timedelta(minutes=int(tf));now=end+timedelta(seconds=1)
  row=dict(ticker='MSFT',feed='BATS:MSFT',timeframe=tf,closed=True,bar_start=self.t.isoformat(),bar_end=end.isoformat(),mc=mc,scmp=False,choppy=False,exit='',exit_side=0,price=100.)
  return dict(source='MF_V13_35',schema_version=7,bank=1,emitted_at=now.isoformat(),observations=[row]),now
 def test_enabled_send_and_dedupe(self):
  control(self.db,True,self.t);p,n=self.packet();ingest(self.db,p,n);ingest(self.db,p,n);sent=[];delivery(self.db,sent.append,n);self.assertEqual(len(sent),1)
  p,n=self.packet('5');ingest(self.db,p,n);delivery(self.db,sent.append,n);self.assertEqual(len(sent),2);self.assertIn('CONFIRM MC',sent[1])
 def test_disabled_does_not_send(self):
  p,n=self.packet();ingest(self.db,p,n);self.assertEqual(delivery(self.db,lambda _:self.fail('sent'),n),[])
 def test_row_failure_does_not_kill_batch(self):
  p,n=self.packet();p['observations'].insert(0,dict(p['observations'][0],feed='bad'));r=ingest(self.db,p,n);self.assertEqual(r['accepted'],1);self.assertEqual(len(r['rejected']),1)
 def test_unconfirmed_message(self):
  control(self.db,True,self.t);p,n=self.packet();ingest(self.db,p,n);sent=[];delivery(self.db,sent.append,n)
  p,n=self.packet('5',False);ingest(self.db,p,n);delivery(self.db,sent.append,n);self.assertIn('후보 미확정 종료',sent[-1])
 def test_activation_disables_v134(self):
  control(self.db,True,self.t)
  with self.db.transaction() as tx:self.assertFalse(tx.get('mf134-control')['enabled'])

 def test_delayed_early_is_not_delivered(self):
  control(self.db,True,self.t);p,n=self.packet();n+=timedelta(seconds=65);p['emitted_at']=n.isoformat();ingest(self.db,p,n)
  self.assertEqual(delivery(self.db,lambda _:self.fail('stale early'),n),[])
 def test_close_expires_unsent_early(self):
  control(self.db,True,self.t);p,n=self.packet();ingest(self.db,p,n)
  p,n=self.packet('5');ingest(self.db,p,n);sent=[];delivery(self.db,sent.append,n)
  self.assertEqual(len(sent),1);self.assertIn('CONFIRM',sent[0])
