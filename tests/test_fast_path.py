import unittest,tempfile
from datetime import datetime,timedelta,timezone
from pathlib import Path
from scanner.fast_path import step,gates,ingest,sweep,VERSION
from scanner.store import Store
import test_engine_relay

class FastPath(unittest.TestCase):
 def setUp(self):
  self.fixture=test_engine_relay.RelayTests();self.fixture.setUp();self.db=self.fixture.db
  self.t=datetime(2026,9,25,13,31,tzinfo=timezone.utc)
  self.r={**self.fixture.row,'observed_at':self.t.isoformat(),'open':500,'price':500.6,'high':500.65,'low':499.9,'atr':1,'previous_atr':1,'ema9':500.2,'ema21':500.1,'vwap':500.1,'rsi':57,'previous_rsi':53,'relative_volume':.5,'session_open':500,'previous_session_close':480,'session_start':self.fixture.row['bar_start'],'previous_high':482,'previous_low':478,'choppy':False,'wide_whipsaw':False,'elapsed_seconds':60,'chart_mc':False,'chart_scmp':False}
 def tearDown(self):self.fixture.tearDown()
 def row(self,seconds=0,**changes):
  return {**self.r,'observed_at':(self.t+timedelta(seconds=seconds)).isoformat(),'elapsed_seconds':60+seconds,**changes}
 def advance(self,s,seconds=0,kind='pulse',**changes):
  r=self.row(seconds,**changes);return step(s,r,kind,self.t+timedelta(seconds=seconds))
 def batch(self,rows,now=None):return ingest(self.db,{'source':VERSION,'schema_version':5,'bank':1,'observations':rows},now or self.t)
 def test_two_samples_not_first_spike(self):
  s,e=self.advance({});self.assertEqual(e,'PREP');self.assertFalse(s['sent'])
  s,e=self.advance(s,15);self.assertEqual(e,'SEND');self.assertTrue(s['sent'])
 def test_short_spike_never_sends(self):
  s,_=self.advance({});s,e=self.advance(s,15,price=500.1,rsi=49);self.assertEqual(e,'PREP_CANCEL');self.assertFalse(s['sent'])
 def test_57_second_gap_never_fast_confirms(self):
  s,_=self.advance({});s,e=self.advance(s,57);self.assertEqual(e,'EXPIRED');self.assertFalse(s['sent'])
 def test_duplicate_and_out_of_order_no_send(self):
  s,_=self.advance({});s,e=self.advance(s);self.assertIsNone(e)
  _,e=self.advance(s,-1);self.assertIsNone(e)
 def test_symmetric_gates(self):
  mirror=dict(self.r)
  for k in ('price','open','ema9','ema21','vwap','session_open','previous_session_close'):mirror[k]=1000-self.r[k]
  mirror['high']=1000-self.r['low'];mirror['low']=1000-self.r['high'];mirror['previous_high']=1000-self.r['previous_low'];mirror['previous_low']=1000-self.r['previous_high']
  mirror['rsi']=100-self.r['rsi'];mirror['previous_rsi']=100-self.r['previous_rsi']
  a,b=gates(self.r,1),gates(mirror,-1)
  for key in ('prep','fast','hold','distance_atr','body_atr','wick','volume_rate_proxy','extension_since_open'):self.assertAlmostEqual(a[key],b[key])
 def test_gap_not_chase(self):
  a=gates(self.r,1);b=gates({**self.r,'previous_session_close':400,'previous_high':401,'previous_low':399},1)
  self.assertEqual(a['fast'],b['fast']);self.assertEqual(a['distance_atr'],b['distance_atr']);self.assertNotEqual(a['gap_from_prev_close'],b['gap_from_prev_close'])
 def test_intraday_uses_previous_bar_trigger(self):
  r=self.row(bar_start='2026-09-25T13:35:00+00:00',previous_high=500.3)
  self.assertAlmostEqual(gates(r,1)['distance_atr'],.3)
 def test_after_send_observed_failure_cancels(self):
  s,_=self.advance({});s,_=self.advance(s,15);s,e=self.advance(s,30,rsi=49,price=500.1);self.assertEqual(e,'CANCEL');self.assertEqual(s['phase'],'cancelled')
 def test_close_without_prep_cannot_enter(self):
  _,e=self.advance({},241,'closed',chart_mc=True);self.assertIsNone(e)
 def test_confirm_then_duplicate_not_entry(self):
  s,_=self.advance({});s,_=self.advance(s,15);s,e=self.advance(s,241,'closed',chart_mc=True);self.assertEqual(e,'CONFIRM')
  _,e=self.advance(s,255,'closed',chart_mc=True);self.assertIsNone(e)
 def test_worker_expires_missing_close_without_new_tick(self):
  self.batch([dict(self.row(),kind='pulse')]);self.batch([dict(self.row(15),kind='pulse')],self.t+timedelta(seconds=15))
  result=sweep(self.db,self.t+timedelta(seconds=301));self.assertEqual(len(result),1);self.assertEqual(result[0]['state']['phase'],'expired');self.assertEqual(result[0]['state']['reason'],'close_missing')
 def test_late_x_cannot_attach_expired_candidate(self):
  s,_=self.advance({});s,_=self.advance(s,15);s,_=self.advance(s,301)
  _,e=self.advance(s,360,bar_start='2026-09-25T13:35:00+00:00',bar_end='2026-09-25T13:40:00+00:00',chart_exit='X',chart_exit_side=1,price=500.1,rsi=49)
  self.assertNotEqual(e,'CHART_X')
 def test_per_ticker_close_retry_and_idempotence(self):
  a=dict(self.row(),kind='pulse');b={**a,'ticker':'AAPL','feed':'BATS:AAPL'};self.batch([a,b])
  self.batch([{**a,**self.row(15)},{**b,**self.row(15),'ticker':'AAPL','feed':'BATS:AAPL'}],self.t+timedelta(seconds=15))
  close={**self.row(241),'kind':'closed','chart_mc':True}
  self.batch([close],self.t+timedelta(seconds=241))
  self.batch([{**self.row(256),'kind':'closed','chart_mc':True,'ticker':'AAPL','feed':'BATS:AAPL'}],self.t+timedelta(seconds=256))
  with self.db.transaction() as tx:
   self.assertEqual(tx.get('mf133-state:2026-09-25:MSFT')['phase'],'confirmed');self.assertEqual(tx.get('mf133-state:2026-09-25:AAPL')['phase'],'confirmed')
 def test_invalid_row_does_not_discard_valid_peer(self):
  result=self.batch([{'ticker':'SPY'},dict(self.row(),kind='pulse')]);self.assertEqual(result['decisions'][0]['event'],'REJECTED_ROW');self.assertEqual(result['decisions'][1]['event'],'PREP')
 def test_shadow_never_queues_telegram(self):
  self.batch([dict(self.row(),kind='pulse')]);self.batch([dict(self.row(15),kind='pulse')],self.t+timedelta(seconds=15))
  with self.db.transaction() as tx:self.assertEqual(tx.execute('SELECT COUNT(*) FROM mf_outbox').fetchone()[0],0)
 def test_pine_original_engine_unchanged(self):
  root=Path(__file__).parents[1]/'pine'
  a=(root/'Market_Forge_V13_32_Early_20_TEST.pine').read_text();b=(root/'Market_Forge_V13_33_Fast_Path_SHADOW.pine').read_text()
  self.assertEqual(a[a.index('f_engine() =>'):a.index('    p = Snapshot.new(')],b[b.index('f_engine() =>'):b.index('    probeNewSession =')])
