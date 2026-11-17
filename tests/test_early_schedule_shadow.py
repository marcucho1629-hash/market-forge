"""Nov16 historical and Nov18+ daily research-only early Shadow tests."""
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
    down=[candle(i,100-.6*i,99.3-.6*i) for i in range(n)]
    flip_vals=[(100,100.6),(100.6,101),(101,101.5),
               (101.5,100.4),(100.4,99.6)]
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

    def test_daily_sessions_from_nov18_have_independent_keys_and_no_alerts(self):
        # Nov17 was a documented gap; do not pretend to backfill it.
        wed_open=OPEN+timedelta(days=2)
        wed_now=wed_open+timedelta(minutes=3)
        self.assertEqual(e.scheduled_step(wed_now),"MOMENTUM_0733")
        self.assertIsNone(e.scheduled_step(wed_now+timedelta(days=3)))
        shifted={s:[dict(x,t=(datetime.fromisoformat(x["t"])+timedelta(days=2)).isoformat())
                    for x in bars] for s,bars in sample_minute(3).items()}
        session=dict(SESSION,date="2026-11-18")
        with patch.object(e,"calendar",return_value=(session,None)),patch.object(
             e,"bounds",return_value=(wed_open,wed_open+timedelta(hours=6,minutes=30))),patch.object(
             e,"config",return_value={"symbols":list(shifted)}),patch.object(
             e,"fetch_universe",return_value=shifted),patch.object(
             e,"utc",return_value=wed_now+timedelta(seconds=6)):
            first=e.worker(self.db,wed_now)
            repeated=e.worker(self.db,wed_now+timedelta(minutes=1))
        self.assertEqual(first["status"],"recorded",first)
        self.assertEqual(repeated["status"],"already_captured")
        with self.db.transaction() as tx:
            snap=tx.get(e.prefix_for("2026-11-18")+"MOMENTUM_0733")
            old=tx.get(e.PREFIX+"MOMENTUM_0733")
        self.assertIsNotNone(snap)
        self.assertIsNone(old)
        self.assertEqual(snap["day"],"2026-11-18")
        self.assertEqual(snap["orders"],0)
        self.assertFalse(snap["telegram_sent"])
        self.assertIn("rejection_reasons",snap)
        by_day=e.phase_summary(self.db,[],day="2026-11-18")
        self.assertEqual(by_day["stages_recorded"],1)
        self.assertEqual(by_day["date"],"2026-11-18")
        self.assertEqual(e.phase_summary(self.db,[])["stages_recorded"],0)

    def test_five_minute_diagnostic_is_read_only_and_preserves_block_reasons(self):
        from scanner.intraday_v3 import research_diagnostic
        rows=[dict(ticker="PANW",direction="BEARISH",reason="qualified",
                   score=90,selected=True,chase="LOW",choppy=False,
                   late_chase=False,points=dict(momentum=12),price=304),
              dict(ticker="CRWD",reason="choppy_chart",selected=False),
              dict(ticker="TEAM",reason="late_chase",selected=False)]
        original=[dict(r) for r in rows]
        now=OPEN+timedelta(minutes=47)
        end=OPEN+timedelta(minutes=45)
        recorded=research_diagnostic(rows,now,end)
        self.assertEqual(rows,original)
        self.assertEqual(recorded["reason_counts"],
                         {"qualified":1,"choppy_chart":1,"late_chase":1})
        self.assertEqual(recorded["candidates"][0]["momentum"],12)
        self.assertTrue(recorded["candidates"][0]["selected"])
        self.assertEqual(recorded["candidates"][1]["reason"],"choppy_chart")
        self.assertEqual(recorded["bar_cutoff"],end.isoformat())

    def test_live_direction_telegram_only_with_valid_completed_early_data(self):
        live_open=OPEN+timedelta(days=2)
        now=live_open+timedelta(minutes=3,seconds=27)
        shifted={symbol:[dict(row,t=(datetime.fromisoformat(row["t"])+
                   timedelta(days=2)).isoformat()) for row in bars]
                 for symbol,bars in sample_minute(3).items()}
        sent=[]
        with patch.object(e,"calendar",return_value=(dict(SESSION,date="2026-11-18"),None)),patch.object(
             e,"bounds",return_value=(live_open,live_open+timedelta(hours=6,minutes=30))),patch.object(
             e,"config",return_value={"symbols":list(shifted)}),patch.object(
             e,"fetch_universe",return_value=shifted),patch.object(
             e,"utc",return_value=now):
            result=e.worker(self.db,now,send=sent.append)
            repeat=e.worker(self.db,now+timedelta(minutes=1),send=sent.append)
        self.assertEqual(result["status"],"recorded",result)
        self.assertTrue(result["telegram_sent"],result)
        self.assertEqual(repeat["status"],"already_captured")
        self.assertEqual(len(sent),1)
        self.assertIn("LIVE DIRECTION",sent[0])
        self.assertIn("EARLY MOMENTUM",sent[0])
        self.assertIn("SNDK",sent[0])
        self.assertNotIn("NVDA",sent[0])  # extended initial decline
        self.assertIn("\n",sent[0])
        self.assertNotIn("\\n",sent[0])
        with self.db.transaction() as tx:
            history=tx.get(e.prefix_for("2026-11-18")+"MOMENTUM_0733")
            self.assertEqual(history["orders"],0)
            self.assertTrue(history["live_candidates"])
            self.assertTrue(history["telegram_sent"])
            self.assertEqual(history["live_delivery"][0]["status"],"sent")
            alert=tx.execute("SELECT status FROM mf_outbox WHERE id=?",
                             ("early-direction-live:2026-11-18:MOMENTUM_0733",)).fetchone()
        self.assertEqual(alert[0],"sent")

    def test_direction_candidates_reject_chasing_and_repeated_same_side(self):
        rows=[dict(ticker="PANW",side="BEARISH",state="EARLY_MOMENTUM_PRELIM",
                   price=300,session_pct=-.9,spy_relative_pct=.85,
                   iex_notional=850000,entry=False),
              dict(ticker="CRWD",side="BEARISH",state="EXTENDED_NO_ENTRY",
                   price=200,session_pct=-3.8,spy_relative_pct=3.0,
                   iex_notional=2000000,entry=False),
              dict(ticker="NIO",side="BEARISH",state="EARLY_MOMENTUM_PRELIM",
                   price=2.8,session_pct=-.9,spy_relative_pct=1.1,
                   iex_notional=300000,entry=False),
              dict(ticker="ORCL",side="BULLISH",state="EARLY_MOMENTUM_PRELIM",
                   price=115,session_pct=.8,spy_relative_pct=1.0,
                   iex_notional=200000,entry=False)]
        first=e.direction_candidates("MOMENTUM_0733",rows,{})
        self.assertEqual({r["ticker"] for r in first},{"PANW","ORCL"})
        dedup=e.direction_candidates("MOMENTUM_0733",rows,
                                     {"PANW":{"side":"BEARISH"}})
        self.assertNotIn("PANW",{r["ticker"] for r in dedup})
        self.assertEqual(e.direction_candidates("EARLY_150_0745",
            [dict(ticker="PANW",side="BEARISH",state="EARLY_LOW_WATCH",
                  price=298,score=90,iex_notional=500000,chase="MED")],{}),[])

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
