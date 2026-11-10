"""Scheduled 13:40 MT overnight research watch, no trading or private API calls."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from scanner.store import Store
from scanner import overnight_watch as watch

WINTER=datetime(2026,11,10,20,40,tzinfo=timezone.utc)
SUMMER=datetime(2026,7,10,19,40,tzinfo=timezone.utc)

def snapshot(now=WINTER):
    return dict(day=now.astimezone(watch.DENVER).date().isoformat(),
        mode="WATCH_ONLY",at=(now-timedelta(minutes=3)).isoformat(),
        bar_cutoff=(now-timedelta(minutes=5)).isoformat(),
        ready_count=150,selected=[
            dict(ticker="NVDA",direction="BULLISH",eligible=True,
                 score=85.5,price=178.15,chase="LOW",
                 bar_end=(now-timedelta(minutes=5)).isoformat()),
            dict(ticker="HIMS",direction="BEARISH",eligible=True,
                 score=81.3,price=22.88,chase="MED",
                 bar_end=(now-timedelta(minutes=5)).isoformat()),
            dict(ticker="AVGO",direction="BULLISH",eligible=True,
                 score=92.0,price=310.21,chase="HIGH",
                 bar_end=(now-timedelta(minutes=5)).isoformat()),
            dict(ticker="MSFT",direction="BULLISH",eligible=True,
                 score=65.0,price=546.0,chase="LOW",
                 bar_end=(now-timedelta(minutes=20)).isoformat()),
            dict(ticker="AAPL",direction="BULLISH",eligible=True,
                 score=64.9,price=220.0,chase="LOW",
                 bar_end=(now-timedelta(minutes=5)).isoformat()),
        ])

class CandidateChecks(unittest.TestCase):
    def test_filters_stale_ticker_chase_and_low_scores(self):
        picks,quality=watch.candidates(snapshot(),WINTER)
        self.assertEqual(quality,"READY")
        self.assertEqual([x["ticker"] for x in picks],["NVDA","HIMS"])
    def test_flagged_chop_or_late_chase_never_enters_overnight(self):
        data=snapshot()
        data["selected"][0]["choppy"]=True
        data["selected"][1]["late_chase"]=True
        items,quality=watch.candidates(data,WINTER)
        self.assertEqual(quality,"READY")
        self.assertEqual(items,[])
    def test_snapshot_unavailable_no_fallback(self):
        self.assertEqual(watch.candidates({},WINTER),([],"MISSING_SCANNER_SNAPSHOT"))
        stale=snapshot(WINTER-timedelta(minutes=30))
        self.assertEqual(watch.candidates(stale,WINTER)[1],"STALE_SCANNER_SNAPSHOT")
        different=snapshot()
        different["day"]="2026-11-09"
        self.assertEqual(watch.candidates(different,WINTER)[1],"WRONG_SESSION")
    def test_mst_and_mdt_correct_1340_only(self):
        self.assertTrue(watch.is_window(WINTER))
        self.assertTrue(watch.is_window(SUMMER))
        self.assertFalse(watch.is_window(WINTER-timedelta(hours=1)))
        self.assertFalse(watch.is_window(SUMMER+timedelta(hours=1)))
        self.assertFalse(watch.is_window(WINTER.replace(minute=39)))
    def test_message_is_research_not_entry(self):
        report=dict(day="2026-11-10",at=WINTER.isoformat(),
            bar_cutoff=(WINTER-timedelta(minutes=5)).isoformat(),
            quality="READY",picks=watch.candidates(snapshot(),WINTER)[0],
            statuses={"NVDA":"NEW","HIMS":"CONTINUE"})
        text=watch.message(report)
        for phrase in ("OVERNIGHT SWING WATCH","실제 진입 신호 아님","옵션 거래량: 미검증","NVDA","HIMS"):
            self.assertIn(phrase,text)
        self.assertLess(len(text),3900)

class WorkerTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store=Store(temp.name+"/db")
        self.sent=[]
        self.day="2026-11-10"
    def seeded(self,now=WINTER):
        day=now.astimezone(watch.DENVER).date().isoformat()
        with self.store.transaction() as tx:
            tx.put("intraday-v3:"+day+":latest",snapshot(now))
            tx.put("intraday-v3:"+day+":hourly_last",{
                "selected":[dict(ticker="NVDA",direction="BULLISH"),
                            dict(ticker="HIMS",direction="BULLISH")]})
    def _run(self,now=WINTER):
        session={"date":now.astimezone(watch.DENVER).date().isoformat(),
                 "open":"09:30","close":"16:00"}
        with patch.object(watch,"calendar",return_value=(session,None)):
            return watch.worker(self.store,self.sent.append,now)
    def test_sent_once_only_with_statuses(self):
        self.seeded()
        result=self._run()
        self.assertEqual(result["status"],"sent_or_recorded")
        self.assertEqual((result["bullish"],result["bearish"]),(1,1))
        self.assertEqual([x["status"] for x in result["delivery"]],["sent"])
        self.assertEqual(len(self.sent),1)
        self.assertIn("CONTINUE",self.sent[0])
        self.assertIn("FLIP",self.sent[0])
        again=self._run(WINTER+timedelta(minutes=2))
        self.assertFalse(again["created"])
        self.assertEqual(len(self.sent),1)
        with self.store.transaction() as tx:
            self.assertIsNotNone(tx.get("overnight-watch-v1:2026-11-10:1340"))
            self.assertEqual(
                tx.execute("SELECT COUNT(*) FROM mf_outbox WHERE id LIKE ?",
                           ("overnight-watch-v1:2026-11-10:1340%",)).fetchone()[0],1)
    def test_stale_scan_queues_explicit_no_candidate_warning(self):
        self.seeded(WINTER-timedelta(minutes=30))
        with self.store.transaction() as tx:
            stale=tx.get("intraday-v3:2026-11-10:latest")
            tx.put("intraday-v3:2026-11-10:latest",stale)
        result=self._run(WINTER)
        self.assertEqual(result["quality"],"STALE_SCANNER_SNAPSHOT")
        self.assertEqual(result["bullish"],0)
        self.assertEqual(result["bearish"],0)
        self.assertIn("추천 보류",self.sent[0])
    def test_outside_window_no_send(self):
        self.seeded()
        result=self._run(WINTER-timedelta(minutes=11))
        self.assertEqual(result["status"],"outside_1340_mountain_window")
        self.assertEqual(self.sent,[])

if __name__=="__main__":unittest.main()
