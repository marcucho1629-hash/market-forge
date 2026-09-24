import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from scanner.tradingview import ingest
from scanner.store import Store, drain

class TradingViewTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(self.tmp.name+'/db')
        self.now=datetime(2026,9,23,14,tzinfo=timezone.utc)
        self.row=dict(ticker='META',direction='LONG',observed_at=self.now.isoformat(),
                      event_id='one',confirmed=False,source_whipsaw=False,action='MC',
                      price=100,momentum_score=90,rsi=75,relative_volume=2,
                      directional_momentum_atr=.6,extension_atr=.5,market_condition='ENGINE')
    def tearDown(self): self.tmp.cleanup()
    def calendar(self):
        with self.store.transaction() as tx:
            tx.put('tv-session:2026-09-23',dict(is_open=True,open='2026-09-23T13:30:00Z',close='2026-09-23T20:00:00Z'))
    def test_missing_calendar_blocks(self):
        self.assertFalse(ingest(self.store,self.row,self.now,True)['information_selected'])
    def test_shadow_never_sends_or_claims_entry(self):
        self.calendar()
        result=ingest(self.store,self.row,self.now)
        self.assertTrue(result['information_selected'])
        self.assertFalse(result['entry_selected'])
        self.assertIsNone(result['composite_score'])
        self.assertEqual(drain(self.store,lambda _: self.fail('sent')),[])
    def test_live_retry_and_delivery_isolation(self):
        self.calendar()
        ingest(self.store,self.row,self.now,True)
        self.assertTrue(ingest(self.store,self.row,self.now,True)['duplicate'])
        with self.store.transaction() as tx: tx.enqueue('legacy',{'text':'legacy'})
        sent=[]
        result=drain(self.store,sent.append,prefix='tv-info:',now=self.now)
        self.assertEqual(len(sent),1)
        self.assertIn('종합 80점 진입 신호 아님',sent[0])
        self.assertEqual(result[0]['status'],'sent')
    def test_expired_information_never_sent(self):
        self.calendar()
        ingest(self.store,self.row,self.now,True)
        result=drain(self.store,lambda _:self.fail('expired alert sent'),prefix='tv-info:',now=self.now+timedelta(seconds=61))
        self.assertEqual(result[0]['status'],'expired')
    def test_chop_whipsaw_and_chase_block(self):
        self.calendar()
        for i,(key,value) in enumerate([('market_condition','CHOPPY'),('source_whipsaw',True),('extension_atr',2)]):
            result=ingest(self.store,{**self.row,key:value,'event_id':str(i)},self.now,True)
            self.assertFalse(result['information_selected'])
    def test_forged_old_observation_rejected(self):
        self.row['observed_at']='2026-09-22T14:00:00Z'
        with self.assertRaises(ValueError): ingest(self.store,self.row,self.now)
