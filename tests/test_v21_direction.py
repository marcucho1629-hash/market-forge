"""V2.1 regression: sent-only alerts, time gates, repeat safety and CHOP."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from scanner.store import Store
from scanner import v21_direction as v21
from scanner.intraday_early_watch import live_low_alert

DAY = "2026-11-23"
AT = datetime(2026, 11, 23, 14, 41, tzinfo=timezone.utc)
MON = datetime(2026, 11, 23, 15, 4, tzinfo=timezone.utc)


def snap(now, cutoff, ticker="ABC", direction="BEARISH", choppy=False):
    return dict(at=(now - timedelta(minutes=2)).isoformat(),
                bar_cutoff=cutoff.isoformat(), rows=[dict(
                    ticker=ticker, quality="READY", choppy=choppy,
                    bar_end=cutoff.isoformat(), direction=direction,
                    price=97.0 if direction=="BEARISH" else 103.0,
                    vwap=100.0)])


class Pure(unittest.TestCase):
    def test_preopen_uses_0740_early_states(self):
        original=[dict(ticker="A",direction="BULLISH"),
                  dict(ticker="B",direction="BULLISH"),
                  dict(ticker="C",direction="BEARISH"),
                  dict(ticker="D",direction="BEARISH")]
        early=[dict(ticker="A",side="BULLISH",state="EARLY_MOMENTUM_WATCH",price=100),
               dict(ticker="B",side="BEARISH",state="EARLY_FLIP_WATCH",price=90),
               dict(ticker="C",side="BULLISH",state="EARLY_UNCONFIRMED",price=110)]
        rows=v21.classify(original,early)
        self.assertEqual([r["status"] for r in rows],
                         ["CONFIRMED","FLIPPED","WEAKNESS","NO_CONFIRM"])
        self.assertIn("NO_CONFIRM",v21.confirmation_text(DAY,rows))

    def test_two_distinct_complete_opposite_bars_required(self):
        base=dict(side="BULLISH",last_bar=None,opposite_streak=0,warned=False,
                  flipped=False)
        first=datetime(2026,11,23,15,0,tzinfo=timezone.utc)
        s,kind=v21.step_direction(base,snap(MON,first)["rows"][0],first)
        self.assertEqual((kind,s["opposite_streak"]),("WEAKNESS",1))
        s["warned"]=True
        second=first+timedelta(minutes=5)
        s,kind=v21.step_direction(s,snap(MON,second)["rows"][0],second)
        self.assertEqual(kind,"CONFIRMED_FLIP")
        s["flipped"]=True
        _,again=v21.step_direction(s,snap(MON,second)["rows"][0],second)
        self.assertIsNone(again)

    def test_chop_and_missing_bars_fail_closed(self):
        first=datetime(2026,11,23,15,0,tzinfo=timezone.utc)
        s=dict(side="BULLISH",last_bar=None,opposite_streak=0,warned=False,
               flipped=False)
        s,result=v21.step_direction(s,snap(MON,first,choppy=True)["rows"][0],first)
        self.assertIsNone(result)
        self.assertEqual(s["opposite_streak"],0)
        later=first+timedelta(minutes=10)
        s,result=v21.step_direction(s,snap(MON,later)["rows"][0],later)
        self.assertEqual(result,"WEAKNESS")
        stale=dict(snap(MON,later)["rows"][0],bar_end=first.isoformat())
        s,result=v21.step_direction(s,stale,later+timedelta(minutes=5))
        self.assertIsNone(result)
        self.assertEqual(s["opposite_streak"],0)


class Integrated(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store=Store(temp.name+"/database.sqlite")
        self.messages=[]

    def seed_early(self, mark_sent):
        event="early-direction-live:"+DAY+":MOMENTUM_0733"
        with self.store.transaction() as tx:
            tx.put("early-schedule-shadow:"+DAY+":MOMENTUM_0733",
                   dict(observed_at="2026-11-23T14:33:27+00:00",
                        live_candidates=[dict(ticker="ABC",side="BULLISH",price=100)]))
            tx.enqueue(event,dict(text="real Telegram candidate"))
            if mark_sent:
                tx.execute("UPDATE mf_outbox SET status='sent' WHERE id=?",(event,))

    def test_confirmation_rejects_unsent_preopen_and_is_idempotent(self):
        with self.store.transaction() as tx:
            tx.put("preopen-watch-v1:"+DAY+":report",
                   dict(selected=[dict(ticker="ABC",direction="BULLISH")]))
            tx.put("early-schedule-shadow:"+DAY+":CONFIRM_0740",
                   dict(input_frame="5Min",cutoff="2026-11-23T14:40:00+00:00",
                        rows=[dict(ticker="ABC",side="BEARISH",
                                   state="EARLY_FLIP_WATCH",price=98)]))
            tx.enqueue("preopen-watch-v1:"+DAY+":watchlist",dict(text="preopen"))
        result=v21.confirm_worker(self.store,self.messages.append,AT)
        self.assertEqual(result["status"],"preopen_report_or_delivery_missing")
        self.assertEqual(self.messages,[])
        with self.store.transaction() as tx:
            tx.execute("UPDATE mf_outbox SET status='sent' WHERE id=?",
                       ("preopen-watch-v1:"+DAY+":watchlist",))
        ok=v21.confirm_worker(self.store,self.messages.append,AT)
        self.assertEqual(ok["flipped"],1)
        self.assertEqual(ok["delivery_states"],["sent"])
        self.assertEqual(len(self.messages),1)
        retry=v21.confirm_worker(self.store,self.messages.append,AT+timedelta(minutes=1))
        self.assertEqual(retry["status"],"already_recorded")
        self.assertEqual(len(self.messages),1)

    def test_unsent_live_signal_never_enters_flip_tracking(self):
        self.seed_early(False)
        cutoff=datetime(2026,11,23,15,0,tzinfo=timezone.utc)
        with self.store.transaction() as tx:
            tx.put("intraday-v3:"+DAY+":latest",snap(MON,cutoff))
        result=v21.monitor_worker(self.store,self.messages.append,MON)
        self.assertEqual(result["tracked"],0)
        self.assertFalse(self.messages)

    def test_weakness_then_flip_once_for_verified_telegram(self):
        self.seed_early(True)
        cutoff=datetime(2026,11,23,15,0,tzinfo=timezone.utc)
        with self.store.transaction() as tx:
            tx.put("intraday-v3:"+DAY+":latest",snap(MON,cutoff))
        first=v21.monitor_worker(self.store,self.messages.append,MON)
        self.assertEqual((first["tracked"],first["warnings"]),(1,1))
        self.assertIn("WEAKNESS",self.messages[0])
        nxt=MON+timedelta(minutes=5)
        nextcutoff=cutoff+timedelta(minutes=5)
        with self.store.transaction() as tx:
            tx.put("intraday-v3:"+DAY+":latest",snap(nxt,nextcutoff))
        second=v21.monitor_worker(self.store,self.messages.append,nxt)
        self.assertEqual(second["warnings"],1)
        self.assertIn("CONFIRMED FLIP",self.messages[1])
        duplicate=v21.monitor_worker(self.store,self.messages.append,nxt)
        self.assertEqual(duplicate["status"],"already_monitored")
        self.assertEqual(len(self.messages),2)

    def test_five_min_strong_low_scans_even_hour_boundary(self):
        prefix="intraday-v3:"+DAY+":"
        current=dict(ticker="ABC",direction="BULLISH",score=78,price=101.0,eligible=True,
                     chase="LOW",choppy=False,late_chase=False)
        with self.store.transaction() as tx:
            tx.put(prefix+"early:low_previous",
                   dict(asof="2026-11-23T14:55:00+00:00",by_ticker={}))
            result=live_low_alert(tx,prefix,[current],int(MON.timestamp())//300,
                                  MON,MON-timedelta(minutes=4),should_send=True)
        self.assertEqual(result["new"],1)
        self.assertTrue(result["queued"])

    def test_hourly_top10_silent_but_scanner_baseline_persists(self):
        from unittest.mock import patch
        from scanner import intraday_v3 as scanner
        opened = datetime(2026, 11, 23, 14, 30, tzinfo=timezone.utc)
        scan_at = datetime(2026, 11, 23, 15, 42, tzinfo=timezone.utc)
        bars = []
        for i in range(14):
            close = 100 + i * .06
            op = close - .02
            bars.append(dict(t=(opened + timedelta(minutes=i * 5)).isoformat(),
                             o=op, h=close+.08, l=op-.08, c=close,
                             v=35000, n=100, vw=(close+op)/2))
        dataset = {"SPY": bars, "ABC": bars}
        session = dict(date=DAY, open="09:30", close="16:00")
        with patch.object(scanner, "calendar", return_value=(session, None)), \
             patch.object(scanner, "pre_config", return_value={"symbols": ["SPY","ABC"]}), \
             patch.object(scanner, "gather", return_value=dataset):
            report = scanner.worker(self.store, self.messages.append, scan_at)
        self.assertEqual(report["status"], "scanned")
        self.assertFalse(report["telegram_queued"])
        with self.store.transaction() as tx:
            self.assertEqual(len(tx.get("intraday-v3:"+DAY+":history", [])), 1)
            self.assertIsNotNone(tx.get("intraday-v3:"+DAY+":latest"))
            record = tx.execute("SELECT status FROM mf_outbox WHERE id=?",
                                ("intraday-v3:"+DAY+":hour:8:watch",)).fetchone()
            self.assertIsNone(record)
        self.assertEqual(self.messages, [])

    def test_five_min_below_75_not_sent(self):
        prefix="intraday-v3:"+DAY+":"
        weak=dict(ticker="ABC",direction="BULLISH",score=74,eligible=True,
                  chase="LOW",choppy=False,late_chase=False)
        with self.store.transaction() as tx:
            tx.put(prefix+"early:low_previous",dict(by_ticker={}))
            result=live_low_alert(tx,prefix,[weak],int(MON.timestamp())//300,
                                  MON,MON-timedelta(minutes=4),should_send=False)
        self.assertEqual(result["new"],0)

if __name__=="__main__":
    unittest.main()
