"""Keep data gap safety and critical alerts; silence noisy DAY Telegram gaps."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from scanner.store import Store
from scanner.v136_live import runtime as day

OPEN=datetime(2026,10,23,13,30,tzinfo=timezone.utc)

def calendar(_day):
    return dict(is_open=True,open=OPEN.isoformat(),
                close=(OPEN+timedelta(hours=6,minutes=30)).isoformat())

def candle(minute,volume=100):
    t=OPEN+timedelta(minutes=minute)
    return ["TSLA",int(t.timestamp()*1000),368.,369.,367.,368.,volume]

class GapTelegramSafety(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store=Store(tmp.name+"/outbox.sqlite3")
        day.control(self.store,True,OPEN)
        day.delivery(self.store,lambda _message:None,OPEN)

    def feed(self,rows,minute):
        when=OPEN+timedelta(minutes=minute,seconds=2)
        return day.ingest(self.store,dict(source=day.VERSION,bank=1,
                                          emitted=int(when.timestamp()*1000),
                                          bars=rows),when,calendar)

    def test_gap_remains_detected_but_no_gap_telegram_queued(self):
        self.feed([candle(2)],2)
        with self.store.transaction() as tx:
            state=tx.get(day.PREFIX+"2026-10-23:TSLA")
            self.assertEqual(state["data_quality"],"partial_minutes")
            self.assertTrue(state["gaps"])
            self.assertIsNotNone(state["missing_end"])
            self.assertEqual(state["observed_gaps"],1)
            self.assertEqual(state["processed"],0)
            self.assertEqual(tx.execute("SELECT COUNT(*) FROM mf_outbox WHERE id LIKE ?",
                                        (day.PREFIX+"%:DATA_GAP:%",)).fetchone()[0],0)
        self.feed([candle(3)],3)
        with self.store.transaction() as tx:
            state=tx.get(day.PREFIX+"2026-10-23:TSLA")
            self.assertEqual(state["observed_gaps"],1)
            self.assertEqual(state["data_quality"],"partial_minutes")
            self.assertEqual(tx.execute("SELECT COUNT(*) FROM mf_outbox WHERE id LIKE ?",
                                        (day.PREFIX+"%:DATA_GAP:%",)).fetchone()[0],0)

    def test_existing_pending_gap_expires_without_telegram_and_keeps_audit(self):
        ident=day.PREFIX+"2026-10-23:TSLA:DATA_GAP:123456"
        with self.store.transaction() as tx:
            tx.enqueue(ident,dict(text="DAY TEST TSLA 데이터 누락 감지",
                                  expires_at=(datetime.now(timezone.utc)+timedelta(hours=2)).isoformat()))
        sent=[]
        result=day.delivery(self.store,sent.append,datetime.now(timezone.utc))
        self.assertFalse(sent)
        with self.store.transaction() as tx:
            self.assertEqual(tx.execute("SELECT status FROM mf_outbox WHERE id=?",(ident,)).fetchone()[0],
                             "expired")
        self.assertFalse(any(x["id"]==ident and x["status"]=="sent" for x in result))

    def test_critical_data_conflict_still_queued_and_blocks_entry(self):
        self.feed([candle(1)],1)
        self.feed([candle(1,volume=101),candle(2)],2)
        with self.store.transaction() as tx:
            state=tx.get(day.PREFIX+"2026-10-23:TSLA")
            self.assertIn("halted",state)
            conflicts=tx.execute("SELECT status,payload FROM mf_outbox WHERE id LIKE ?",
                                 (day.PREFIX+"%:DATA_CONFLICT",)).fetchall()
            self.assertEqual(len(conflicts),1)
            self.assertEqual(conflicts[0][0],"pending")
            self.assertIn("확정봉 충돌",json.loads(conflicts[0][1])["text"])
            gaps=tx.execute("SELECT id FROM mf_outbox WHERE id LIKE ?",
                            (day.PREFIX+"%:DATA_GAP:%",)).fetchall()
            self.assertEqual(gaps,[])

    def test_gap_recovery_is_unchanged(self):
        self.feed([candle(2)],2)
        self.feed([candle(1),candle(2)],2)
        with self.store.transaction() as tx:
            state=tx.get(day.PREFIX+"2026-10-23:TSLA")
            self.assertEqual(state["processed"],candle(2)[1])
            self.assertEqual(state["observed_gaps"],0)
            self.assertEqual(state["data_quality"],"complete")

if __name__=="__main__":
    unittest.main()
