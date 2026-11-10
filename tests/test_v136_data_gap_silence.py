"""Regression tests: data-gap diagnostics stay active, Telegram remains quiet."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from scanner.store import Store
from scanner.v136_live import runtime as day

OPEN = datetime(2026, 10, 23, 13, 30, tzinfo=timezone.utc)


def calendar(_day):
    return {
        "is_open": True,
        "open": OPEN.isoformat(),
        "close": (OPEN + timedelta(hours=6, minutes=30)).isoformat()
    }


def candle(minute, volume=100):
    end = OPEN + timedelta(minutes=minute)
    return ["TSLA", int(end.timestamp() * 1000), 368., 369., 367., 368., volume]


class DayGapSilence(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Store(tmp.name + "/journal.sqlite3")
        day.control(self.store, True, OPEN)
        day.delivery(self.store, lambda _text: None, OPEN)

    def feed(self, rows, minute):
        now = OPEN + timedelta(minutes=minute, seconds=2)
        return day.ingest(
            self.store,
            {"source": day.VERSION, "bank": 1,
             "emitted": int(now.timestamp() * 1000), "bars": rows},
            now, calendar
        )

    def test_gap_remains_diagnosed_but_not_messaged(self):
        self.feed([candle(2)], 2)
        with self.store.transaction() as tx:
            state = tx.get(day.PREFIX + "2026-10-23:TSLA")
            self.assertEqual(state["data_quality"], "partial_minutes")
            self.assertTrue(state["gaps"])
            self.assertIsNotNone(state["missing_end"])
            self.assertEqual(state["observed_gaps"], 1)
            matching = tx.execute(
                "SELECT id FROM mf_outbox WHERE id LIKE ?",
                (day.PREFIX + "%:DATA_GAP:%",)
            ).fetchall()
            self.assertEqual(matching, [])
        sent = []
        day.delivery(self.store, sent.append, OPEN + timedelta(minutes=2, seconds=3))
        self.assertEqual(sent, [])

    def test_old_queued_gap_is_expired_not_sent_or_deleted(self):
        identity = day.PREFIX + "2026-10-23:TSLA:DATA_GAP:123456"
        with self.store.transaction() as tx:
            tx.enqueue(identity, {
                "text": "DAY TEST TSLA 데이터 누락 감지",
                "expires_at": (OPEN + timedelta(hours=2)).isoformat()
            })
        sent = []
        day.delivery(self.store, sent.append, OPEN + timedelta(minutes=2))
        self.assertEqual(sent, [])
        with self.store.transaction() as tx:
            row = tx.execute(
                "SELECT status FROM mf_outbox WHERE id=?", (identity,)
            ).fetchone()
            self.assertEqual(row[0], "expired")

    def test_data_conflict_warning_is_not_suppressed(self):
        self.feed([candle(1)], 1)
        revised = candle(1, volume=101)
        self.feed([revised, candle(2)], 2)
        with self.store.transaction() as tx:
            state = tx.get(day.PREFIX + "2026-10-23:TSLA")
            self.assertIn("halted", state)
        # The test fixture uses an October timestamp; the real drain clock is
        # later, so assert that the critical warning is still queued (not silenced).
        with self.store.transaction() as tx:
            queued = tx.execute(
                "SELECT id,status,payload FROM mf_outbox WHERE id LIKE ?",
                (day.PREFIX + "%:DATA_CONFLICT",)
            ).fetchall()
            self.assertEqual(len(queued), 1)
            self.assertEqual(queued[0][1], "pending")
            self.assertIn("확정봉 충돌", json.loads(queued[0][2])["text"])


if __name__ == "__main__":
    unittest.main()
