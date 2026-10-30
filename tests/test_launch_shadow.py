import unittest,json,tempfile,os,copy
from pathlib import Path
from datetime import datetime,timedelta,timezone
from unittest.mock import patch
from scanner.v136_live.engine import Engine,Bar,ET
from scanner.v136_live.launch_shadow import compare,FastPreview,capture,deliver_and_record,SYMBOLS
from scanner.store import Store
START=datetime(2026,10,29,9,30,tzinfo=ET)
FIXTURE=Path(__file__).resolve().parent/'fixtures/launch-2026-10-29.json'

def bar(m):
 op=(100,103,103.4)[min(m-1,2)];cl=(103,103.4,103.8)[min(m-1,2)]
 return Bar('SPY',START+timedelta(minutes=m),op,cl+.1,op-.1,cl,10000)
def seed():return [Bar('SPY',START-timedelta(days=1)+timedelta(minutes=m),100,101,99,100,1000) for m in range(1,391)]
class ShadowTests(unittest.TestCase):
 def test_fast_can_enter_at_two_without_three(self):
  e=FastPreview();e.seed(seed());e.market=e
  with patch('scanner.v136_live.launch_shadow.context',return_value=(True,'pass',{})):
   self.assertEqual(e.on_bar(bar(1)),[])
   events=e.on_bar(bar(2));self.assertEqual([(v.action,v.timeframe) for v in events],[('MC',2)])
 def test_context_veto_not_bypassed(self):
  e=FastPreview();e.seed(seed());e.market=e
  with patch('scanner.v136_live.launch_shadow.context',return_value=(False,'5m_chop',{})):
   e.on_bar(bar(1));self.assertEqual(e.on_bar(bar(2)),[])
  self.assertEqual(e.funnel['5m_chop'],1)
 def test_same_time_no_ctn_or_duplicate_entry(self):
  e=FastPreview();e.seed(seed());e.market=e
  with patch('scanner.v136_live.launch_shadow.context',return_value=(True,'pass',{})):
   out=[v for m in (1,2,3) for v in e.on_bar(bar(m))]
  self.assertEqual(len([v for v in out if v.action=='MC']),1)
 def test_real_replay_baseline_equal_to_production(self):
  x=json.loads(FIXTURE.read_text())
  result=compare(x)
  actual={(v['ticker'],v['time'],v['action'],v['price']) for v in x['events'] if v.get('engine')=='MF_V13_39_DATA_RECOVERY' and v['ticker'] in SYMBOLS}
  replay={(v['ticker'],v['time'],v['action'],v['price']) for v in result['events'] if v['path']=='baseline'}
  self.assertEqual(actual,replay)
 def test_future_bars_cannot_change_entry_prefix(self):
  x=json.loads(FIXTURE.read_text());a=compare(x)
  cutoff=int(datetime(2026,10,29,18,0,tzinfo=timezone.utc).timestamp()*1000)
  y=copy.deepcopy(x)
  for s in y['symbols'].values():s['bars']=[r for r in s['bars'] if r[1]<=cutoff]
  b=compare(y)
  fields=('signal_id','condition_met_at','signal_price','candidate_detected_at','atr','chase_atr')
  before=[tuple(v[k] for k in fields) for v in a['entries'] if datetime.fromisoformat(v['condition_met_at']).timestamp()*1000<=cutoff]
  after=[tuple(v[k] for k in fields) for v in b['entries']]
  self.assertEqual(before,after)
 def test_live_capture_cannot_enqueue_or_mutate_operating_state(self):
  fd,path=tempfile.mkstemp();os.close(fd)
  try:
   store=Store(path);x=json.loads(FIXTURE.read_text())
   with store.transaction() as tx:tx.put('research136live:control',{'enabled':True});tx.put('research139recovery:test',{'sent_entry':1})
   with patch('scanner.v136_live.runtime.diagnostic_export',return_value=x):
    self.assertTrue(capture(store,datetime(2026,10,29,20,1,tzinfo=timezone.utc))['ok'])
   with store.transaction() as tx:
    self.assertEqual(tx.get('research136live:control'),{'enabled':True});self.assertEqual(tx.get('research139recovery:test'),{'sent_entry':1})
    self.assertEqual(tx.execute('SELECT COUNT(*) FROM mf_outbox').fetchone()[0],0)
  finally:os.unlink(path)
 def test_failed_diagnostics_never_sends(self):
  fd,path=tempfile.mkstemp();os.close(fd)
  try:
   with patch('scanner.v136_live.runtime.diagnostic_export',side_effect=ValueError('private')):
    self.assertFalse(capture(Store(path),START)['ok'])
  finally:os.unlink(path)
 def test_delivery_wrapper_calls_original_once(self):
  fd,path=tempfile.mkstemp();os.close(fd)
  try:
   with patch('scanner.v136_live.runtime.delivery',return_value=[]) as original:
    self.assertEqual(deliver_and_record(Store(path),None,START),[]);original.assert_called_once()
  finally:os.unlink(path)
if __name__=='__main__':unittest.main()
