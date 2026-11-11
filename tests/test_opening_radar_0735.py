"""Regress 07:35 opening radar and Nov12 stronger Top10 without changing Nov11."""
import tempfile,unittest
from datetime import datetime,timedelta,timezone
from unittest.mock import patch
from scanner.store import Store
from scanner import intraday_v3 as v
from scanner import opening_radar_0735 as radar

DATE="2026-11-12"
OPEN=datetime(2026,11,12,14,30,tzinfo=timezone.utc)
SESSION={"date":DATE,"open":"09:30","close":"16:00"}

def bar(t,op,cl,volume=5000):
    pad=max(.09,abs(cl-op)*.2)
    return {"t":t.isoformat(),"o":op,"h":max(op,cl)+pad,"l":min(op,cl)-pad,
            "c":cl,"v":volume,"n":100,"vw":(op+cl)/2}

def first_bars():
    spy=[bar(OPEN,700,700.06)]
    bull=[bar(OPEN,100,100.75)]
    bear=[bar(OPEN,100,99.25)]
    return {"SPY":spy,"NVDA":bull,"HIMS":bear}

def ranked(ticker,score,momentum,chase="LOW",eligible=True):
    return {"ticker":ticker,"direction":"BULLISH","score":score,
            "eligible":eligible,"chase":chase,
            "points":{"momentum":momentum},"choppy":False,
            "late_chase":False}

class OpeningRadar(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store=Store(tmp.name+"/radar.db")
        self.sent=[]
    def test_first_closed_5m_detects_both_sides_at_0737(self):
        d=first_bars()
        now=OPEN+timedelta(minutes=7)
        with patch.object(v,"calendar",return_value=(SESSION,None)),patch.object(
                v,"pre_config",return_value={"symbols":["SPY","NVDA","HIMS"]}),patch.object(
                v,"gather",return_value=d):
            r=v.worker(self.store,self.sent.append,now)
            repeat=v.worker(self.store,self.sent.append,now)
        self.assertEqual(r["status"],"radar_scanned",r)
        self.assertEqual(r["new"],2)
        self.assertEqual(r["delivery"][0]["status"],"sent")
        self.assertEqual(repeat["status"],"radar_already_scanned")
        self.assertEqual(len(self.sent),1)
        self.assertIn("07:35 OPEN RADAR",self.sent[0])
        self.assertIn("CHASE LOW 검증 전 (UNKNOWN)",self.sent[0])
        self.assertIn("매매 신호 아님",self.sent[0])
        self.assertNotIn("MC/SCMP",self.sent[0])
        with self.store.transaction() as tx:
            self.assertIsNone(tx.get("intraday-v3:"+DATE+":latest"))
            self.assertIsNone(tx.get("intraday-v3:"+DATE+":hour:7"))
            self.assertEqual(tx.get("intraday-v3:"+DATE+":early:radar:latest")["new"],2)
    def test_missing_or_future_bars_do_not_generate_fake_alert(self):
        d=first_bars()
        end=OPEN+timedelta(minutes=5)
        missing=radar.evaluate("NVDA",[],d["SPY"],OPEN,end)
        self.assertEqual(missing["reason"],"missing_5m_bar")
        injected=d["NVDA"]+[bar(OPEN+timedelta(minutes=5),100.75,101.5)]
        x=radar.evaluate("NVDA",injected,d["SPY"],OPEN,end)
        baseline=radar.evaluate("NVDA",d["NVDA"],d["SPY"],OPEN,end)
        self.assertEqual(x,baseline)
    def test_one_bar_cannot_claim_low_or_validated_momentum(self):
        d=first_bars()
        r=radar.evaluate("NVDA",d["NVDA"],d["SPY"],OPEN,OPEN+timedelta(minutes=5))
        self.assertTrue(r["eligible"],r)
        self.assertEqual(r["chase"],"UNKNOWN")
        self.assertNotIn("score",r)
        self.assertNotIn("entry",r)
    def test_chase_spike_and_low_liquidity_blocked(self):
        d=first_bars();end=OPEN+timedelta(minutes=5)
        spike=radar.evaluate("NVDA",[bar(OPEN,100,104)],d["SPY"],OPEN,end)
        self.assertEqual(spike["reason"],"opening_spike_chase")
        little=radar.evaluate("NVDA",[bar(OPEN,100,100.75,volume=100)],
                              d["SPY"],OPEN,end)
        self.assertEqual(little["reason"],"iex_liquidity_low")
    def test_duplicate_0742_not_resent_and_new_candidate_can_alert(self):
        d=first_bars()
        with patch.object(v,"calendar",return_value=(SESSION,None)),patch.object(
                v,"pre_config",return_value={"symbols":["SPY","NVDA","HIMS"]}),patch.object(
                v,"gather",return_value=d):
            v.worker(self.store,self.sent.append,OPEN+timedelta(minutes=7))
        two={
          "SPY":[d["SPY"][0],bar(OPEN+timedelta(minutes=5),700.06,700.12)],
          "NVDA":[d["NVDA"][0],bar(OPEN+timedelta(minutes=5),100.75,101.15)],
          "HIMS":[d["HIMS"][0],bar(OPEN+timedelta(minutes=5),99.25,98.9)]
        }
        with patch.object(v,"calendar",return_value=(SESSION,None)),patch.object(
                v,"pre_config",return_value={"symbols":["SPY","NVDA","HIMS"]}),patch.object(
                v,"gather",return_value=two):
            r=v.worker(self.store,self.sent.append,OPEN+timedelta(minutes=12))
        self.assertEqual(r["status"],"radar_scanned")
        self.assertEqual(r["new"],0)
        self.assertEqual(len(self.sent),1)
    def test_missing_spy_no_recommendation(self):
        d=first_bars();d["SPY"]=[]
        with patch.object(v,"calendar",return_value=(SESSION,None)),patch.object(
                v,"pre_config",return_value={"symbols":["SPY","NVDA","HIMS"]}),patch.object(
                v,"gather",return_value=d):
            r=v.worker(self.store,self.sent.append,OPEN+timedelta(minutes=7))
        self.assertEqual(r["new"],0)
        self.assertFalse(self.sent)
    def test_nov11_has_no_premature_new_radar(self):
        older=OPEN-timedelta(days=1)
        old_session={"date":"2026-11-11","open":"09:30","close":"16:00"}
        with patch.object(v,"calendar",return_value=(old_session,None)):
            r=v.worker(self.store,self.sent.append,older+timedelta(minutes=7))
        self.assertEqual(r["status"],"outside_intraday_window")
    def test_0747_uses_existing_three_bar_early_watch(self):
        with patch.object(v,"calendar",return_value=(SESSION,None)),patch.object(
                v,"pre_config",return_value={"symbols":["SPY","NVDA"]}),patch.object(
                v,"opening_worker",return_value={"status":"early_open_scanned"}) as old:
            r=v.worker(self.store,self.sent.append,OPEN+timedelta(minutes=17))
        self.assertEqual(r["status"],"early_open_scanned")
        old.assert_called_once()

class StrongTop10(unittest.TestCase):
    def test_strong_requires_70_momentum_8_and_chase_low(self):
        rows=[ranked("NVDA",83,12),
              ranked("APP",74,7),ranked("AAPL",69,14),
              ranked("TSLA",88,12,"MED"),
              ranked("META",71,8),
              ranked("SPY",95,15,eligible=False)]
        baseline=v.select([dict(x) for x in rows])
        self.assertEqual(len(baseline),5)
        chosen=v.select(rows,strong=True)
        self.assertEqual({x["ticker"] for x in chosen},{"NVDA","META"})
        self.assertTrue(all(x["score"]>=70 and x["chase"]=="LOW" for x in chosen))
        self.assertFalse(any(x["selected"] for x in rows if x["ticker"]=="APP"))
    def test_weak_thirty_to_sixty_point_filler_excluded(self):
        rows=[ranked("JPM",58,13),ranked("AXP",56.4,10),
              ranked("NET",74.5,8),ranked("MRK",77.6,11,"MED")]
        chosen=v.select(rows,strong=True)
        self.assertEqual([x["ticker"] for x in chosen],["NET"])
        self.assertNotIn("JPM",{x["ticker"] for x in chosen})
        self.assertLess(len(chosen),10)
    def test_20261111_policy_still_legacy(self):
        self.assertGreaterEqual(v.MIN_SCORE,55)
        self.assertEqual(v.STRONG_START_DATE,"2026-11-12")
        self.assertEqual(len(v.select([ranked("JPM",58,13)],strong=False)),1)

if __name__=="__main__":unittest.main()
