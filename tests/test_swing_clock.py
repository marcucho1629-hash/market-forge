import json,tempfile,unittest
from pathlib import Path
from datetime import datetime,timezone
from scanner.store import Store
from scanner.swing_compare import ingest_bank,diagnostics
class SwingClock(unittest.TestCase):
 def setUp(self):
  self.f=tempfile.NamedTemporaryFile();self.store=Store(self.f.name)
  self.data=json.loads((Path(__file__).parent/'fixtures/swing-422-2026-10-29.json').read_text())
  self.now=datetime.fromtimestamp((self.data['emitted_ms']+2000)/1000,timezone.utc)
 def tearDown(self):self.f.close()
 def test_real_failed_xlk_plan_accepted_once_without_clock_equality(self):
  self.assertNotEqual(self.data['emitted_ms'],self.data['events'][0]['emitted_ms'])
  self.assertEqual(ingest_bank(self.store,self.data,self.now)['queued'],1)
  self.assertEqual(ingest_bank(self.store,self.data,self.now)['queued'],0)
  report=diagnostics(self.store,self.now);self.assertEqual(len(report['trade_events']),1)
  row=report['trade_events'][0]
  self.assertEqual(row['source_computed_ms'],self.data['events'][0]['emitted_ms'])
  self.assertEqual(row['emitted_ms'],self.data['emitted_ms'])
  self.assertEqual(row['action'],'PLAN');self.assertEqual(row['price'],188.67)
 def test_stale_outer_envelope_still_rejected(self):
  from datetime import timedelta
  with self.assertRaises(ValueError):ingest_bank(self.store,self.data,self.now+timedelta(minutes=5))
 def test_future_inner_clock_rejected(self):
  self.data['events'][0]['emitted_ms']=self.data['emitted_ms']+11000
  with self.assertRaises(ValueError):ingest_bank(self.store,self.data,self.now)
 def test_cross_candle_event_still_rejected(self):
  self.data['events'][0]['bar_end_ms']-=3600000
  with self.assertRaises(ValueError):ingest_bank(self.store,self.data,self.now)
 def test_zero_event_health_not_signal(self):
  self.data['events']=[];ingest_bank(self.store,self.data,self.now)
  report=diagnostics(self.store,self.now)
  self.assertEqual(len(report['hourly_receipts']),1);self.assertEqual(report['trade_events'],[]);self.assertEqual(report['deliveries'],[])
if __name__=='__main__':unittest.main()
