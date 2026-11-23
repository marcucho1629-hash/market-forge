"""V2.2 regression: research-only 3-minute shadow and 05:30 to 07:30 handoff."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

from scanner.store import Store
from scanner import v22_weakness_shadow as v22
from scanner import early_options_0530 as pre
from scanner import early_schedule_shadow as early

MT=ZoneInfo("America/Denver")
DAY="2026-11-24"


def at(hour, minute, second=12):
    return datetime(2026, 11, 24, hour, minute, second, tzinfo=MT).astimezone(timezone.utc)


def session():
    return dict(date=DAY,open="09:30",close="16:00")


def minutes(start, values):
    result=[]
    for i, (o,c) in enumerate(values):
        result.append(dict(t=(start+timedelta(minutes=i)).isoformat(),
            o=o,h=max(o,c)+.08,l=min(o,c)-.08,c=c,v=1500,n=12,vw=(o+c)/2))
    return result


class Pure(unittest.TestCase):
    def test_three_minute_cutoffs_winter_dst_and_session_boundaries(self):
        self.assertIsNone(v22.slot(at(7,45)))
        self.assertIsNone(v22.slot(at(7,47)))
        self.assertEqual(v22.slot(at(7,48)),at(7,48,0))
        self.assertEqual(v22.slot(at(7,51)),at(7,51,0))
        self.assertEqual(v22.slot(at(13,57)),at(13,57,0))
        self.assertIsNone(v22.slot(at(14,0)))
        summer=datetime(2027,7,7,7,48,13,tzinfo=MT).astimezone(timezone.utc)
        self.assertEqual(v22.slot(summer),summer.replace(second=0))
        prior=datetime(2026,11,23,8,0,tzinfo=MT).astimezone(timezone.utc)
        self.assertIsNone(v22.slot(prior))

    def test_0530_underlyings_stay_in_0733_and_through_rth(self):
        from scanner.early_underlyings import SYMBOLS, tracked_symbols
        from scanner import intraday_v3
        from scanner import early_schedule_shadow
        self.assertEqual(len(SYMBOLS),30)
        self.assertEqual(len(set(SYMBOLS)),30)
        base=["SPY","QQQ","NVDA","ABC"]
        union=tracked_symbols(base)
        self.assertEqual(union[:4],base)
        self.assertTrue(set(SYMBOLS).issubset(set(union)))
        self.assertIn("NOK",union)
        self.assertIn("SPCX",union)
        self.assertIs(intraday_v3.tracked_symbols,tracked_symbols)
        self.assertIs(early_schedule_shadow.tracked_symbols,tracked_symbols)
        self.assertEqual(pre.SYMBOLS,SYMBOLS)

    def test_3min_opposite_weakness_needs_all_three_bars_and_vwap(self):
        start=at(8,3,0)
        raw=minutes(start,[(100,99.9),(99.9,99.5),(99.5,99.1)])
        out=v22.analyze_bar("ORCL","BULLISH",raw,at(7,30,0),at(8,6,0))
        self.assertEqual(out["quality"],"READY",out)
        self.assertTrue(out["weakness"],out)
        self.assertLess(out["original_side_move_pct"],-0.4)
        incomplete=v22.analyze_bar("ORCL","BULLISH",raw[:-1],at(7,30,0),at(8,6,0))
        self.assertFalse(incomplete["weakness"])
        self.assertEqual(incomplete["quality"],"MISSING_1M")
        gap=v22.analyze_bar("ORCL","BULLISH",[raw[0],raw[2]],at(7,30,0),at(8,6,0))
        self.assertEqual(gap["quality"],"MISSING_1M")
        rising=v22.analyze_bar("ORCL","BEARISH",raw,at(7,30,0),at(8,6,0))
        self.assertFalse(rising["weakness"])

    def test_extended_prelim_is_label_not_entry_veto(self):
        pick=dict(ticker="ORCL",side="BULLISH",state="POSSIBLE_FLIP",
                  price=118.43,session_pct=1.60,spy_relative_pct=.8,iex_notional=100000)
        self.assertTrue(early.extended_advisory(pick))
        msg=early.direction_message("POSSIBLE_FLIP_0735",[pick],at(7,35),at(7,35,0))
        self.assertIn("EXTENDED WATCH",msg)
        self.assertFalse(early.extended_advisory(dict(pick,session_pct=.8)))
        self.assertTrue(early.extended_advisory(dict(pick,side="BEARISH",session_pct=-1.3)))
        self.assertFalse(early.extended_advisory(dict(pick,side="BEARISH",session_pct=1.3)))


class Integration(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.store=Store(t.name+"/v22.sqlite")

    def seed_sent(self, status="sent",seen="2026-11-24T14:33:27+00:00"):
        key="early-direction-live:"+DAY+":MOMENTUM_0733"
        with self.store.transaction() as tx:
            tx.put("early-schedule-shadow:"+DAY+":MOMENTUM_0733",
                dict(observed_at=seen,live_candidates=[
                    dict(ticker="ORCL",side="BULLISH",price=100)]))
            tx.enqueue(key,dict(text="initial direction"))
            if status=="sent":
                tx.execute("UPDATE mf_outbox SET status='sent' WHERE id=?",(key,))

    def test_3m_unsent_telegram_never_monitored_and_no_orders(self):
        self.seed_sent("pending")
        with patch.object(v22,"calendar",return_value=(session(),None)), \
             patch.object(v22,"bounds",return_value=(at(7,30,0),at(14,0,0))), \
             patch.object(v22,"fetch",side_effect=AssertionError("fetch should not run")):
            result=v22.worker(self.store,at(8,6))
        self.assertEqual(result["status"],"no_verified_sent_signals")
        self.assertEqual(result["orders"],0)

    def test_3m_sent_only_shadow_no_telegram_or_repeat(self):
        self.seed_sent()
        barstart=at(8,3,0)
        sample=minutes(barstart,[(100,99.9),(99.9,99.5),(99.5,99.1)])
        calls=[]
        def fetch(symbols,frame,start,end):
            calls.append((symbols,frame,start,end))
            return {"ORCL":sample}
        with patch.object(v22,"calendar",return_value=(session(),None)), \
             patch.object(v22,"bounds",return_value=(at(7,30,0),at(14,0,0))), \
             patch.object(v22,"fetch",side_effect=fetch):
            first=v22.worker(self.store,at(8,6))
            repeated=v22.worker(self.store,at(8,6,30))
        self.assertEqual(first["status"],"captured")
        self.assertEqual(first["flagged"],1)
        self.assertFalse(first["telegram"])
        self.assertEqual(first["orders"],0)
        self.assertEqual(repeated["status"],"already_captured")
        self.assertEqual(len(calls),1)
        with self.store.transaction() as tx:
            record=tx.get("v22-weakness-3m:"+DAY+":latest")
            outbox=tx.execute("SELECT count(*) FROM mf_outbox").fetchone()[0]
        self.assertEqual(record["mode"],"SHADOW_ONLY")
        self.assertEqual(outbox,1) # original sent event only

    def test_rth_handoff_only_on_0730_and_uses_fresh_premarket(self):
        recorded=at(7,25,0)
        with self.store.transaction() as tx:
            tx.put("early-options-0530:"+DAY+":latest",
                   dict(cutoff=recorded.isoformat(),selected=[dict(ticker="NVDA")]))
        with patch.object(pre,"calendar",return_value=(session(),None)), \
             patch.object(pre,"bounds",return_value=(at(7,30,0),at(14,0,0))):
            self.assertIsNone(pre.regular_open_handoff(self.store,at(7,25)))
            result=pre.regular_open_handoff(self.store,at(7,30))
            repeat=pre.regular_open_handoff(self.store,at(7,30,35))
        self.assertEqual(result["status"],"regular_open_handoff")
        self.assertEqual(result["premarket"],"SIP_STOCK")
        self.assertEqual(repeat["premarket"],"SIP_STOCK")
        with self.store.transaction() as tx:
            record=tx.get("early-options-0530:"+DAY+":rth_handoff")
        self.assertEqual(record["premarket_symbols"],["NVDA"])
        self.assertEqual(record["tracked_universe"],list(pre.SYMBOLS))
        self.assertEqual(len(record["tracked_universe"]),30)
        self.assertEqual(record["next_early_watch"].split()[0],"07:33")
        self.assertEqual(record["orders"],0)

if __name__=="__main__":
    unittest.main()
