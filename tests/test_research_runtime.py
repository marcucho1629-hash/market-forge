import unittest,tempfile
from datetime import datetime,timezone,timedelta
from unittest.mock import patch
from scanner.store import Store
from scanner.research.runtime import control,ingest,delivery,status
class RuntimeTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.s=Store(self.tmp.name+'/db');self.t=datetime(2026,10,5,14,0,tzinfo=timezone.utc)
  self.p=patch('scanner.research.runtime.current_session',return_value=({},True));self.p.start()
 def tearDown(self):self.p.stop();self.tmp.cleanup()
 def packet(self,sec,ok=True,kind='pulse'):
  t=(self.t+timedelta(seconds=sec)).isoformat()
  return dict(source='MF_V13_34_SHADOW',schema_version=6,bank=1,emitted_at=t,observations=[dict(ticker='MSFT',feed='BATS:MSFT',confirmed_parity=True,preview_state_unchanged=True,core_preview_mc=ok,core_preview_scmp=False,chart_mc=ok,chart_scmp=False,kind=kind,observed_at=t,bar_start=self.t.isoformat(),bar_end=(self.t+timedelta(seconds=300)).isoformat(),price=100.,previous_atr=2.)])
 def step(self,sec,**kw):return ingest(self.s,self.packet(sec,**kw),self.t+timedelta(seconds=sec))
 def test_disabled_by_default(self):
  self.step(60);self.step(88)
  self.assertEqual(delivery(self.s,lambda x:self.fail('sent'),self.t+timedelta(seconds=88)),[])
 def test_send_confirm(self):
  control(self.s,True,self.t);self.step(60);self.step(88);sent=[]
  delivery(self.s,sent.append,self.t+timedelta(seconds=88));self.assertEqual(len(sent),1)
  self.step(300,kind='closed');delivery(self.s,sent.append,self.t+timedelta(seconds=300));self.assertEqual(len(sent),2)
  self.assertIn('봉 마감 확인',sent[1])
 def test_cancel_before_send_expires_queue(self):
  control(self.s,True,self.t);self.step(60);self.step(88);self.step(90,ok=False)
  sent=[];delivery(self.s,sent.append,self.t+timedelta(seconds=90));self.assertEqual(sent,[])
 def test_control_disables_old_versions(self):
  control(self.s,True,self.t)
  with self.s.transaction() as tx:
   for v in ('mf130','mf131','mf132','mf133'):self.assertFalse(tx.get(v+'-control')['enabled'])
 def test_duplicate_not_second_send(self):
  control(self.s,True,self.t);self.step(60);self.step(88);self.step(88)
  sent=[];delivery(self.s,sent.append,self.t+timedelta(seconds=88));self.assertEqual(len(sent),1)
