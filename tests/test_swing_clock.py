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
 def test_future_inner_clock_event_quarantined(self):
  self.data['events'][0]['emitted_ms']=self.data['emitted_ms']+11000
  result=ingest_bank(self.store,self.data,self.now)
  self.assertEqual((result['status'],result['queued'],result['rejected_events']),('partial_accepted',0,1))
  report=diagnostics(self.store,self.now)
  self.assertEqual(report['overview']['individual_event_rejects_today'],1)
  self.assertEqual(report['overview']['banks_reporting_today'],1)
  self.assertEqual(report['trade_events'],[])
 def test_cross_candle_event_quarantined(self):
  self.data['events'][0]['bar_end_ms']-=3600000
  self.assertEqual(ingest_bank(self.store,self.data,self.now)['rejected_events'],1)
 def test_one_bad_event_cannot_block_valid_xlk_plan(self):
  import copy
  malformed=copy.deepcopy(self.data['events'][0])
  malformed['ticker']='XLF'
  malformed['emitted_ms']=self.data['emitted_ms']+11000
  self.data['events'].append(malformed)
  result=ingest_bank(self.store,self.data,self.now)
  self.assertEqual((result['queued'],result['rejected_events']),(1,1))
  report=diagnostics(self.store,self.now)
  self.assertEqual(len(report['trade_events']),1)
  self.assertEqual(report['overview']['received_candidate_events_today'],2)
  self.assertEqual(report['overview']['server_queued_today'],1)
  self.assertEqual(report['overview']['individual_event_rejects_today'],1)
  self.assertEqual(report['overview']['ready_tickers_latest_by_bank']['10'],8)
 def test_missing_bank_counts_do_not_claim_cause(self):
  self.data['events']=[]
  ingest_bank(self.store,self.data,self.now)
  overview=diagnostics(self.store,self.now)['overview']
  self.assertEqual(overview['expected_banks'],13)
  self.assertEqual(overview['banks_missing_today'],[1,2,3,4,5,6,7,8,9,11,12,13])
  self.assertEqual(overview['received_candidate_events_today'],0)

 def test_zero_event_health_not_signal(self):
  self.data['events']=[];ingest_bank(self.store,self.data,self.now)
  report=diagnostics(self.store,self.now)
  self.assertEqual(len(report['hourly_receipts']),1);self.assertEqual(report['trade_events'],[]);self.assertEqual(report['deliveries'],[])
if __name__=='__main__':unittest.main()
