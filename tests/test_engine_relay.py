import unittest,tempfile
from datetime import datetime,timezone,timedelta
from scanner.store import Store
from scanner.engine_relay import ingest,delivery,VERSION

class RelayTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=Store(self.tmp.name+'/db')
        self.now=datetime(2026,9,25,13,31,10,tzinfo=timezone.utc)
        self.row=dict(ticker='MSFT',feed='BATS:MSFT',observed_at=self.now.isoformat(),bar_start='2026-09-25T13:30:00+00:00',bar_end='2026-09-25T13:35:00+00:00',benchmark_start='2026-09-25T13:30:00+00:00',price=500,open=498,high=501,low=497,atr=1,ema9=497,ema21=496,vwap=497,previous_ema9=496,previous_high=499,previous_low=497,rsi=70,relative_volume=3,symbol_return_pct=1,benchmark_return_pct=.1,choppy=True,wide_whipsaw=True,bull_breakout=True,bear_breakout=False,bull_pullback=False,bear_pullback=False,engine_action='MC',engine_exit='',engine_exit_side=0,continuation='',chart_mc=True,chart_scmp=False,chart_ctn_up=False,chart_ctn_down=False,chart_exit='',chart_exit_side=0)
    def tearDown(self):self.tmp.cleanup()
    def enable(self):
        with self.db.transaction() as tx:tx.put('mf131-control',{'enabled':True,'version':VERSION})
    def runrow(self,kind='pulse',**changes):return ingest(self.db,dict(source=VERSION,schema_version=3,bank=1,kind=kind,observations=[{**self.row,**changes}]),self.now)
    def send(self,fn):return delivery(self.db,fn,self.now,clock=lambda:self.now)
    def test_default_shadow(self):
        self.runrow();sent=[];self.send(sent.append);self.assertEqual(sent,[])
    def test_chart_flag_bypasses_independent_chasing_news_score(self):
        self.enable();d=self.runrow();sent=[];self.send(sent.append)
        self.assertEqual(len(sent),1);self.assertIn('MC',sent[0]);self.assertIn('미확정',sent[0]);self.assertEqual(d['decisions'][0]['events'][0]['result'],'queued')
    def test_no_chart_no_entry_even_with_momentum(self):
        self.enable();self.runrow(chart_mc=False);sent=[];self.send(sent.append);self.assertEqual(sent,[])
    def test_no_remapping_removed_codes(self):
        self.enable();self.runrow(chart_mc=False,engine_action='C');sent=[];self.send(sent.append);self.assertEqual(sent,[])
    def test_pulse_and_close_one_event(self):
        self.enable();self.runrow();sent=[];self.send(sent.append)
        self.now+=timedelta(seconds=15);self.runrow(observed_at=self.now.isoformat())
        self.now=datetime(2026,9,25,13,35,1,tzinfo=timezone.utc);self.runrow('closed',observed_at=self.now.isoformat());self.send(sent.append);self.assertEqual(len(sent),1)
    def test_retracted_candidate_not_fake_exit(self):
        self.enable();self.runrow();sent=[];self.send(sent.append)
        self.now=datetime(2026,9,25,13,35,1,tzinfo=timezone.utc)
        d=self.runrow('closed',observed_at=self.now.isoformat(),chart_mc=False);self.send(sent.append)
        self.assertEqual(len(sent),1);self.assertEqual(d['decisions'][0]['events'][0]['result'],'not_present_at_close')
    def test_chart_exit_is_identified(self):
        self.enable();self.runrow(chart_mc=False,chart_exit='SX',chart_exit_side=1);sent=[];self.send(sent.append)
        self.assertIn('MSFT X',sent[0]);self.assertIn('차트 엔진 SX',sent[0])
    def test_expiry_no_late_send(self):
        self.enable();self.runrow();self.now+=timedelta(seconds=31);sent=[];results=self.send(sent.append)
        self.assertEqual(sent,[]);self.assertEqual(results[0]['status'],'expired')
    def test_uncertain_never_retry_and_actual_timestamps(self):
        self.enable();self.runrow()
        def fail(_):raise TimeoutError()
        results=self.send(fail);self.assertEqual(results[0]['status'],'uncertain_or_failed');self.assertIn('send_completed_at',results[0]);self.assertEqual(self.send(fail),[])
    def test_stale_rejected(self):
        self.now+=timedelta(seconds=16)
        with self.assertRaises(ValueError):self.runrow()
    def test_wrong_feed_rejected(self):
        with self.assertRaises(ValueError):self.runrow(feed='BATS:META')
    def test_new_bar_new_chart_event_not_20min_cooldown(self):
        self.enable();self.runrow();sent=[];self.send(sent.append)
        self.now=datetime(2026,9,25,13,35,15,tzinfo=timezone.utc)
        self.runrow(observed_at=self.now.isoformat(),bar_start='2026-09-25T13:35:00+00:00',bar_end='2026-09-25T13:40:00+00:00',benchmark_start='2026-09-25T13:35:00+00:00');self.send(sent.append)
        self.assertEqual(len(sent),2)
