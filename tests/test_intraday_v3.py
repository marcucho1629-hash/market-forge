import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from unittest.mock import patch
from scanner.store import Store
from scanner import intraday_v3 as v

OPEN=datetime(2026,11,10,14,30,tzinfo=timezone.utc)
NOW=OPEN+timedelta(minutes=42)
TODAY=dict(date="2026-11-10",open="09:30",close="16:00")

def bars(direction=1,volume=25000,count=8,spike=False):
    out=[]
    for i in range(count):
        price=100+direction*i*.07
        if spike and i==count-1:price+=direction*3.0
        op=price-direction*.015
        out.append(dict(t=(OPEN+timedelta(minutes=i*5)).isoformat(),
                        o=op,h=max(price,op)+.06,l=min(price,op)-.06,
                        c=price,v=volume+i*1500,n=80,vw=(op+price)/2))
    return out

class Core(unittest.TestCase):
    def test_bull_bear_and_no_trading_actions(self):
        bullish=v.evaluate("NVDA",bars(1),OPEN,OPEN+timedelta(minutes=40),spy_move=0)
        bearish=v.evaluate("HIMS",bars(-1),OPEN,OPEN+timedelta(minutes=40),spy_move=0)
        self.assertEqual(bullish["direction"],"BULLISH")
        self.assertEqual(bearish["direction"],"BEARISH")
        self.assertTrue(bullish["eligible"])
        self.assertTrue(bearish["eligible"])
        self.assertGreaterEqual(bullish["score"],v.MIN_SCORE)
        self.assertNotIn("order",bullish)
        self.assertNotIn("signal",bearish)
    def test_future_or_invalid_bars_cannot_turn_into_candidates(self):
        rows=bars()
        future=dict(rows[-1],t=(OPEN+timedelta(minutes=45)).isoformat())
        self.assertEqual(v.evaluate("NVDA",rows+[future],OPEN,OPEN+timedelta(minutes=40))["score"],
                         v.evaluate("NVDA",rows,OPEN,OPEN+timedelta(minutes=40))["score"])
        broken=bars();broken[0]["h"]=broken[0]["l"]-.01
        self.assertEqual(v.evaluate("NVDA",broken,OPEN,OPEN+timedelta(minutes=40))["quality"],"INVALID")
    def test_insufficient_and_stale_not_selected(self):
        self.assertFalse(v.evaluate("NVDA",bars(count=4),OPEN,OPEN+timedelta(minutes=40))["eligible"])
        self.assertFalse(v.evaluate("NVDA",bars(),OPEN,OPEN+timedelta(hours=2))["eligible"])
    def test_spike_chase_block(self):
        r=v.evaluate("NVDA",bars(spike=True),OPEN,OPEN+timedelta(minutes=40),spy_move=0)
        self.assertEqual(r["chase"],"HIGH")
        self.assertFalse(r["eligible"])
        self.assertEqual(r["reason"],"late_chase")
    def test_choppy_sideways_excluded_both_directions(self):
        # Realistic two-sided whipsaws: high travel, little net progress.
        sample=[]
        for i in range(14):
            midpoint=100+(-1 if i%2 else 1)*.12+i*.003
            op=midpoint+(-.015 if i%2 else .015)
            sample.append(dict(t=(OPEN+timedelta(minutes=i*5)).isoformat(),
                               o=op,h=max(op,midpoint)+.08,l=min(op,midpoint)-.08,
                               c=midpoint,v=45000,n=120,vw=(op+midpoint)/2))
        r=v.evaluate("NVDA",sample,OPEN,OPEN+timedelta(minutes=70),spy_move=0)
        self.assertEqual(r["quality"],"READY")
        self.assertEqual(r["reason"],"choppy_chart")
        self.assertTrue(r["choppy"])
        self.assertFalse(r["eligible"])
        self.assertGreaterEqual(r["choppiness"],61.8)
    def test_missing_structure_window_bars_stay_disqualified(self):
        sample=bars(count=14)
        del sample[-9]
        row=v.evaluate("NVDA",sample,OPEN,OPEN+timedelta(minutes=70),spy_move=0)
        self.assertEqual(row["reason"],"structure_gap")
        self.assertFalse(row["eligible"])
    def test_clean_trend_is_not_choppy_but_may_be_too_late_to_enter(self):
        # Clean multi-bar trend != an attractive fresh entry.
        r=v.evaluate("NVDA",bars(count=14),OPEN,OPEN+timedelta(minutes=70),spy_move=0)
        self.assertFalse(r["choppy"])
        self.assertEqual(r["direction"],"BULLISH")
        self.assertIn(r["reason"],("qualified","late_chase"))
        if r["reason"]=="late_chase":
            self.assertFalse(r["eligible"])
            self.assertEqual(r["chase"],"HIGH")
    def test_extreme_bearish_extension_not_selected(self):
        sample=bars(direction=-1,spike=True)
        r=v.evaluate("HIMS",sample,OPEN,OPEN+timedelta(minutes=40),spy_move=0)
        self.assertEqual(r["direction"],"BEARISH")
        self.assertEqual(r["reason"],"late_chase")
        self.assertFalse(r["eligible"])
    def test_chop_and_late_chase_are_removed_not_replaced(self):
        base=[dict(ticker="NVDA",direction="BULLISH"),
              dict(ticker="HIMS",direction="BEARISH")]
        rows=[dict(ticker="NVDA",direction="NEUTRAL",quality="READY",reason="choppy_chart"),
              dict(ticker="HIMS",direction="BEARISH",quality="READY",reason="late_chase")]
        statuses,removed=v.compare(base,[],rows)
        self.assertEqual(statuses,{})
        self.assertEqual({r["ticker"]:r["status"] for r in removed},
                         {"NVDA":"CHOPPY","HIMS":"CHASE"})
        self.assertEqual(v.select(rows),[])

    def test_flip_weakness_and_no_forced_ten(self):
        pre=[{"ticker":"NVDA","direction":"BULLISH"},
             {"ticker":"HIMS","direction":"BEARISH"}]
        latest=[{"ticker":"NVDA","direction":"BEARISH"},
                {"ticker":"GLW","direction":"BULLISH"}]
        rows=[dict(ticker="HIMS",reason="direction_conflict",quality="READY",direction="NEUTRAL")]
        status,removed=v.compare(pre,latest,rows)
        self.assertEqual(status["NVDA"],"FLIP")
        self.assertEqual(status["GLW"],"NEW")
        self.assertEqual(removed[0]["status"],"WEAKNESS")
        self.assertEqual(v.select([]),[])
    def test_message_under_telegram_limit(self):
        selected=[dict(ticker="S"+str(i),direction=("BULLISH" if i<10 else "BEARISH"),score=72.4,
                       price=123.12,chase="LOW") for i in range(20)]
        r=dict(day="2026-11-10",at=NOW.isoformat(),ready_count=150,selected=selected,
               statuses={p["ticker"]:"NEW" for p in selected},removed=[])
        self.assertLess(len(v.message(r)),3900)
        self.assertIn("WATCH ONLY",v.message(r))
        self.assertIn("Top 10 강제 충원 없음",v.message(r))

class Worker(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store=Store(tmp.name+"/db")
        self.sent=[]
        with self.store.transaction() as tx:
            tx.put("preopen-watch-v1:2026-11-10:report",
                   {"prior_rth_context":{"selected":[dict(ticker="NVDA",direction="BULLISH"),
                                                       dict(ticker="HIMS",direction="BEARISH")]}})
    def test_hourly_send_once_against_preopen_and_persist(self):
        universe=["SPY","NVDA","HIMS"]
        dataset=dict(SPY=bars(1),NVDA=bars(1),HIMS=bars(-1))
        with patch.object(v,"calendar",return_value=(TODAY,None)),patch.object(v,"pre_config",return_value={"symbols":universe}),patch.object(v,"gather",return_value=dataset):
            result=v.worker(self.store,self.sent.append,NOW)
            self.assertEqual(result["status"],"scanned")
            self.assertTrue(result["telegram_queued"])
            self.assertEqual(len(self.sent),1)
            again=v.worker(self.store,self.sent.append,NOW)
            self.assertEqual(again["status"],"already_scanned")
            self.assertEqual(len(self.sent),1)
            later=v.worker(self.store,self.sent.append,NOW+timedelta(minutes=5))
            self.assertEqual(later["status"],"scanned")
            self.assertFalse(later["telegram_queued"])
            self.assertEqual(len(self.sent),1)
        with self.store.transaction() as tx:
            report=tx.get("intraday-v3:2026-11-10:latest")
            self.assertEqual(report["baseline"],"PRIOR_HOURLY" if report["at"]!=NOW.isoformat() else "PREOPEN_0720")
            self.assertEqual(len(tx.get("intraday-v3:2026-11-10:history")),1)
    def test_nov11_shadow_timeline_commits_and_hourly_telegram_sends(self):
        # Regression for Nov 11-only code path: missing "chosen" used to
        # roll back both the scanner report and 8AM Telegram enqueue.
        next_open=OPEN+timedelta(days=1)
        next_now=NOW+timedelta(days=1)
        session=dict(date="2026-11-11",open="09:30",close="16:00")
        shift=lambda seq:[dict(row,t=(datetime.fromisoformat(row["t"])+timedelta(days=1)).isoformat())
                          for row in seq]
        universe=["SPY","NVDA","HIMS"]
        dataset=dict(SPY=shift(bars(1)),NVDA=shift(bars(1)),HIMS=shift(bars(-1)))
        with patch.object(v,"calendar",return_value=(session,None)),patch.object(
                v,"pre_config",return_value={"symbols":universe}),patch.object(
                v,"gather",return_value=dataset):
            first=v.worker(self.store,self.sent.append,next_now)
            self.assertEqual(first["status"],"scanned")
            self.assertTrue(first["telegram_queued"])
            self.assertEqual(len(self.sent),1)
            self.assertIn("INTRADAY V3",self.sent[0])
            again=v.worker(self.store,self.sent.append,next_now)
            self.assertEqual(again["status"],"already_scanned")
            self.assertEqual(len(self.sent),1)
        end=(next_now-timedelta(seconds=10)).replace(second=0,microsecond=0)
        end=end-timedelta(minutes=end.minute%5)
        bucket=int(end.timestamp())//300
        with self.store.transaction() as tx:
            snap=tx.get("intraday-v3:2026-11-11:latest")
            timeline=tx.get("intraday-v3:2026-11-11:timeline:"+str(bucket))
            send=tx.execute("SELECT status FROM mf_outbox WHERE id=?",
                            ("intraday-v3:2026-11-11:hour:8:watch",)).fetchone()
        self.assertEqual(snap["mode"],"WATCH_ONLY")
        self.assertEqual(timeline["feed"],"IEX_5M")
        self.assertEqual(timeline["selected"],[
            {k:p.get(k) for k in ("ticker","direction","score","chase","eligible","choppy","late_chase")}
            for p in snap["selected"]])
        self.assertEqual(send[0],"sent")
        self.assertNotIn("orders",timeline)

    def test_outside_hours_silent(self):
        result=v.worker(self.store,self.sent.append,OPEN+timedelta(minutes=20))
        self.assertEqual(result["status"],"outside_intraday_window")
        self.assertFalse(self.sent)

if __name__=="__main__":unittest.main()
