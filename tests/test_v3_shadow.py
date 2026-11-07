import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
from scanner.v3_shadow.core import Bar, Engine, Reference

ET = ZoneInfo("America/New_York")

def bar(m, op, hi, lo, cl, vol=200):
    return Bar("TEST", datetime(2026, 11, 6, 9, m, tzinfo=ET), op, hi, lo, cl, vol)

REF = Reference(prior_close=125, atr=1, avg_minute_volume=100)

class ShadowChecks(unittest.TestCase):
    def test_first_bar_watch_not_trade(self):
        e = Engine()
        events = e.on_bar(bar(31, 100, 100.2, 98.7, 99), REF)
        self.assertEqual([x["event"] for x in events], ["WATCH"])
        self.assertLess(events[0]["gap_atr_context"], -20)

    def test_followup_provisional_not_trade(self):
        e = Engine()
        e.on_bar(bar(31, 100, 100.2, 98.7, 99), REF)
        r = e.on_bar(bar(32, 99.1, 99.2, 98.65, 98.85), REF)[0]
        self.assertEqual(r["event"], "PROVISIONAL")
        self.assertEqual(r["side"], "BEAR")
        self.assertTrue(r["research_only"])
        self.assertGreater(r["stop_reference"], r["entry_proxy"])

    def test_extension_never_chase(self):
        e = Engine()
        e.on_bar(bar(31, 100, 100.2, 98.7, 99), REF)
        self.assertEqual(e.on_bar(bar(32, 99, 99.0, 98, 98.1), REF)[0]["event"], "MISSED")

    def test_volume_failed(self):
        e = Engine()
        self.assertEqual(e.on_bar(bar(31, 100, 100.2, 98.7, 99, 100), REF), [])

    def test_seed_incomplete(self):
        e = Engine()
        incomplete = Reference(125, 1, 100, False)
        self.assertEqual(e.on_bar(bar(31, 100, 100.2, 98.7, 99), incomplete)[0]["event"], "DATA_HALT")

    def test_gap_halts_no_synthetic_bars(self):
        e = Engine()
        e.on_bar(bar(31, 100, 100.2, 98.7, 99), REF)
        self.assertEqual(e.on_bar(bar(33, 99, 99.3, 98.6, 98.9), REF)[0]["event"], "DATA_HALT")

    def test_no_second_event_or_duplicate(self):
        e = Engine()
        e.on_bar(bar(31, 100, 100.2, 98.7, 99), REF)
        e.on_bar(bar(32, 99.1, 99.2, 98.65, 98.85), REF)
        self.assertEqual(e.on_bar(bar(33, 98.9, 99.1, 98.7, 98.8), REF), [])
        with self.assertRaises(ValueError):
            e.on_bar(bar(33, 98.9, 99.1, 98.7, 98.8), REF)

if __name__ == "__main__":
    unittest.main()
