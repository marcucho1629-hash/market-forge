import tempfile
import unittest
from datetime import datetime,timezone,timedelta
from scanner.store import Store,drain
from scanner.chart_news import shadow_batch,UNIVERSE
from scanner.news import import_documents,review_document

class ChartNewsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=Store(self.tmp.name+'/db')
        self.now=datetime(2026,9,23,14,tzinfo=timezone.utc)
        with self.db.transaction() as tx:
            tx.put('tv-session:2026-09-23',dict(is_open=True,open='2026-09-23T13:30:00Z',close='2026-09-23T20:00:00Z'))
        self.row=dict(ticker='META',direction='LONG',observed_at=self.now.isoformat(),sampling='closed_5m',
                      price=100,ema9=99,ema21=98,vwap=99,ema9_slope_atr=.1,relative_return_pct=.2,
                      relative_volume=3,directional_momentum_atr=1,rsi=70,extension_atr=1,
                      choppy=False,wide_whipsaw=False,breakout_valid=True,pullback_valid=False)
    def tearDown(self): self.tmp.cleanup()
    def news(self,ticker='META',score=80,category='news',age=0,direction='LONG'):
        pub=(self.now-timedelta(seconds=age)).isoformat()
        identity=ticker+category
        import_documents(self.db,[dict(id=identity,url='https://example.com/test',title='Synthetic test',timestamp=pub,text='Synthetic body for testing only.')],self.now)
        review_document(self.db,dict(document_id=identity,ticker=ticker,category=category,direction=direction,score=score,event_id='same-event',rationale='Synthetic only',supporting_excerpt='Synthetic body',reviewer='unit-test'),self.now)
    def run_rows(self,rows=None): return shadow_batch(self.db,dict(observations=rows or [self.row]),self.now)
    def test_math_and_no_delivery(self):
        self.news()
        d=self.run_rows()['decisions'][0]
        self.assertEqual((d['chart_score'],d['bigdata_score'],d['total_score']),(75,16,91))
        self.assertTrue(d['entry_selected'])
        self.assertEqual(drain(self.db,lambda _:self.fail('must not send')),[])
    def test_missing_and_forged_news_never_fill_score(self):
        self.row['news']={'score':100,'source':'Bigdata.com'}
        d=self.run_rows()['decisions'][0]
        self.assertIsNone(d['total_score']);self.assertFalse(d['entry_selected'])
        self.assertTrue(d['information_selected'])
    def test_same_event_not_added_twice(self):
        self.news(score=60);self.news(score=100,category='catalyst')
        self.assertEqual(self.run_rows()['decisions'][0]['bigdata_score'],20)
    def test_news_direction_mismatch(self):
        self.news(direction='SHORT')
        self.assertIsNone(self.run_rows()['decisions'][0]['total_score'])
    def test_news_decay(self):
        self.news(score=100,category='catalyst',age=7200)
        self.assertEqual(self.run_rows()['decisions'][0]['bigdata_score'],10)
    def test_known_after_observation(self):
        self.news();self.row['observed_at']=(self.now-timedelta(seconds=1)).isoformat()
        self.assertIsNone(self.run_rows()['decisions'][0]['bigdata_score'])
    def test_hard_blocks_even_with_news(self):
        self.news(score=100)
        for name,value in [('choppy',True),('wide_whipsaw',True),('extension_atr',2)]:
            with self.subTest(name=name):
                d=self.run_rows([{**self.row,name:value}])['decisions'][0]
                self.assertFalse(d['entry_selected']);self.assertFalse(d['information_selected'])
    def test_threshold_exact_and_below(self):
        self.news(score=25)
        self.assertTrue(self.run_rows()['decisions'][0]['entry_selected'])
        self.row['ticker']='QQQ';self.news('QQQ',score=24.99)
        d=self.run_rows()['decisions'][0]
        self.assertFalse(d['entry_selected']);self.assertIn('total_below_80',d['blocks'])
    def test_chart_floor_and_closed_calendar(self):
        self.news(score=100)
        self.row.update(breakout_valid=False)
        d=self.run_rows()['decisions'][0]
        self.assertEqual(d['chart_score'],55)
        self.assertIn('chart_below_60',d['blocks'])
        self.now+=timedelta(seconds=20)
        self.row.update(observed_at=self.now.isoformat(),breakout_valid=True)
        with self.db.transaction() as tx:tx.put('tv-session:2026-09-23',{'is_open':False})
        d=self.run_rows()['decisions'][0]
        self.assertFalse(d['entry_selected']);self.assertFalse(d['information_selected'])
    def test_all_70_and_shared_budget(self):
        self.assertEqual(len(set(UNIVERSE)),70)
        for t in UNIVERSE:self.news(t)
        result=self.run_rows([{**self.row,'ticker':t} for t in UNIVERSE])
        self.assertEqual(len(result['decisions']),70)
        self.assertEqual(sum(d['entry_selected'] for d in result['decisions']),3)
        self.assertEqual(result['telegram_sent'],0)
    def test_short_symmetry(self):
        self.news(direction='SHORT')
        self.row.update(direction='SHORT',ema9=101,ema21=102,vwap=101,ema9_slope_atr=-.1,relative_return_pct=-.2,rsi=30)
        self.assertEqual(self.run_rows()['decisions'][0]['total_score'],91)
    def test_repeat_is_idempotent(self):
        self.news();self.run_rows()
        self.assertTrue(self.run_rows()['duplicate'])
    def test_bad_input_and_stale(self):
        for change in [dict(ticker='UNKNOWN'),dict(rsi=True),dict(relative_volume=float('nan')),dict(observed_at='2026-09-22T14:00:00Z')]:
            with self.subTest(change=change),self.assertRaises(ValueError):self.run_rows([{**self.row,**change}])
