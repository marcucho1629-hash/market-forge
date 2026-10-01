import unittest
from datetime import timedelta
from scanner.fast_delivery import control, delivery, active
from scanner.fast_path import sweep
from test_fast_path import FastPath

class Delivery(unittest.TestCase):
 setUp=FastPath.setUp
 tearDown=FastPath.tearDown
 row=FastPath.row
 batch=FastPath.batch
 def activate(self):control(self.db,True,self.t)
 def send(self,seconds=15):
  now=self.t+timedelta(seconds=seconds);messages=[]
  result=delivery(self.db,messages.append,now,clock=lambda:now)
  return messages,result
 def prepare(self):
  self.batch([dict(self.row(),kind='pulse')]);self.batch([dict(self.row(15),kind='pulse')],self.t+timedelta(seconds=15))
 def test_live_entry_once_and_cancel(self):
  self.activate();self.prepare();msgs,r=self.send();self.assertEqual(len(msgs),1);self.assertIn('미확정',msgs[0]);self.assertEqual(r[0]['status'],'sent')
  self.assertEqual(self.send()[0],[])
  self.batch([dict(self.row(30,price=500.1,rsi=49),kind='pulse')],self.t+timedelta(seconds=30))
  msgs,_=self.send(30);self.assertEqual(len(msgs),1);self.assertIn('후보 취소',msgs[0])
 def test_cancel_before_delivery_suppresses_entry(self):
  self.activate();self.prepare()
  self.batch([dict(self.row(30,price=500.1,rsi=49),kind='pulse')],self.t+timedelta(seconds=30))
  self.assertEqual(self.send(30)[0],[])
 def test_expired_parent_never_notifies(self):
  self.activate();self.prepare();self.assertEqual(self.send(40)[0],[])
  self.batch([dict(self.row(241,chart_mc=True),kind='closed')],self.t+timedelta(seconds=241))
  self.assertEqual(self.send(241)[0],[])
 def test_close_confirm_and_missing_close_expiry(self):
  self.activate();self.prepare();self.send()
  self.batch([dict(self.row(241,chart_mc=True),kind='closed')],self.t+timedelta(seconds=241))
  msgs,_=self.send(241);self.assertEqual(len(msgs),1);self.assertIn('봉 마감 확인',msgs[0]);self.assertEqual(self.send(250)[0],[])
 def test_missing_close_not_price_stop(self):
  self.activate();self.prepare();self.send();sweep(self.db,self.t+timedelta(seconds=301))
  msgs,_=self.send(301);self.assertEqual(len(msgs),1);self.assertIn('마감 확인 누락',msgs[0]);self.assertNotIn('$',msgs[0])
 def test_shadow_state_not_reused_live(self):
  self.prepare();self.activate()
  result=self.batch([dict(self.row(30),kind='pulse')],self.t+timedelta(seconds=30))
  self.assertEqual(result['decisions'][0]['event'],'PREP');self.assertEqual(self.send(30)[0],[])
 def test_cutover_disables_old_versions(self):
  self.activate()
  with self.db.transaction() as tx:
   for p in ('mf130','mf131','mf132'):self.assertFalse(tx.get(p+'-control')['enabled'])
 def test_daily_holiday_and_postclose(self):
  self.activate()
  with self.db.transaction() as tx:cfg=tx.get('mf133-control')
  self.assertTrue(active(cfg,self.t));self.assertTrue(active(cfg,self.t+timedelta(days=3)))
  self.assertFalse(active(cfg,self.t+timedelta(days=1)))
  late=self.t.replace(hour=20,minute=1)
  self.assertTrue(active(cfg,late));self.assertFalse(active(cfg,late,True));self.assertFalse(active(cfg,late+timedelta(minutes=3)))
 def test_disable_cancels_pending(self):
  self.activate();self.prepare();control(self.db,False,self.t+timedelta(seconds=16));self.assertEqual(self.send(16)[0],[])
