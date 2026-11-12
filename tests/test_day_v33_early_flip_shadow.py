"""Strict research-only Nov13 as-of Early/FLIP/Profit Protection checks."""
import tempfile,unittest
from datetime import datetime,timedelta,timezone
from unittest.mock import patch
from scanner.store import Store
from scanner import day_v33_early_flip_shadow as x

OPEN=datetime(2026,11,13,14,30,tzinfo=timezone.utc)
CLOSE=OPEN+timedelta(hours=6,minutes=30)
NOW=CLOSE+timedelta(minutes=52)
SESSION=dict(date=x.DAY,is_open=True,open=OPEN.isoformat(),close=CLOSE.isoformat())

def strong(ticker="NVDA",direction="BULLISH",score=82,momentum=11,
           eligible=True,choppy=False,selected=False,price=100):
    return dict(ticker=ticker,direction=direction,quality="READY",
        score=score,eligible=eligible,chase="LOW",choppy=choppy,
        late_chase=False,points={"momentum":momentum},
        price=price,vwap=99.9 if direction=="BULLISH" else 100.1,
        reason="qualified",selected=selected)

def minutes(n=80,gap=None):
    values=[]
    for i in range(n):
        if i==gap:continue
        p=100+i*0.02
        values.append(dict(t=(OPEN+timedelta(minutes=i)).isoformat(),
            o=p,h=p+.07,l=p-.03,c=p+.04,v=10000,n=50,vw=p+.02))
    return values

class Ledger(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store=Store(tmp.name+"/study.db")
    def sample(self,at,source,picks):
        return x.capture(self.store,x.DAY,at,at-timedelta(minutes=2),source,picks)
    def test_five_min_all_rows_and_duplicate(self):
        at=OPEN+timedelta(minutes=37)
        one=self.sample(at,"FIVE_MIN",[strong(),strong("AAPL",score=60,momentum=3,eligible=False),
                                         dict(strong("TSLA"),choppy=True)])
        self.assertEqual(one,{"status":"captured","count":3})
        self.assertEqual(self.sample(at,"FIVE_MIN",[strong(score=99)])["status"],"duplicate")
        rows=x.timeline(self.store)
        self.assertEqual(len(rows),1)
        q={r["ticker"]:r for r in rows[0]["rows"]}
        self.assertTrue(q["NVDA"]["eligible_strong"])
        self.assertFalse(q["AAPL"]["eligible_strong"])
        self.assertFalse(q["TSLA"]["eligible_strong"])
        self.assertEqual(q["NVDA"]["price"],100)
    def test_only_friday_target_and_no_future_bars(self):
        at=OPEN+timedelta(minutes=37)
        self.assertEqual(x.capture(self.store,"2026-11-12",at,at,"FIVE_MIN",[])["status"],"outside_study")
        with self.assertRaises(ValueError):
            x.capture(self.store,x.DAY,at,at+timedelta(minutes=5),"FIVE_MIN",[])
        self.assertEqual(x.timeline(self.store),[])
    def test_early_radar_chase_is_unknown_not_low(self):
        at=OPEN+timedelta(minutes=7)
        self.sample(at,"OPEN_RADAR",[dict(ticker="NVDA",direction="BULLISH",
                                         eligible=True,price=100,chase="LOW")])
        self.sample(at+timedelta(minutes=10),"OPEN_EARLY",[
            dict(ticker="NVDA",direction="BULLISH",eligible=True,price=100.5,chase="LOW")])
        early,strong5,five=x.first_appearances(x.timeline(self.store))
        self.assertEqual(early[("OPEN_RADAR","NVDA","BULLISH")]["chase"],"UNKNOWN")
        self.assertEqual(len(early),2)
        self.assertEqual(strong5,{})
    def test_weakness_before_confirmed_opposite_strong(self):
        at=OPEN+timedelta(minutes=37)
        self.sample(at,"FIVE_MIN",[strong()])
        self.sample(at+timedelta(minutes=5),"FIVE_MIN",[
            dict(strong(score=50,momentum=3,eligible=False),reason="score_below_minimum")])
        self.sample(at+timedelta(minutes=10),"FIVE_MIN",[
            strong(direction="BEARISH",price=98)])
        early,strongs,five=x.first_appearances(x.timeline(self.store))
        weak,flip=x.transitions(strongs,five)
        self.assertEqual(weak["NVDA"]["reason"],"WEAK_MOMENTUM_OR_SCORE")
        self.assertEqual(flip["NVDA"]["to_side"],"BEARISH")
        self.assertLess(weak["NVDA"]["observed_at"],flip["NVDA"]["observed_at"])
        self.assertEqual(len(strongs),1)
    def test_choppy_warns_but_does_not_create_flip(self):
        at=OPEN+timedelta(minutes=37)
        self.sample(at,"FIVE_MIN",[strong()])
        self.sample(at+timedelta(minutes=5),"FIVE_MIN",[
            dict(strong(eligible=False,choppy=True),quality="INSUFFICIENT",reason="choppy_chart")])
        _,s,f=x.first_appearances(x.timeline(self.store))
        weak,flip=x.transitions(s,f)
        self.assertEqual(weak["NVDA"]["reason"],"CHOPPY")
        self.assertEqual(flip,{})

class Replay(unittest.TestCase):
    def test_next_minute_reference_not_stale_five_close(self):
        at=OPEN+timedelta(minutes=20,seconds=30)
        result=x.prices_after(at.isoformat(),"BULLISH",50,minutes(80))
        self.assertEqual(result["reference_at"],(OPEN+timedelta(minutes=21)).isoformat())
        self.assertAlmostEqual(result["reference_1m_open"],minutes(80)[21]["o"])
        self.assertEqual(result["covered_minutes"],30)
        self.assertIsNotNone(result["giveback_30_pct"])
        self.assertNotEqual(result["reference_1m_open"],50)
    def test_gap_first_minute_or_missing_exact_future_fails_closed(self):
        at=OPEN+timedelta(minutes=20,seconds=30)
        missing=x.prices_after(at.isoformat(),"BULLISH",100,minutes(80,gap=21))
        self.assertIsNone(missing["reference_1m_open"])
        gap=x.prices_after(at.isoformat(),"BULLISH",100,minutes(80,gap=31))
        self.assertIsNone(gap["signed_close_30_pct"])
        self.assertIsNone(gap["giveback_30_pct"])
    def test_bearish_giveback_direction_sign(self):
        at=OPEN+timedelta(minutes=20,seconds=30)
        a=x.prices_after(at.isoformat(),"BULLISH",100,minutes())
        b=x.prices_after(at.isoformat(),"BEARISH",100,minutes())
        self.assertGreater(a["mfe_30_pct"],b["mfe_30_pct"])
        self.assertLess(b["signed_close_30_pct"],0)
    def test_report_early_lead_and_3m_proxy_hindsight_label(self):
        at=OPEN+timedelta(minutes=7)
        entries=[
          dict(source="OPEN_RADAR",observed_at=at.isoformat(),bar_cutoff=(at-timedelta(minutes=2)).isoformat(),
               feed="IEX_5M",rows=[dict(ticker="NVDA",direction="BULLISH",chase="UNKNOWN")]),
          dict(source="FIVE_MIN",observed_at=(at+timedelta(minutes=30)).isoformat(),
               bar_cutoff=(at+timedelta(minutes=28)).isoformat(),feed="IEX_5M",
               rows=[dict(ticker="NVDA",direction="BULLISH",score=80,price=100,
                   eligible_strong=True,ready=True,chase="LOW",momentum=10,top10=False)]),
        ]
        report=x.analyze(entries,{"NVDA":minutes(80)},OPEN,CLOSE,NOW)
        self.assertEqual(report["status"],"ready")
        self.assertEqual(report["early_lead_count"],1)
        self.assertEqual(report["early_same_direction_leads"][0]["lead_min"],30)
        self.assertEqual(report["orders"],0)
        self.assertEqual(report["new_live_entries"],0)
        self.assertEqual(report["new_live_exits"],0)
        self.assertEqual(report["telegram_during_session"],0)
        self.assertEqual(report["first_5m_strong"],1)

class Cron(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store=Store(tmp.name+"/worker.db")
        self.sent=[]
    def test_after_close_report_once_and_no_intraday_messages(self):
        at=OPEN+timedelta(minutes=37)
        x.capture(self.store,x.DAY,at,at-timedelta(minutes=2),"FIVE_MIN",[strong()])
        with patch.object(x,"schedule",return_value=SESSION),patch.object(
                x,"load_native",return_value={"NVDA":minutes(80)}):
            a=x.worker(self.store,self.sent.append,NOW)
            b=x.worker(self.store,self.sent.append,NOW+timedelta(minutes=1))
        self.assertEqual(a["status"],"recorded",a)
        self.assertEqual(a["delivery"],["sent"])
        self.assertFalse(b["created"])
        self.assertEqual(len(self.sent),1)
        self.assertIn("EARLY + FLIP + GIVEBACK SHADOW",self.sent[0])
        self.assertIn("기존 MC/SCMP/X/SX/HX 변경 없음",self.sent[0])
    def test_no_record_never_sends_success(self):
        with patch.object(x,"schedule",return_value=SESSION):
            self.assertEqual(x.worker(self.store,self.sent.append,NOW)["status"],
                             "no_observed_5m_timeline")
        self.assertEqual(self.sent,[])
    def test_not_today_or_not_after_close(self):
        self.assertFalse(x.window(NOW-timedelta(minutes=20)))
        self.assertFalse(x.window(NOW+timedelta(days=1)))
        self.assertTrue(x.window(NOW))
        self.assertEqual(x.worker(self.store,self.sent.append,NOW-timedelta(minutes=20))["status"],
                         "outside_1452_friday")

if __name__=="__main__":unittest.main()
