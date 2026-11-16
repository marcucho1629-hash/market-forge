"""07:33/07:35 1m and 07:40/07:45 5m Shadow, Nov16 only."""
import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from unittest.mock import patch
from scanner.store import Store
from scanner import early_schedule_shadow as e
from scanner import day_v34_early_flip_freshness as v34

OPEN=datetime(2026,11,16,14,30,tzinfo=timezone.utc)
SESSION=dict(date=e.DAY,open="09:30",close="16:00")
def candle(i,o,c,frame=1,v=600):
    return dict(t=(OPEN+timedelta(minutes=frame*i)).isoformat(),
                o=o,c=c,h=max(o,c)+.12,l=min(o,c)-.12,v=v,n=11,vw=(o+c)/2)

def sample_minute(n=5):
    flat=[candle(i,100,100) for i in range(n)]
    spy=[candle(i,500,500) for i in range(n)]
    down=[candle(i,100-.5*i,99.5-.5*i) for i in range(n)]
    flip_vals=[(100,100.6),(100.6,101),(101,101.1),
               (101.1,100.4),(100.4,99.6)]
    flip=[candle(i,o,c) for i,(o,c) in enumerate(flip_vals[:n])]
    return {"SPY":spy,"NVDA":down,"SNDK":flip,
            **{f"TEST{i:03d}":flat for i in range(58)}}

def sample_five(n=3):
    spy=[candle(i,500,500,frame=5) for i in range(n)]
    flip_vals=[(100,100.8),(100.8,98.6),(98.6,97.8)]
    down_vals=[(100,97),(97,96.4),(96.4,96)]
    flip=[candle(i,o,c,frame=5) for i,(o,c) in enumerate(flip_vals[:n])]
    down=[candle(i,o,c,frame=5) for i,(o,c) in enumerate(down_vals[:n])]
    flat=[candle(i,100,100,frame=5) for i in range(n)]
    return {"SPY":spy,"SNDK":flip,"NVDA":down,
            **{f"TEST{i:03d}":flat for i in range(58)}}

class ResearchStages(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db=Store(temp.name+"/early.sqlite")

    def test_exact_schedule_and_one_minute_provider_retry(self):
        for stage,(target,_,__) in e.TARGETS.items():
            now=OPEN+timedelta(minutes=target-30)
            self.assertEqual(e.scheduled_step(now),stage)
            self.assertEqual(e.scheduled_step(now+timedelta(minutes=1)),stage)
            self.assertNotEqual(e.scheduled_step(now+timedelta(minutes=2)),stage)
        self.assertIsNone(e.scheduled_step(OPEN-timedelta(days=1)+timedelta(minutes=3)))
        self.assertIsNone(e.scheduled_step(OPEN+timedelta(days=1,minutes=3)))

    def test_733_completed_first_three_not_entry(self):
        r=e.compute("MOMENTUM_0733",sample_minute(3),OPEN,OPEN+timedelta(minutes=3))
        self.assertEqual(r["status"],"ready",r)
        z={x["ticker"]:x for x in r["rows"]}
        self.assertEqual(z["NVDA"]["state"],"EXTENDED_NO_ENTRY")
        self.assertEqual(z["NVDA"]["chase"],"UNKNOWN")
        self.assertFalse(z["NVDA"]["entry"])
        self.assertEqual(z["SNDK"]["state"],"EARLY_MOMENTUM_PRELIM")

    def test_735_preliminary_flip_from_only_five_completed_1min(self):
        r=e.compute("POSSIBLE_FLIP_0735",sample_minute(5),OPEN,
                    OPEN+timedelta(minutes=5))
        self.assertEqual(r["status"],"ready")
        sndk=next(x for x in r["rows"] if x["ticker"]=="SNDK")
        self.assertEqual(sndk["state"],"POSSIBLE_FLIP")
        self.assertEqual(sndk["side"],"BEARISH")
        self.assertFalse(sndk["entry"])
        self.assertEqual(sndk["chase"],"UNKNOWN")

    def test_740_two_bar_reversal_and_745_three_bar_low(self):
        r=e.compute("CONFIRM_0740",sample_five(2),OPEN,OPEN+timedelta(minutes=10))
        self.assertEqual(r["status"],"ready")
        vals={x["ticker"]:x for x in r["rows"]}
        self.assertEqual(vals["SNDK"]["state"],"EARLY_FLIP_WATCH")
        self.assertEqual(vals["NVDA"]["state"],"EXTENDED_NO_ENTRY")
        later=e.compute("EARLY_150_0745",sample_five(3),OPEN,
                        OPEN+timedelta(minutes=15))
        self.assertEqual(later["status"],"ready")
        nvda=next(x for x in later["rows"] if x["ticker"]=="NVDA")
        self.assertEqual(nvda["state"],"EXTENDED_NO_ENTRY")
        self.assertFalse(nvda["entry"])

    def test_incomplete_or_missing_spy_fails_closed(self):
        data=sample_minute(3)
        data["NVDA"]=[data["NVDA"][0],data["NVDA"][2]]
        r=e.compute("MOMENTUM_0733",data,OPEN,OPEN+timedelta(minutes=3))
        self.assertEqual(r["status"],"ready")
        self.assertNotIn("NVDA",{x["ticker"] for x in r["rows"]})
        data.pop("SPY")
        bad=e.compute("MOMENTUM_0733",data,OPEN,OPEN+timedelta(minutes=3))
        self.assertEqual(bad["status"],"insufficient_complete_iex_bars")
        self.assertFalse(bad["spy_complete"])
        sparse={"SPY":sample_minute(3)["SPY"]}
        self.assertEqual(e.compute("MOMENTUM_0733",sparse,OPEN,OPEN+timedelta(minutes=3))["status"],
                         "insufficient_complete_iex_bars")

    def test_real_observation_time_and_retries_no_duplicate(self):
        now=OPEN+timedelta(minutes=3)
        with patch.object(e,"calendar",return_value=(SESSION,None)),patch.object(
              e,"config",return_value={"symbols":list(sample_minute(3))}),patch.object(
              e,"fetch_universe",return_value=sample_minute(3)),patch.object(
              e,"utc",return_value=now+timedelta(seconds=5)):
            first=e.worker(self.db,now)
            repeat=e.worker(self.db,now+timedelta(minutes=1))
        self.assertEqual(first["status"],"recorded",first)
        self.assertEqual(first["stage"],"MOMENTUM_0733")
        self.assertEqual(repeat["status"],"already_captured")
        self.assertEqual(first["observed_at"],(now+timedelta(seconds=5)).isoformat())
        with self.db.transaction() as tx:
            stored=tx.get(e.PREFIX+"MOMENTUM_0733")
        self.assertEqual(stored["orders"],0)
        self.assertFalse(stored["telegram_sent"])
        self.assertEqual(stored["input_frame"],"1Min")

    def test_first_minute_missing_does_not_save_and_retry_can_recover(self):
        now=OPEN+timedelta(minutes=3)
        with patch.object(e,"calendar",return_value=(SESSION,None)),patch.object(
              e,"config",return_value={"symbols":list(sample_minute(3))}),patch.object(
              e,"fetch_universe",side_effect=[{"SPY":sample_minute(3)["SPY"]},
                                               sample_minute(3)]),patch.object(
              e,"utc",return_value=now+timedelta(minutes=1,seconds=10)):
            failed=e.worker(self.db,now)
            recovered=e.worker(self.db,now+timedelta(minutes=1))
        self.assertEqual(failed["status"],"insufficient_complete_iex_bars")
        self.assertEqual(recovered["status"],"recorded")
        self.assertEqual(recovered["observed_at"],(now+timedelta(minutes=1,seconds=10)).isoformat())

    def test_post_close_stage_comparison_not_simulated_trade(self):
        now=OPEN+timedelta(minutes=3)
        with patch.object(e,"calendar",return_value=(SESSION,None)),patch.object(
             e,"config",return_value={"symbols":list(sample_minute(3))}),patch.object(
             e,"fetch_universe",return_value=sample_minute(3)),patch.object(
             e,"utc",return_value=now+timedelta(seconds=5)):
            e.worker(self.db,now)
        after=now+timedelta(minutes=30)
        events=[dict(source="FIVE_MIN",at=after.isoformat(),
                  rows=[dict(ticker="SNDK",side="BULLISH",strong_low=True)])]
        summary=e.phase_summary(self.db,events)
        self.assertEqual(summary["stages_recorded"],1)
        self.assertEqual(summary["stages_expected"],4)
        self.assertEqual(summary["early_preceded_strong_low"],1)
        self.assertGreater(summary["lead_examples"][0]["lead_minutes"],0)
        self.assertEqual(summary["stages"]["POSSIBLE_FLIP_0735"]["status"],"not_captured")

    def test_existing_v34_report_can_run_without_all_four_phases(self):
        event=dict(source="FIVE_MIN",feed="IEX_5M",
                   at=(OPEN+timedelta(minutes=32)).isoformat(),
                   asof=(OPEN+timedelta(minutes=30)).isoformat(),hourly=True,
                   rows=[dict(ticker="NVDA",side="BEARISH",strong_low=True,
                              selected=True,price=96,session_extension_atr=2,chase="LOW")])
        report=v34.summary([event],OPEN+timedelta(hours=7))
        self.assertEqual(report["status"],"ready")
        missing=e.phase_summary(self.db,[event])
        self.assertEqual(missing["status"],"no_early_captures")
        self.assertEqual(missing["stages_recorded"],0)

if __name__=="__main__":unittest.main()
