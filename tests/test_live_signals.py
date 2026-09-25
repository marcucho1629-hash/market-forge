import unittest,tempfile,copy
from unittest.mock import patch
from datetime import datetime,timezone,timedelta
from scanner.store import Store
from scanner.live_signals import ingest,delivery,normalize,level,message,VERSION
from scanner.live_runtime import worker
from scanner.calendar import schedule
from scanner.news import import_documents,review_document

class LiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=Store(self.tmp.name+'/db')
        self.now=datetime(2026,9,24,14,0,30,tzinfo=timezone.utc)
        self.r=dict(ticker='META',observed_at=self.now.isoformat(),bar_start='2026-09-24T14:00:00+00:00',bar_end='2026-09-24T14:05:00+00:00',benchmark_start='2026-09-24T14:00:00+00:00',price=101,open=100,high=101,low=100,atr=1,ema9=100.5,ema21=100,vwap=100.1,previous_ema9=100.3,previous_high=100.8,previous_low=99.8,rsi=70,relative_volume=3,symbol_return_pct=1,benchmark_return_pct=.1,choppy=False,wide_whipsaw=False,bull_breakout=True,bear_breakout=False,bull_pullback=False,bear_pullback=False,engine_action='',engine_exit='',engine_exit_side=0,continuation='')
        with self.db.transaction() as tx:tx.put('mf130-control',{'enabled':True,'version':VERSION})
    def tearDown(self):self.tmp.cleanup()
    def payload(self,kind='pulse',**changes):return dict(source=VERSION,schema_version=2,bank=1,kind=kind,observations=[{**self.r,**changes}])
    def runrow(self,kind='pulse',**changes):return ingest(self.db,self.payload(kind,**changes),self.now)
    def state(self):
        with self.db.transaction() as tx:return tx.get('mf130-state:2026-09-24:META',{})
    def advance(self,seconds=15):
        self.now+=timedelta(seconds=seconds);self.r['observed_at']=self.now.isoformat()
    def news(self,identity='fresh',age=0,score=80):
        pub=(self.now-timedelta(seconds=age)).isoformat()
        import_documents(self.db,[dict(id=identity,url='https://example.com/test',title='Synthetic',timestamp=pub,text='Synthetic body')],self.now)
        review_document(self.db,dict(document_id=identity,ticker='META',category='catalyst',direction='LONG',score=score,event_id=identity,rationale='test',supporting_excerpt='Synthetic body',reviewer='test'),self.now)
    def closed(self):
        self.r.update(bar_start='2026-09-24T13:55:00+00:00',bar_end='2026-09-24T14:00:00+00:00',benchmark_start='2026-09-24T13:55:00+00:00',engine_action='MC')
    def test_intrabar_independent_and_actual_delivery(self):
        d=self.runrow()['decisions'][0];self.assertEqual(d['code'],'MC');self.assertEqual(d['rank'],0)
        self.assertFalse(self.state().get('active'));sent=[];delivery(self.db,sent.append,self.now)
        self.assertIn('미확정',sent[0]);self.assertEqual(len(sent[0].splitlines()),3)
        self.advance();self.runrow();self.assertTrue(self.state()['active'])
    def test_duplicate_no_second_send(self):
        p=self.payload();ingest(self.db,p,self.now);self.assertEqual(ingest(self.db,p,self.now)['status'],'duplicate')
        sent=[];delivery(self.db,sent.append,self.now);delivery(self.db,sent.append,self.now);self.assertEqual(len(sent),1)
    def test_exit_only_sent_original_direction(self):
        self.runrow();sent=[];delivery(self.db,sent.append,self.now);self.advance()
        d=self.runrow(price=99.8,low=99.5,rsi=48)['decisions'][0]
        self.assertEqual(d['code'],'X');delivery(self.db,sent.append,self.now)
        self.assertIn('상승 후보 종료',sent[1]);self.assertIn('실시간 감지',sent[1])
    def test_no_phantom_exit(self):
        self.assertIsNone(self.runrow(price=99.8,low=99.5,rsi=48,engine_exit='X',engine_exit_side=1)['decisions'][0]['code'])
    def test_uncertain_send_never_activates_or_retries(self):
        self.runrow()
        def fail(_):raise RuntimeError()
        delivery(self.db,fail,self.now);self.advance();self.runrow(choppy=True)
        self.assertFalse(self.state().get('active'));self.assertEqual(delivery(self.db,lambda _:self.fail(),self.now),[])
    def test_hard_filters(self):
        for change in [dict(choppy=True),dict(wide_whipsaw=True),dict(ema9=98),dict(relative_volume=1),dict(rsi=60),dict(bull_breakout=False)]:
            with self.subTest(change=change):self.assertIsNone(self.runrow(**change)['decisions'][0]['code'])
    def test_news_required_for_colored_grade(self):
        self.closed();self.assertIsNone(self.runrow('closed')['decisions'][0]['code'])
        self.news();self.advance(1);d=self.runrow('closed')['decisions'][0]
        self.assertEqual(d['code'],'MC');self.assertEqual(d['rank'],3)
    def test_older_document_does_not_destroy_newer(self):
        self.news('new',age=0,score=80);self.news('old',age=7200,score=100)
        self.closed();d=self.runrow('closed')['decisions'][0];self.assertEqual(d['bigdata_score'],16)
    def test_thresholds(self):
        self.assertEqual([level(v) for v in (None,69.99,70,74.99,75,79.99,80)],[0,0,1,1,2,2,3])
    def test_reject_invalid_clock_benchmark(self):
        for change in [dict(observed_at=(self.now-timedelta(seconds=16)).isoformat()),dict(benchmark_start='2026-09-24T13:55:00Z'),dict(choppy='false'),dict(price=True)]:
            with self.subTest(change=change),self.assertRaises(ValueError):self.runrow(**change)
        with self.assertRaises(ValueError):self.runrow('closed')
    def test_disabled_and_holiday(self):
        with self.db.transaction() as tx:tx.put('mf130-control',{'enabled':False,'version':VERSION})
        self.assertIsNone(self.runrow()['decisions'][0]['code'])
        self.assertFalse(schedule('2026-12-25')['is_open'])
        self.assertEqual(schedule('2026-11-27')['close'],'2026-11-27T18:00:00+00:00')
        self.assertEqual(schedule('2026-11-02')['open'],'2026-11-02T14:30:00+00:00')
    @patch('scanner.live_runtime.review_latest',return_value={})
    @patch('scanner.live_runtime.refresh',return_value={'documents':[]})
    def test_preopen_news_and_one_call_per_minute(self,refresh,review):
        before=datetime(2026,9,24,12,tzinfo=timezone.utc)
        a=worker(self.db,lambda _:self.fail(),before);b=worker(self.db,lambda _:self.fail(),before)
        self.assertIsNotNone(a['ticker']);self.assertIsNone(b['ticker']);self.assertEqual(refresh.call_count,1)
    def test_close_message_last_observation_not_current_price(self):
        self.runrow();delivery(self.db,lambda _:None,self.now);self.advance();self.runrow()
        sent=[];worker(self.db,sent.append,datetime(2026,9,24,20,1,tzinfo=timezone.utc))
        self.assertEqual(len(sent),1);self.assertIn('마지막 관측',sent[0]);self.assertIn('후보 추적 종료',sent[0])
