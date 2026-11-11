"""Swing Scanner Shadow strict, independent candidate diagnostics."""
import math,tempfile,unittest
from datetime import datetime,timedelta,timezone
from unittest.mock import patch
from scanner.store import Store
from scanner import swing_scanner_shadow as s

DAY="2026-11-11"
CLOSE=datetime(2026,11,11,21,0,tzinfo=timezone.utc)
NOW=CLOSE+timedelta(minutes=25)

def daily(symbol="NVDA",side=1,count=90,day=DAY):
    # Main trend with actual countertrend bars to keep RSI out of exhaustion.
    rows=[]
    base=100.
    for i in range(count):
        if i:
            base+=side*([.6,-.35,.25][i%3])
        price=base
        timestamp=(datetime(2026,7,1,4,tzinfo=timezone.utc)+timedelta(days=i))
        rows.append({"t":timestamp.isoformat(),"o":price-.1,"h":price+1.4,
                    "l":price-1.4,"c":price,"v":25000,"vw":price,"n":120})
    # Force dates unique and last day at midnight ET. Other preceding dates
    # need not be market sessions for this pure ranking regression.
    first=datetime(2026,8,14,4,tzinfo=timezone.utc)
    for i,r in enumerate(rows):
        r["t"]=(first+timedelta(days=i)).isoformat()
    # Since count=90 exceeds the DAY range, just anchor last day and unique
    # dates by deriving from its ET midnight back through calendar days.
    last=datetime(2026,11,11,5,tzinfo=timezone.utc)
    for i,r in enumerate(rows):
        r["t"]=(last-timedelta(days=count-1-i)).isoformat()
    return rows

def hourly(symbol="NVDA",side=1,day=DAY):
    rows=[]
    for i in range(12):
        if i<6:
            t=datetime(2026,11,10,14,30,tzinfo=timezone.utc)+timedelta(hours=i)
        else:
            t=datetime(2026,11,11,14,30,tzinfo=timezone.utc)+timedelta(hours=i-6)
        p=110+side*i*.22
        rows.append({"t":t.isoformat(),"o":p-.05,
                    "h":p+1.,"l":p-1.,"c":p,
                    "v":20000,"vw":p,"n":100})
    return rows

class Validate(unittest.TestCase):
    def test_valid_bars_cleaned_and_future_excluded(self):
        r=daily()
        self.assertEqual(len(s.clean(r,DAY,"day",CLOSE)),90)
        r.append({**r[-1],"t":"2026-11-12T05:00:00+00:00"})
        self.assertEqual(len(s.clean(r,DAY,"day",CLOSE)),90)
        r2=daily();r2.append(dict(r2[-1],c=r2[-1]["c"]+0.5))
        self.assertEqual(s.clean(r2,DAY,"day",CLOSE),[])
    def test_h1_ignores_premarket(self):
        r=hourly()
        bad={**r[-1],"t":"2026-11-11T13:00:00+00:00"}
        data=s.clean(r+[bad],DAY,"hour",CLOSE)
        self.assertEqual(len(data),12)
        self.assertEqual(sum(x["_day"]==DAY for x in data),6)
    def test_two_horizons_are_independent_and_nonforced(self):
        d=s.clean(daily(),DAY,"day",CLOSE)
        h=s.clean(hourly(),DAY,"hour",CLOSE)
        spy=s.clean(daily("SPY"),DAY,"day",CLOSE)
        r=s.study("NVDA",d,h,spy,DAY,CLOSE,"2-3","BULLISH")
        self.assertEqual(r["reason"],"qualified")
        self.assertTrue(r["eligible"])
        self.assertGreaterEqual(r["score"],s.LIMITS["2-3"])
        longer=s.study("NVDA",d,h,spy,DAY,CLOSE,"5-10","BULLISH")
        self.assertTrue(longer["eligible"])
        bearish=s.study("NVDA",d,h,spy,DAY,CLOSE,"2-3","BEARISH")
        self.assertEqual(bearish["reason"],"daily_trend_conflict")
    def test_chase_blocks_high_quality_daily(self):
        data=daily()
        for r in data[-8:]:
            r["c"]+=20
            r["h"]+=22
            r["o"]+=20
            r["l"]+=18
        d=s.clean(data,DAY,"day",CLOSE)
        h=s.clean(hourly(),DAY,"hour",CLOSE)
        result=s.study("NVDA",d,h,d,DAY,CLOSE,"2-3","BULLISH")
        self.assertFalse(result["eligible"])
        self.assertIn(result["reason"],("rsi_overextended","late_chase"))
    def test_missing_day_is_not_stale_candidate(self):
        d=s.clean(daily()[:-1],DAY,"day",CLOSE)
        h=s.clean(hourly(),DAY,"hour",CLOSE)
        r=s.study("NVDA",d,h,d,DAY,CLOSE,"2-3","BULLISH")
        self.assertEqual(r["reason"],"daily_missing_or_stale")
    def test_ranking_no_forced_list(self):
        data={"day":{"SPY":daily("SPY"),"NVDA":daily(),"TSLA":[]},"hour":{
               "SPY":hourly("SPY"),"NVDA":hourly(),"TSLA":[]}}
        with patch.object(s,"UNIVERSE",["SPY","NVDA","TSLA"]):
            result=s.rank(data,DAY,CLOSE)
        self.assertGreaterEqual(len(result["picks"]),1)
        self.assertLess(len(result["picks"]),20)
        self.assertNotIn("TSLA",{x["ticker"] for x in result["picks"]})
        self.assertEqual(len(result["rows"]),8) # SPY excluded, 2 tickers x 4 combinations
        self.assertTrue(result["reason_counts"]["daily_missing_or_stale"]>=4)
    def test_dst_exact_and_no_orders(self):
        self.assertTrue(s.scheduled(NOW))
        summer=datetime(2026,7,10,20,25,tzinfo=timezone.utc)
        self.assertTrue(s.scheduled(summer))
        self.assertFalse(s.scheduled(NOW-timedelta(minutes=20)))
        self.assertFalse(s.scheduled(NOW+timedelta(hours=1)))
    def test_worker_idempotent_and_research_only(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db=Store(tmp.name+"/swing.sqlite3")
        sent=[]
        fake={"status":"ready","day":DAY,"asof":NOW.isoformat(),"feed":"IEX",
             "mode":"SWING_RESEARCH_WATCH_ONLY","picks":[],
             "reason_counts":{"daily_trend_conflict":50},
             "universe_count":100,
             "quality":{str(k):{"daily":65,"hourly":7} for k in range(100)},
             "bigdata_calls":0,"orders":0}
        with patch.object(s,"completed_session",return_value=(DAY,CLOSE)),patch.object(s,"analyze",return_value=fake):
            first=s.worker(db,sent.append,NOW)
            second=s.worker(db,sent.append,NOW+timedelta(minutes=1))
        self.assertEqual(first["status"],"recorded")
        self.assertEqual(first["selected"],0)
        self.assertEqual(first["delivery"],["sent"])
        self.assertFalse(second["created"])
        self.assertEqual(len(sent),1)
        self.assertIn("진입 신호 아님",sent[0])
        self.assertNotIn("MC",sent[0])
        with db.transaction() as tx:
            self.assertEqual(tx.get(s.PREFIX+DAY+":report")["orders"],0)

if __name__=="__main__":unittest.main()
