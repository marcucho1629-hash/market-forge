"""Nov 12 forward-observed 150x5m to retrospective 3m research only."""
import tempfile,unittest
from datetime import datetime,timedelta,timezone
from unittest.mock import patch
from scanner.store import Store
from scanner import day_v32_shadow as x

OPEN=datetime(2026,11,12,14,30,tzinfo=timezone.utc)
CLOSE=OPEN+timedelta(hours=6,minutes=30)
NOW=CLOSE+timedelta(minutes=33)
SESSION={"date":x.DAY,"is_open":True,"open":OPEN.isoformat(),"close":CLOSE.isoformat()}

def low(ticker="NVDA",score=79,momentum=12,side="BULLISH",selected=False):
    return dict(ticker=ticker,direction=side,score=score,eligible=True,
                chase="LOW",points={"momentum":momentum},
                selected=selected,choppy=False,late_chase=False)

def minute_rows(n=70):
    rows=[]
    for i in range(n):
        t=OPEN+timedelta(minutes=i)
        p=100+i*.04
        rows.append(dict(t=t.isoformat(),o=p,h=p+.10,l=p-.04,
                         c=p+.06,v=10000 if i<12 else 20000,
                         vw=p+.03,n=120))
    return rows

class ObservedLedger(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store=Store(tmp.name+"/study.sqlite")
    def test_all_150_pool_strong_low_including_outside_top10(self):
        rows=[low("NVDA",selected=False),low("APP",score=80,selected=True),
              low("AAPL",score=69),low("TSLA",momentum=7),
              dict(low("META"),chase="MED"),
              dict(low("AMD"),choppy=True),
              dict(low("PLTR"),late_chase=True)]
        out=x.capture(self.store,x.DAY,OPEN+timedelta(minutes=37),
                      OPEN+timedelta(minutes=35),"5M_STRONG_ALL",rows)
        self.assertEqual(out,{"status":"captured","count":2})
        ledger=x.read_timeline(self.store)
        self.assertEqual({r["ticker"] for r in ledger[0]["selected"]},{"NVDA","APP"})
        n=next(y for y in ledger[0]["selected"] if y["ticker"]=="NVDA")
        self.assertFalse(n["top10"])
        self.assertEqual(n["chase"],"LOW")
        repeat=x.capture(self.store,x.DAY,OPEN+timedelta(minutes=37),
                         OPEN+timedelta(minutes=35),"5M_STRONG_ALL",rows)
        self.assertEqual(repeat["status"],"duplicate")
    def test_future_timestamp_rejected_and_other_date_no_change(self):
        with self.assertRaises(ValueError):
            x.capture(self.store,x.DAY,OPEN,OPEN+timedelta(minutes=5),"OPEN_RADAR",[])
        self.assertEqual(x.capture(self.store,"2026-11-11",OPEN,OPEN,"OPEN_RADAR",[])["status"],
                         "outside_study")
        self.assertEqual(x.read_timeline(self.store),[])
    def test_preliminary_radar_unknown_is_not_low(self):
        x.capture(self.store,x.DAY,OPEN+timedelta(minutes=7),
                  OPEN+timedelta(minutes=5),"OPEN_RADAR",
                  [dict(ticker="NVDA",direction="BULLISH",eligible=True)])
        event=x.first_seen(x.read_timeline(self.store))[0]
        self.assertEqual(event["chase"],"UNKNOWN")
        self.assertEqual(event["source"],"OPEN_RADAR")
        self.assertFalse(event["top10"])
    def test_first_observed_event_is_preserved_not_backdated(self):
        a=OPEN+timedelta(minutes=37)
        b=OPEN+timedelta(minutes=42)
        x.capture(self.store,x.DAY,a,a-timedelta(minutes=2),
                  "5M_STRONG_ALL",[low()])
        x.capture(self.store,x.DAY,b,b-timedelta(minutes=2),
                  "5M_STRONG_ALL",[low(score=99)])
        events=x.first_seen(x.read_timeline(self.store))
        self.assertEqual(len(events),1)
        self.assertEqual(events[0]["observed_at"],a.isoformat())
        self.assertEqual(events[0]["score"],79)

class Confirmed3m(unittest.TestCase):
    def test_only_complete_native_1m_triplets_create_3m(self):
        bars=minute_rows(12)
        partial=bars[:]
        del partial[4] # destroy 3m bar ending 9:36 ET
        full=x.aggregate_3m(bars,OPEN)
        broken=x.aggregate_3m(partial,OPEN)
        self.assertEqual(len(full),4)
        self.assertEqual(len(broken),3)
        self.assertEqual(full[0]["end"],OPEN+timedelta(minutes=3))
        self.assertNotIn(OPEN+timedelta(minutes=6),
                         [r["end"] for r in broken])
    def test_no_lookahead_at_or_before_observation(self):
        # Complete 3m candle ending 9:36 is never eligible after 9:37 watch.
        event={"ticker":"NVDA","direction":"BULLISH",
               "observed_at":(OPEN+timedelta(minutes=7)).isoformat()}
        entries=[]
        for i in range(5):
            p=100+i*.15
            entries.append(dict(end=OPEN+timedelta(minutes=3*(i+1)),
                                o=p,c=p+.25,h=p+.29,l=p-.04,
                                v=600 if i==4 else 100))
        res=x.try_confirm(event,entries)
        self.assertEqual(res["status"],"3M_PROXY_CONFIRMED",res)
        self.assertEqual(res["time"],(OPEN+timedelta(minutes=15)).isoformat())
        self.assertEqual(res["delay_min"],8)
        later={**event,"observed_at":(OPEN+timedelta(minutes=15)).isoformat()}
        self.assertNotEqual(x.try_confirm(later,entries)["status"],"3M_PROXY_CONFIRMED")
    def test_forward_returns_exact_and_missing_not_filled(self):
        b=minute_rows(45)
        start=OPEN+timedelta(minutes=15)
        confirmation={"time":start.isoformat(),"price":100.0}
        r=x.future_changes(confirmation,b,"BULLISH")
        self.assertEqual(r["5"],round(100*(b[19]["c"]/100-1),3))
        self.assertIsNotNone(r["15"])
        self.assertIsNotNone(r["30"])
        blank=x.future_changes({"time":None,"price":None},b,"BULLISH")
        self.assertEqual(blank,{"5":None,"15":None,"30":None})
        sparse=[v for v in b if v["t"]!=(start+timedelta(minutes=15)-timedelta(minutes=1)).isoformat()]
        r2=x.future_changes(confirmation,sparse,"BULLISH")
        self.assertIsNone(r2["15"])
    def test_replay_not_equivalent_to_v136_or_live_bats(self):
        early={"source":"OPEN_RADAR","ticker":"NVDA","direction":"BULLISH",
               "observed_at":(OPEN+timedelta(minutes=7)).isoformat(),
               "bar_cutoff":(OPEN+timedelta(minutes=5)).isoformat(),
               "score":None,"momentum":None,"chase":"UNKNOWN","top10":False}
        report=x.research([dict(source="OPEN_RADAR",observed_at=early["observed_at"],
            bar_cutoff=early["bar_cutoff"],selected=[early],feed="IEX_5M")],
            {"NVDA":minute_rows(70)},OPEN,CLOSE,NOW)
        self.assertEqual(report["status"],"ready")
        self.assertFalse(report["live_3m_alerts"])
        self.assertFalse(report["live_mc_scmp"])
        self.assertEqual(report["orders"],0)
        self.assertEqual(report["universe_5m"],150)
        self.assertEqual(report["per_source"]["OPEN_RADAR"]["observed"],1)
        self.assertEqual(report["unique_1m_replay_symbols"],1)
    def test_zero_candidates_is_not_fake_provider_failure(self):
        report=x.research([dict(observed_at=(OPEN+timedelta(minutes=37)).isoformat(),
                                bar_cutoff=(OPEN+timedelta(minutes=35)).isoformat(),
                                source="5M_STRONG_ALL",feed="IEX_5M",selected=[])],
                          {},OPEN,CLOSE,NOW)
        self.assertEqual(report["status"],"ready")
        self.assertEqual(report["observed_candidates"],0)
        self.assertEqual(report["per_source"]["5M_STRONG_ALL"]["confirmed"],0)

class Worker(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store=Store(tmp.name+"/run.sqlite")
        self.sent=[]
    def test_once_stores_real_report_and_sends_eod_only(self):
        x.capture(self.store,x.DAY,OPEN+timedelta(minutes=37),
                  OPEN+timedelta(minutes=35),"5M_STRONG_ALL",[low()])
        with patch.object(x,"schedule",return_value=SESSION),patch.object(
             x,"load_1m",return_value={"NVDA":minute_rows(70)}):
            first=x.worker(self.store,self.sent.append,NOW)
            second=x.worker(self.store,self.sent.append,NOW+timedelta(minutes=7))
        self.assertEqual(first["status"],"recorded",first)
        self.assertEqual(first["delivery"],["sent"])
        self.assertFalse(second["created"])
        self.assertEqual(len(self.sent),1)
        self.assertIn("150x5M -> 3M SHADOW",self.sent[0])
        self.assertIn("실시간 알림 아님",self.sent[0])
        with self.store.transaction() as tx:
            report=tx.get(x.PREFIX+x.DAY+":report")
        self.assertEqual(report["observed_candidates"],1)
        self.assertEqual(report["orders"],0)
    def test_no_5m_capture_no_report_and_no_telegram(self):
        with patch.object(x,"schedule",return_value=SESSION):
            result=x.worker(self.store,self.sent.append,NOW)
        self.assertEqual(result["status"],"no_live_5m_observation_no_report")
        self.assertFalse(self.sent)
    def test_disabled_outside_one_day_window(self):
        self.assertFalse(x.window(NOW+timedelta(days=1)))
        self.assertFalse(x.window(NOW-timedelta(minutes=10)))
        self.assertTrue(x.window(NOW))
        self.assertFalse(self.sent)

if __name__=="__main__":unittest.main()
