"""Regression coverage for 05:30 Mountain early-options WATCH scanner."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

from scanner import early_options_0530 as scanner


DENVER=ZoneInfo("America/Denver")


def mt(day,hour,minute,second=3):
    return datetime.fromisoformat(day).replace(tzinfo=DENVER,hour=hour,
        minute=minute,second=second).astimezone(timezone.utc)


def one_minute(start,opens,vol=2000):
    result=[]
    for i,o in enumerate(opens):
        c=opens[i+1] if i+1<len(opens) else o+(opens[i]-opens[i-1] if i else 0)
        top=max(o,c)+.05
        bottom=min(o,c)-.05
        result.append(dict(t=(start+timedelta(minutes=i)).isoformat(),
            o=o,h=top,l=bottom,c=c,v=vol,n=10,vw=(o+c)/2))
    return result


class EarlyOptions0530Test(unittest.TestCase):
    def test_timezone_windows_winter_summer_and_slots(self):
        for day in ("2026-11-24","2026-07-07"):
            self.assertIsNone(scanner.slot(mt(day,5,29)))
            self.assertEqual(scanner.slot(mt(day,5,30))[0],day)
            self.assertEqual(scanner.slot(mt(day,5,35))[0],day)
            self.assertIsNone(scanner.slot(mt(day,5,33)))
            self.assertEqual(scanner.slot(mt(day,7,25))[0],day)
            self.assertIsNone(scanner.slot(mt(day,7,30)))
        self.assertIsNone(scanner.slot(mt("2026-11-28",5,35)))

    def test_consecutive_does_not_fill_gaps_or_use_future_bar(self):
        start=mt("2026-11-24",5,30,0)
        cutoff=start+timedelta(minutes=5)
        bars=one_minute(start,[700,700,700,700,700])
        self.assertEqual(len(scanner.consecutive(bars,1,start,cutoff,5)),5)
        self.assertIsNone(scanner.consecutive(bars[:-1],1,start,cutoff,5))
        self.assertIsNone(scanner.consecutive(bars[:2]+bars[3:],1,start,cutoff,5))
        future=one_minute(cutoff,[701])
        self.assertEqual(len(scanner.consecutive(bars+future,1,start,cutoff,5)),5)

    def test_initial_5min_preliminary_watch_not_option_trade(self):
        start=mt("2026-11-24",5,30,0)
        cutoff=start+timedelta(minutes=5)
        spy=one_minute(start,[700]*5)
        nvda=one_minute(start,[100,100.4,100.8,101.2,101.6])
        choices,valid=scanner.candidates({"SPY":spy,"NVDA":nvda},{},start,cutoff)
        self.assertEqual(valid,2)
        qualified=[p for p in choices if p["ticker"]=="NVDA"]
        self.assertEqual(len(qualified),1)
        self.assertEqual(qualified[0]["state"],"EARLY_MOMENTUM_PRELIM")
        self.assertIsNone(qualified[0]["score"])

    def test_unavailable_spy_fails_closed(self):
        start=mt("2026-11-24",5,30,0)
        cutoff=start+timedelta(minutes=5)
        choices,valid=scanner.candidates({"NVDA":one_minute(start,[100]*5)},
                                          {},start,cutoff)
        self.assertIsNone(choices)
        self.assertEqual(valid,1)

    def test_sip_required_not_iex(self):
        def fake_api(path,params):
            self.assertEqual(params["feed"],"sip")
            self.assertEqual(params["timeframe"],"1Min")
            return {"bars":{"SPY":[]},"next_page_token":None}
        with patch.object(scanner,"api",side_effect=fake_api):
            result=scanner.sip_bars(["SPY"],"1Min",
                                    mt("2026-11-24",5,30),
                                    mt("2026-11-24",5,35))
        self.assertEqual(result,{"SPY":[]})


if __name__=="__main__":
    unittest.main()
