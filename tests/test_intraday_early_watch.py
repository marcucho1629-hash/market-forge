"""Regression: early 5-minute IEX watch and independent intraday LOW alerts.

All fixtures use CLOSED IEX bars. These are WATCH notifications, not MC/SCMP.
"""
import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from unittest.mock import patch

from scanner.store import Store
from scanner import intraday_v3 as v
from scanner import intraday_early_watch as e

OPEN=datetime(2026,11,10,14,30,tzinfo=timezone.utc)  # 07:30 Mountain
DAY="2026-11-10"
SESSION=dict(date=DAY,open="09:30",close="16:00")
SEED_TIME=OPEN+timedelta(minutes=17)

def early_bars(symbol="NVDA",side=1,count=3):
    b=[]
    for i in range(count):
        price=100+side*(.10+.34*i)
        op=price-side*.10
        low=min(op,price)-.30
        high=max(op,price)+.30
        b.append(dict(t=(OPEN+timedelta(minutes=i*5)).isoformat(),
                      o=op,h=high,l=low,c=price,v=30000,n=350,
                      vw=(price+op)/2))
    return b

def spy_bars(count=3):
    out=[]
    for i in range(count):
        p=700+i*.035
        out.append(dict(t=(OPEN+timedelta(minutes=i*5)).isoformat(),
                        o=p-.015,h=p+.25,l=p-.25,c=p,v=30000,n=500,vw=p))
    return out

def candidate(symbol,side="BULLISH",chase="LOW",score=70):
    return dict(ticker=symbol,direction=side,eligible=True,chase=chase,
                score=score,price=100.3,choppy=False,late_chase=False)

class Opening(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store=Store(tmp.name+"/early.db")
        self.sent=[]
    def test_first_three_closed_bars_can_produce_provisional_watch(self):
        raw=early_bars()
        result=e.opening_candidate("NVDA",raw,OPEN,OPEN+timedelta(minutes=15),spy_change=.01)
        self.assertTrue(result["eligible"],result)
        self.assertEqual(result["chase"],"LOW")
        self.assertEqual(result["direction"],"BULLISH")
        self.assertIn("cutoff",result)
        self.assertNotIn("entry",result)
    def test_future_bars_and_chop_cannot_be_injected(self):
        raw=early_bars()
        result=e.opening_candidate("NVDA",raw+[dict(raw[-1],t=(OPEN+timedelta(minutes=20)).isoformat())],
                                   OPEN,OPEN+timedelta(minutes=15),spy_change=.01)
        baseline=e.opening_candidate("NVDA",raw,OPEN,OPEN+timedelta(minutes=15),spy_change=.01)
        self.assertEqual(result,baseline)
        choppy=early_bars()
        choppy[1]["c"]=choppy[0]["c"]-.2
        choppy[1]["o"]=choppy[1]["c"]-.1
        choppy[1]["l"]=min(choppy[1]["o"],choppy[1]["c"])-.3
        choppy[1]["h"]=max(choppy[1]["o"],choppy[1]["c"])+.3
        result=e.opening_candidate("NVDA",choppy,OPEN,OPEN+timedelta(minutes=15),spy_change=.01)
        self.assertFalse(result["eligible"])
        self.assertEqual(result["reason"],"mixed_first_wave")
    def test_opening_before_8am_sends_once_without_top10_state(self):
        data=dict(SPY=spy_bars(),NVDA=early_bars(),HIMS=early_bars(side=-1))
        with patch.object(v,"calendar",return_value=(SESSION,None)),patch.object(
            v,"pre_config",return_value={"symbols":["SPY","NVDA","HIMS"]}),patch.object(
            v,"gather",return_value=data):
            first=v.worker(self.store,self.sent.append,SEED_TIME)
            repeat=v.worker(self.store,self.sent.append,SEED_TIME)
        self.assertEqual(first["status"],"early_open_scanned")
        self.assertEqual(first["new"],2,first)
        self.assertEqual(first["delivery"][0]["status"],"sent")
        self.assertEqual(repeat["status"],"early_open_already_scanned")
        self.assertEqual(len(self.sent),1)
        self.assertIn("EARLY WATCH",self.sent[0])
        self.assertIn("진입 신호 아님",self.sent[0])
        with self.store.transaction() as tx:
            self.assertIsNone(tx.get("intraday-v3:"+DAY+":latest"))
            self.assertIsNone(tx.get("intraday-v3:"+DAY+":hour:7"))
            self.assertIsNotNone(tx.get("intraday-v3:"+DAY+":early:open:latest"))
    def test_no_spy_means_no_false_message(self):
        with patch.object(v,"calendar",return_value=(SESSION,None)),patch.object(
             v,"pre_config",return_value={"symbols":["SPY","NVDA"]}),patch.object(
             v,"gather",return_value={"SPY":[],"NVDA":early_bars()}):
            r=v.worker(self.store,self.sent.append,SEED_TIME)
        self.assertEqual(r["status"],"early_open_scanned")
        self.assertEqual(r["new"],0)
        self.assertEqual(len(self.sent),0)
    def test_opening_is_not_an_entry_gate_side_effect(self):
        with patch.object(v,"calendar",return_value=(SESSION,None)):
            r=v.worker(self.store,self.sent.append,OPEN+timedelta(minutes=12))
        self.assertEqual(r["status"],"outside_intraday_window")
        self.assertFalse(self.sent)

class NewLow(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store=Store(tmp.name+"/low.db")
        self.prefix="intraday-v3:"+DAY+":"
    def test_new_directional_low_once_per_day(self):
        with self.store.transaction() as tx:
            first=e.live_low_alert(tx,self.prefix,[candidate("NVDA")],1,SEED_TIME,
                                   OPEN+timedelta(minutes=40),True)
            self.assertFalse(first["queued"])
        with self.store.transaction() as tx:
            second=e.live_low_alert(tx,self.prefix,[candidate("NVDA"),candidate("HIMS","BEARISH")],
                                    2,SEED_TIME+timedelta(minutes=5),OPEN+timedelta(minutes=45),False)
        self.assertEqual(second["new"],1)
        with self.store.transaction() as tx:
            third=e.live_low_alert(tx,self.prefix,[candidate("NVDA"),candidate("HIMS","BEARISH")],
                                    3,SEED_TIME+timedelta(minutes=10),OPEN+timedelta(minutes=50),False)
            report=tx.get(self.prefix+"early:live:history")
            row=tx.execute("SELECT status FROM mf_outbox WHERE id LIKE ?",
                           (self.prefix+"early:live:msg:%",)).fetchall()
        self.assertEqual(third["new"],0)
        self.assertEqual(report[-2]["candidates"][0]["ticker"],"HIMS")
        self.assertEqual(len(row),1)
    def test_high_med_choppy_and_score_under60_cannot_alert(self):
        with self.store.transaction() as tx:
            e.live_low_alert(tx,self.prefix,[],1,SEED_TIME,OPEN+timedelta(minutes=40),True)
            values=[candidate("NVDA",chase="HIGH"),candidate("AAPL",chase="MED"),
                    candidate("TSLA",score=57),dict(candidate("HIMS"),choppy=True),
                    dict(candidate("AMD"),late_chase=True)]
            r=e.live_low_alert(tx,self.prefix,values,2,
                              SEED_TIME+timedelta(minutes=5),OPEN+timedelta(minutes=45),False)
        self.assertFalse(r["queued"])
    def test_hourly_alert_independent_of_experimental_failure(self):
        next_time=OPEN+timedelta(minutes=42)
        raw=[]
        for i in range(8):
            p=100+i*.07
            op=p-.015
            raw.append(dict(t=(OPEN+timedelta(minutes=5*i)).isoformat(),
                            o=op,h=p+.06,l=op-.06,c=p,v=25000+i*1500,
                            vw=(op+p)/2,n=80))
        spy=[dict(x) for x in raw]
        sent=[]
        with patch.object(v,"calendar",return_value=(SESSION,None)),patch.object(
             v,"pre_config",return_value={"symbols":["SPY","NVDA"]}),patch.object(
             v,"gather",return_value={"SPY":spy,"NVDA":raw}),patch.object(
             v,"live_low_alert",side_effect=RuntimeError("early path failed")):
            res=v.worker(self.store,sent.append,next_time)
        self.assertEqual(res["status"],"scanned")
        self.assertEqual(res["delivery"][0]["status"],"sent")
        self.assertTrue(res["telegram_queued"])
        self.assertEqual(len(sent),1)
        self.assertEqual(res["early_error"],"early_alert_failure")

if __name__=="__main__":unittest.main()
