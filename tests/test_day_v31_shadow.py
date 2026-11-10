"""Isolated Nov 11 A/B 3m vs 1+2+3 research comparison; no orders."""
import json
import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from unittest.mock import patch

from scanner.store import Store
from scanner.v136_live.engine import Bar, Engine, Config
from scanner import day_v31_shadow as shadow

OPEN=datetime(2026,11,11,14,30,tzinfo=timezone.utc)
CLOSE=OPEN+timedelta(hours=6,minutes=30)
NOW=CLOSE+timedelta(minutes=7)
SESSION={"date":"2026-11-11","is_open":True,
         "open":OPEN.isoformat(),"close":CLOSE.isoformat()}
EVENT_TIME=OPEN+timedelta(minutes=36)

def native_rows(n=55):
    rows=[]
    for i in range(n):
        p=100+i*.04
        rows.append(dict(t=(OPEN+timedelta(minutes=i)).isoformat(),
             o=round(p,4),h=round(p+.06,4),l=round(p-.04,4),
             c=round(p+.03,4),v=10000,n=50,vw=round(p+.015,4)))
    return rows

def timeline(at=EVENT_TIME-timedelta(minutes=1),ticker="NVDA",chase="LOW"):
    snap={"observed_at":at.isoformat(),
          "bar_cutoff":(at-timedelta(minutes=2)).isoformat(),
          "feed":"IEX_5M","ready_count":140,
          "selected":[dict(ticker=ticker,direction="BULLISH",score=80,
                            eligible=True,chase=chase,choppy=False,late_chase=False)]}
    return [(at,snap)]

def event(mode="closed_3m",time=EVENT_TIME,ticker="NVDA"):
    return dict(ticker=ticker,action="MC",side=1,
                time=time.isoformat(),price=101.20,
                reason="research",mode=mode)

class BarEngineMode(unittest.TestCase):
    def test_closed_3m_mode_requires_no_one_two_alignment(self):
        class Forced3m(Engine):
            def _entry_side(self,w,vwap):
                return 1 if w.tf==3 else 0
        class ForcedAll(Engine):
            def _entry_side(self,w,vwap):
                return 1 if w.tf in (1,2,3) else 0
        # With the same completed input, B can signal when A cannot.
        entries={}
        for name,cls,mode in (("A",Forced3m,"aligned_123"),
                               ("B",Forced3m,"closed_3m"),
                               ("A_all",ForcedAll,"aligned_123")):
            engine=cls(Config(entry_mode=mode))
            found=[]
            for i in range(1,7):
                t=OPEN+timedelta(minutes=i)
                bar=Bar("NVDA",t,100,100.2,99.8,100.1,10000)
                found.extend(e for e in engine.on_bar(bar) if e.action in ("MC","SCMP"))
            entries[name]=found
        self.assertEqual(len(entries["A"]),0)
        self.assertGreaterEqual(len(entries["B"]),1)
        self.assertTrue(all(e.time==OPEN+timedelta(minutes=3) for e in entries["B"]))
        self.assertGreaterEqual(len(entries["A_all"]),1)
        self.assertEqual(Config().entry_mode,"aligned_123")

class WatchMatching(unittest.TestCase):
    def test_no_future_top10_snapshot_leak(self):
        ev=event()
        future=timeline(at=EVENT_TIME+timedelta(seconds=20))
        result=shadow.gate_event(ev,future)
        self.assertEqual(result["status"],"NO_ASOF_TOP10")
    def test_low_med_wrong_direction_and_missing(self):
        ev=event()
        self.assertEqual(shadow.gate_event(ev,timeline())["status"],"TOP10_LOW_PASS")
        self.assertEqual(shadow.gate_event(ev,timeline(chase="MED"))["status"],"TOP10_MED_WATCH")
        self.assertEqual(shadow.gate_event(ev,timeline(ticker="AAPL"))["status"],"NOT_IN_TOP10")
        snap=timeline()
        snap[0][1]["selected"][0]["direction"]="BEARISH"
        self.assertEqual(shadow.gate_event(ev,snap)["status"],"OPPOSITE_TOP10")
    def test_stale_snapshot_does_not_pass(self):
        old=timeline(at=EVENT_TIME-timedelta(minutes=13))
        self.assertEqual(shadow.gate_event(event(),old)["status"],"STALE_TOP10")
    def test_exact_completed_future_horizons_not_fabricated(self):
        bars=[Bar("NVDA",EVENT_TIME,100,101,99,100,10000),
              Bar("NVDA",EVENT_TIME+timedelta(minutes=5),101,102,100,102,10000),
              Bar("NVDA",EVENT_TIME+timedelta(minutes=15),104,105,103,104,10000)]
        e=event();e["price"]=100
        r=shadow.forward_returns(e,bars)
        self.assertEqual(r,{"5":2.0,"15":4.0,"30":None})
    def test_history_invalid_snapshot_ignored(self):
        a=timeline()[0][1]
        b={**a,"observed_at":(EVENT_TIME+timedelta(minutes=4)).isoformat()}
        out=shadow.history_from_rows([(json.dumps(a),),(json.dumps(b),)])
        self.assertEqual(len(out),2)
        self.assertEqual(shadow.gate_event(event(),out)["status"],"TOP10_LOW_PASS")
    def test_date_and_mst_window_exact(self):
        self.assertTrue(shadow.window(NOW))
        self.assertFalse(shadow.window(NOW-timedelta(minutes=2)))
        self.assertFalse(shadow.window(NOW+timedelta(minutes=15)))
        self.assertFalse(shadow.window(NOW+timedelta(days=1)))

class EndToEnd(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store=Store(tmp.name+"/test.db")
        self.sent=[]
        with self.store.transaction() as tx:
            tx.put("intraday-v3:2026-11-11:timeline:"+str(1),
                   timeline()[0][1])
            tx.put("preopen-watch-v1:2026-11-11:report",{
                "prior_rth_context":{"selected":[{"ticker":"NVDA","direction":"BULLISH"}]}})
    def run_worker(self,now=NOW):
        with patch.object(shadow,"schedule",return_value=SESSION),patch.object(
                shadow,"fetch",return_value={"NVDA":native_rows()}),patch.object(
                shadow,"replay_one",side_effect=lambda symbol,bars,mode,opened:
                    ([event(mode)] if mode=="closed_3m" else [],0)):
            return shadow.worker(self.store,self.sent.append,now)
    def test_once_report_keeps_a_b_provenance_and_top10_low(self):
        out=self.run_worker()
        self.assertEqual(out["status"],"recorded")
        self.assertEqual((out["A"],out["B"],out["B_low"]),(0,1,1))
        self.assertEqual([s["status"] for s in out["delivery"]],["sent"])
        self.assertEqual(len(self.sent),1)
        self.assertIn("B 확정 3분 단독: 1개",self.sent[0])
        second=self.run_worker(NOW+timedelta(minutes=1))
        self.assertEqual(second["status"],"already_recorded")
        self.assertEqual(len(self.sent),1)
        with self.store.transaction() as tx:
            result=tx.get("day-v31-shadow:2026-11-11:report")
        self.assertEqual(result["universe_total"],20)
        self.assertEqual(result["watch_timeline_snapshots"],1)
        self.assertEqual(result["signals_B"][0]["gate"]["status"],"TOP10_LOW_PASS")
        self.assertIsNone(result["signals_B"][0]["observed_at"])
        self.assertEqual(result["source_symbols_with_25min"],1)
        self.assertFalse(result["orders"])
    def test_no_matching_top10_record_is_not_claimed(self):
        with self.store.transaction() as tx:
            tx.execute("DELETE FROM mf_kv WHERE key LIKE ?",
                       ("intraday-v3:2026-11-11:timeline:%",))
        out=self.run_worker()
        self.assertEqual(out["B_low"],0)
        with self.store.transaction() as tx:
            report=tx.get("day-v31-shadow:2026-11-11:report")
        self.assertEqual(report["signals_B"][0]["gate"]["status"],"NO_ASOF_TOP10")
        self.assertIn("quality_warning",report)
    def test_no_data_never_sends_fake_success(self):
        with patch.object(shadow,"schedule",return_value=SESSION),patch.object(
                shadow,"fetch",return_value={}):
            out=shadow.worker(self.store,self.sent.append,NOW)
        self.assertEqual(out["status"],"data_unavailable_no_report")
        self.assertEqual(self.sent,[])
    def test_controlled_outside_window_no_request(self):
        self.assertEqual(shadow.worker(self.store,self.sent.append,NOW+timedelta(minutes=40)),
                         {"status":"outside_one_day_post_close_window"})

if __name__=="__main__":unittest.main()
