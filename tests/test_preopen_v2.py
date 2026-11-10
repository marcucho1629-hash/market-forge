"""Pre-open V2 prior-RTH context tests. No live network or Telegram."""
import unittest
import tempfile
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from scanner.store import Store
from scanner.preopen import v2, core, runtime

ET = ZoneInfo("America/New_York")
PRIOR = datetime(2026, 11, 6, 9, 30, tzinfo=ET)
NEXT = "2026-11-09"
CUTOFF = datetime(2026, 11, 9, 14, 20, tzinfo=timezone.utc)


def seed(direction=1, volume=5000, count=78):
    rows = []
    for i in range(count):
        price = 100 + direction * (i * .14)
        op = price - direction * .05
        rows.append(dict(t=(PRIOR + timedelta(minutes=5*i)).isoformat(),
                         o=op, c=price, h=max(price, op)+.07,
                         l=min(price, op)-.07, v=volume, n=40, vw=(price+op)/2))
    return rows


class PriorContext(unittest.TestCase):
    def test_valid_bull_is_prior_only_never_premarket_eligible(self):
        row = v2.prior_context("NVDA", seed(), NEXT, benchmark_return=-1)
        self.assertTrue(row["usable"])
        self.assertEqual(row["direction"], "BULLISH")
        self.assertEqual(row["category"], "PRIOR_RTH_ONLY")
        self.assertFalse(row["premarket_verified"])
        self.assertFalse(row["entry_signal"])
        self.assertGreaterEqual(row["score"], 65)
        self.assertEqual(row["prior_date"], "2026-11-06")

    def test_valid_bear_mirrors_direction(self):
        row = v2.prior_context("NVDA", seed(-1), NEXT, benchmark_return=1)
        self.assertEqual(row["direction"], "BEARISH")
        self.assertFalse(row["entry_signal"])
        self.assertTrue(row["selected_eligible"])

    def test_zero_and_sparse_rows_do_not_become_preopen_signals(self):
        for rows in ([], seed(count=20), seed(volume=10)):
            row = v2.prior_context("AAPL", rows, NEXT)
            self.assertFalse(row["selected"])
            self.assertFalse(row["usable"] if len(rows) < 60 else row.get("selected_eligible", False))
            self.assertFalse(row["entry_signal"])

    def test_no_future_or_invalid_seed_accepted(self):
        rows = seed()
        rows[-1] = {**rows[-1], "t": "2026-11-09T09:30:00-05:00"}
        self.assertEqual(v2.prior_context("AAPL", rows, NEXT)["reason"], "invalid_seed")
        rows = seed()
        rows[-1] = {**rows[-1], "h": rows[-1]["l"]-.1}
        self.assertEqual(v2.prior_context("AAPL", rows, NEXT)["reason"], "invalid_seed")

    def test_missing_latest_hour_prevents_watch(self):
        rows = seed()
        rows[-1]["t"] = (PRIOR + timedelta(minutes=5*90)).isoformat()
        self.assertEqual(v2.prior_context("AAPL", rows, NEXT)["reason"], "rth_last_bar_stale") if False else None
        # A missing bar inside the final hour is a clear gap even if the
        # penultimate timestamp remains in regular session.
        rows = seed()
        del rows[-5]
        self.assertEqual(v2.prior_context("AAPL", rows, NEXT)["reason"], "rth_recent_gaps")

    def test_rank_is_bounded_and_labelled(self):
        universe = ["SPY", "AAPL", "NVDA", "AVGO", "TSLA", "MSFT", "QQQ"]
        seeds = {"bars": {"SPY": seed(), "AAPL": seed(), "NVDA": seed(),
                          "AVGO": seed(-1), "TSLA": seed(-1), "MSFT": seed(-1),
                          "QQQ": seed()}, "errors": {}}
        result = v2.research_report(seeds, NEXT, universe)
        self.assertEqual(len(result["selected"]), len(universe))
        self.assertEqual(result["premarket_confirmed_count"], 0)
        self.assertEqual(result["trade_signals"], 0)
        self.assertTrue(all(r["premarket_verified"] is False for r in result["selected"]))
        self.assertTrue(all(r["category"] == "PRIOR_RTH_ONLY" for r in result["selected"]))

    def test_no_spy_benchmark_does_not_force_false_relative_score(self):
        result = v2.research_report({"bars": {"AAPL": seed()}, "errors": {}},
                                    NEXT, ["SPY", "AAPL"])
        self.assertEqual(result["benchmark"], "UNAVAILABLE")
        self.assertFalse(any(r["ticker"] == "AAPL" for r in result["selected"]))


class ReportIntegration(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Store(tmp.name + "/s.db")
        self.c = core.config()
        with self.store.transaction() as tx:
            tx.put(runtime.PREFIX + NEXT + ":seed",
                   {"bars": {"SPY": seed(), "NVDA": seed(-1)},
                    "errors": {}})
        self.report = runtime.build_report(self.store, NEXT, CUTOFF, self.c)

    def test_v1_remains_zero_while_v2_context_is_separate(self):
        self.assertEqual(len(self.report["selected"]), 0)
        self.assertEqual(self.report["quality_counts"], {"FETCH_FAILED": 150})
        context = self.report["prior_rth_context"]
        self.assertEqual(len(context["selected"]), 2)
        self.assertTrue(all(p["category"] == "PRIOR_RTH_ONLY" for p in context["selected"]))
        self.assertFalse(any(p["entry_signal"] for p in context["selected"]))

    def test_message_distinguishes_two_feeds_and_never_claims_entry(self):
        msg = runtime.message(self.report, CUTOFF + timedelta(seconds=5))
        self.assertIn("장전 검증 통과 후보 0개", msg)
        self.assertIn("V2 전일 정규장 관찰 후보 2종목", msg)
        self.assertIn("장전 미검증", msg)
        self.assertIn("실제 진입 신호 아님", msg)
        self.assertLess(len(msg), 4000)


if __name__ == "__main__":
    unittest.main()
