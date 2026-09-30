import unittest,tempfile
from datetime import datetime,timezone,timedelta
from scanner.store import Store
from scanner.early_test import ingest,delivery,control,VERSION
import test_engine_relay

class EarlyIntegration(unittest.TestCase):
 def setUp(self):
  self.fixture=test_engine_relay.RelayTests();self.fixture.setUp();self.db=self.fixture.db;self.now=self.fixture.now
  self.row={**self.fixture.row,'chart_mc':False,'early_mc_raw':True,'early_scmp_raw':False,'early_long_distance_atr':.4,'early_short_distance_atr':-1.0}
  with self.db.transaction() as tx:tx.put('mf132-control',{'enabled':True,'version':VERSION,'test_date':'2026-09-25'})
 def tearDown(self):self.fixture.tearDown()
 def runrow(self,kind='pulse',bank=1,**kw):
  return ingest(self.db,{'source':VERSION,'schema_version':4,'bank':bank,'kind':kind,'observations':[{**self.row,'observed_at':self.now.isoformat(),**kw}]},self.now)
 def send(self):
  sent=[];delivery(self.db,sent.append,self.now,clock=lambda:self.now);return sent
 def close(self,flag):
  self.now=self.now.replace(minute=35,second=1);self.runrow('closed',chart_mc=flag)
 def test_real_ingest_before_close_sends_early(self):
  self.runrow();m=self.send();self.assertEqual(len(m),1);self.assertIn('EARLY MC',m[0]);self.assertIn('미확정',m[0])
 def test_confirm_is_update_not_second_entry(self):
  self.runrow();self.send();self.close(True);m=self.send();self.assertEqual(len(m),1);self.assertIn('확정 MC',m[0]);self.assertIn('새 진입 아님',m[0])
 def test_cancel_not_stop_order(self):
  self.runrow();self.send();self.close(False);m=self.send();self.assertIn('후보 취소',m[0]);self.assertNotIn(' X',m[0])
 def test_no_early_no_late_entry(self):
  self.close(True);self.assertEqual(self.send(),[])
 def test_chase_rejected_and_not_reentered_on_pullback(self):
  self.runrow(early_long_distance_atr=1.1);self.now+=timedelta(seconds=15);self.runrow();self.assertEqual(self.send(),[])
 def test_shadow_never_sends(self):
  with self.db.transaction() as tx:tx.put('mf132-control',{})
  self.runrow();self.assertEqual(self.send(),[])
 def test_only_authorized_twenty(self):
  with self.assertRaises(ValueError):self.runrow(ticker='CRWD',feed='BATS:CRWD')
  with self.assertRaises(ValueError):self.runrow(bank=7)
 def test_wrong_day_never_sends(self):
  with self.db.transaction() as tx:tx.put('mf132-control',{'enabled':True,'test_date':'2026-09-24'})
  self.runrow();self.assertEqual(self.send(),[])
 def test_duplicate_request_one_message(self):
  self.runrow();self.runrow();self.assertEqual(len(self.send()),1)
 def test_failed_parent_no_confirmation(self):
  self.runrow();self.now+=timedelta(seconds=31);self.assertEqual(self.send(),[]);self.close(True);self.assertEqual(self.send(),[])
 def test_outside_session_queue_expired(self):
  self.runrow();self.now=self.now.replace(hour=21);self.assertEqual(self.send(),[])
 def test_no_new_bar_repeat_while_active(self):
  self.runrow();self.send();self.close(True);self.send();self.now=self.now.replace(minute=36)
  self.runrow(bar_start='2026-09-25T13:35:00+00:00',bar_end='2026-09-25T13:40:00+00:00',benchmark_start='2026-09-25T13:35:00+00:00');self.assertEqual(self.send(),[])
 def test_calendar_cutover_disables_old_and_not_auto_restore(self):
  now=datetime(2026,9,29,23,tzinfo=timezone.utc)
  with self.db.transaction() as tx:tx.put('mf131-control',{'enabled':True})
  control(self.db,True,'2026-09-30',now)
  with self.db.transaction() as tx:self.assertFalse(tx.get('mf131-control')['enabled'])
  control(self.db,False,'2026-09-30',now)
  with self.db.transaction() as tx:self.assertFalse(tx.get('mf131-control')['enabled'])
 def test_weekend_test_date_rejected(self):
  with self.assertRaises(ValueError):control(self.db,True,'2026-10-03',datetime(2026,9,29,23,tzinfo=timezone.utc))
 def test_postgres_parameter_parsing_for_expiry_paths(self):
  from unittest.mock import patch
  from psycopg._queries import _split_query
  from scanner.store import Transaction
  execute=Transaction.execute
  def checked(tx,sql,args=()):
   _split_query(sql.replace('?', '%s').encode())
   return execute(tx,sql,args)
  with patch.object(Transaction,'execute',checked):
   self.runrow();self.now=self.now.replace(hour=21)
   self.assertEqual(self.send(),[])
   control(self.db,False,'2026-09-25',self.now)
