"""Regression Nov16 Early FLIP/Freshness Shadow; live Pine/Top10 unchanged."""
import tempfile,unittest
from datetime import datetime,timedelta,timezone
from unittest.mock import patch
from scanner.store import Store
from scanner import day_v34_early_flip_freshness as x
from scanner import opening_radar_0735 as radar

OPEN=datetime(2026,11,16,14,30,tzinfo=timezone.utc)
CLOSE=OPEN+timedelta(hours=6,minutes=30)
NOW=CLOSE+timedelta(minutes=52)
SESSION=dict(date=x.DAY,is_open=True,open=OPEN.isoformat(),close=CLOSE.isoformat())

def bar(i,o,h,l,c,v=10000):
    return dict(t=(OPEN+timedelta(minutes=5*i)).isoformat(),
        o=o,h=h,l=l,c=c,v=v,vw=(o+c)/2,n=100)

def market():
    return {
      "SPY":[bar(0,760,761,757,758),bar(1,758,759,756,757)],
      "SNDK":[bar(0,915,940,914,922),bar(1,924,925,896,903)],
      "NVDA":[bar(0,170,170,164,165),bar(1,165,166,162,163)],
      "AMD":[bar(0,566,567,560,563),bar(1,563,564,556,559)],
    }

def low(ticker="NVDA",side="BEARISH",selected=False,price=160,session_atr=2):
    return dict(ticker=ticker,direction=side,quality="READY",reason="qualified",
        eligible=True,score=80,points={"momentum":12},chase="LOW",choppy=False,
        late_chase=False,selected=selected,price=price,session_extension_atr=session_atr,
        extension_ema_atr=.5)

class Opening(unittest.TestCase):
    def test_opening_flip_rejected_by_legacy_is_detected_as_shadow_only(self):
        results=x.opening_rows(market(),OPEN,OPEN+timedelta(minutes=10))
        by={q["ticker"]:q for q in results}
        self.assertEqual(by["SNDK"]["state"],"EARLY_FLIP_WATCH")
        self.assertTrue(by["SNDK"]["first_two_flip"])
        self.assertEqual(by["SNDK"]["side"],"BEARISH")
        self.assertEqual(by["NVDA"]["state"],"EXTENDED_NO_ENTRY")
        self.assertTrue(by["NVDA"]["extended"])
        # A mixed bar candidate is blocked by production opening radar.
        radar_result=radar.evaluate("SNDK",market()["SNDK"],market()["SPY"],OPEN,
                                    OPEN+timedelta(minutes=10))
        self.assertFalse(radar_result["eligible"])
        self.assertEqual(radar_result["reason"],"mixed_bars")
    def test_first_bar_big_move_is_watch_no_entry(self):
        r=x.opening_rows(market(),OPEN,OPEN+timedelta(minutes=5))
        by={z["ticker"]:z for z in r}
        self.assertEqual(by["NVDA"]["state"],"EXTENDED_NO_ENTRY")
        self.assertFalse(by["SNDK"]["first_two_flip"])
        self.assertEqual(by["SNDK"]["state"],"EARLY_UNCONFIRMED")
    def test_missing_bar_does_not_synthesize_flip(self):
        m=market()
        m["SNDK"]=[m["SNDK"][0]]
        rows=x.opening_rows(m,OPEN,OPEN+timedelta(minutes=10))
        self.assertNotIn("SNDK",{r["ticker"] for r in rows})
    def test_strong_low_eligibility_not_relaxed(self):
        valid=low()
        self.assertTrue(x.five_rows([valid])[0]["strong_low"])
        for modified in (dict(valid,chase="MED"),dict(valid,score=65),
                         dict(valid,choppy=True),dict(valid,late_chase=True),
                         dict(valid,points={"momentum":7})):
            self.assertFalse(x.five_rows([modified])[0]["strong_low"])

class Ledger(unittest.TestCase):
    def setUp(self):
        f=tempfile.TemporaryDirectory()
        self.addCleanup(f.cleanup)
        self.db=Store(f.name+"/v34.sqlite")
    def test_open_records_all_raw_even_rejected_no_live_alert(self):
        at=OPEN+timedelta(minutes=12)
        a=x.capture(self.db,x.DAY,at,OPEN+timedelta(minutes=10),
                    "OPEN_RAW",market(),opened=OPEN)
        self.assertEqual(a["status"],"captured")
        self.assertEqual(a["observed"],3)
        self.assertEqual(x.capture(self.db,x.DAY,at,OPEN+timedelta(minutes=10),
                           "OPEN_RAW",market(),opened=OPEN)["status"],"duplicate")
        timeline=x.timeline(self.db)
        self.assertEqual(len(timeline),1)
        self.assertEqual(timeline[0]["live_alerts"],0)
        self.assertEqual(timeline[0]["orders"],0)
        self.assertEqual({r["ticker"] for r in timeline[0]["rows"]},
                         {"SNDK","AMD","NVDA"})
    def test_date_and_incomplete_cutoff_are_rejected(self):
        at=OPEN+timedelta(minutes=37)
        self.assertEqual(x.capture(self.db,"2026-11-13",at,OPEN,"FIVE_MIN",[])["status"],
                         "outside_study")
        with self.assertRaises(ValueError):
            x.capture(self.db,x.DAY,at,at+timedelta(minutes=5),"FIVE_MIN",[])
        with self.assertRaises(ValueError):
            x.capture(self.db,x.DAY,at,at-timedelta(minutes=1),"FIVE_MIN",[])
        with self.assertRaises(ValueError):
            x.capture(self.db,x.DAY,at-timedelta(days=1),
                      OPEN-timedelta(days=1),"FIVE_MIN",[])
        self.assertEqual(x.timeline(self.db),[])
    def test_hourly_freshness_is_not_naively_called_new_momentum(self):
        t0=OPEN+timedelta(minutes=32)
        x.capture(self.db,x.DAY,t0,OPEN+timedelta(minutes=30),
                  "FIVE_MIN",[low(price=160)],hourly=True)
        t1=t0+timedelta(minutes=15)
        x.capture(self.db,x.DAY,t1,OPEN+timedelta(minutes=45),
                  "FIVE_MIN",[low(price=159)],hourly=False)
        t2=t0+timedelta(minutes=60)
        x.capture(self.db,x.DAY,t2,OPEN+timedelta(minutes=90),
                  "FIVE_MIN",[low(price=158,selected=True,session_atr=7)],hourly=True)
        summary=x.summary(x.timeline(self.db),NOW)
        self.assertEqual(summary["status"],"ready")
        self.assertEqual(summary["first_strong_low_count"],1)
        self.assertEqual(summary["first_hourly_count"],1)
        self.assertEqual(summary["late_hourly_count"],1)
        self.assertEqual(summary["forward_15m_covered"],1)
        self.assertEqual(summary["forward_15m_favorable"],1)
    def test_one_eod_telegram_no_report_without_actual_timeline(self):
        sent=[]
        with patch.object(x,"schedule",return_value=SESSION):
            self.assertEqual(x.worker(self.db,sent.append,NOW)["status"],
                             "no_live_observations_no_report")
        self.assertEqual(sent,[])
        t=OPEN+timedelta(minutes=37)
        x.capture(self.db,x.DAY,t,OPEN+timedelta(minutes=35),
                  "FIVE_MIN",[low(price=160)],hourly=True)
        with patch.object(x,"schedule",return_value=SESSION):
            first=x.worker(self.db,sent.append,NOW)
            repeat=x.worker(self.db,sent.append,NOW+timedelta(minutes=1))
        self.assertEqual(first["status"],"recorded",first)
        self.assertEqual(first["delivery"],["sent"])
        self.assertFalse(repeat["created"])
        self.assertEqual(len(sent),1)
        self.assertIn("기존 DAY 매매/알림/청산 변경 없음",sent[0])
        self.assertIn("EXTENDED WATCH",sent[0])
    def test_only_monday_post_close(self):
        self.assertTrue(x.window(NOW))
        self.assertFalse(x.window(NOW-timedelta(minutes=15)))
        self.assertFalse(x.window(NOW+timedelta(days=1)))
        self.assertFalse(x.window(NOW-timedelta(days=3)))

class Observer(unittest.TestCase):
    def test_optional_hook_error_does_not_block_radar(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        db=Store(temp.name+"/r.sqlite")
        sent=[]
        def gather(symbols,start,end):
            return {s:market().get(s,[])[:1] for s in symbols}
        def fails(raw):
            raise RuntimeError("research_write_unavailable")
        r=radar.worker(db,sent.append,OPEN+timedelta(minutes=7),
                       OPEN,OPEN+timedelta(minutes=5),x.DAY,
                       "intraday-v3:"+x.DAY+":",["SPY","NVDA","SNDK"],gather,
                       observer=fails)
        self.assertEqual(r["status"],"radar_scanned")
        self.assertEqual(r["research_observer"],"research_capture_failed")

if __name__=="__main__":unittest.main()
