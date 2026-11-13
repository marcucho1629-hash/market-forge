"""Regression for missing Swing bank alarms and EOD Shadow reporting.

Must not alter technical candidate gates, chart alerts, orders or Day scanner.
"""
import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from unittest.mock import patch
from scanner.store import Store
from scanner.swing_compare import PREFIX as BANK
from scanner.swing_scanner_shadow import PREFIX as SHADOW
from scanner import swing_delivery_health as h

NOW=datetime(2026,11,11,21,40,tzinfo=timezone.utc)
DAY="2026-11-11"

class Health(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db=Store(temp.name+"/health.sqlite")
        self.sent=[]
    def test_missing_all_13_is_diagnostic_not_zero_technical_candidates(self):
        report=h.assess(self.db,NOW)
        self.assertEqual(report["bank_count"],0)
        self.assertEqual(report["missing_banks"],list(range(1,14)))
        self.assertEqual(report["shadow_status"],"not_recorded")
        self.assertIn("Bank 수신 0/13",h.message(report))
        self.assertIn("각 Bank마다 확인",h.message(report))
        self.assertIn("매매 진입 신호가 아닙니다",h.message(report))
    def test_partial_banks_and_old_records_not_counted(self):
        with self.db.transaction() as tx:
            tx.put(BANK+"bank:1",{"received_at":NOW.isoformat(),"events":2,"queued":1})
            tx.put(BANK+"bank:2",{"received_at":(NOW-timedelta(days=1)).isoformat(),
                                  "events":100,"queued":100})
            tx.put(SHADOW+DAY+":report",{"picks":[]})
            tx.enqueue(SHADOW+DAY+":telegram",{"text":"old shadow"})
        x=h.assess(self.db,NOW)
        self.assertEqual(x["bank_count"],1)
        self.assertEqual(x["event_count_latest"],2)
        self.assertEqual(x["queued_latest"],1)
        self.assertIn(2,x["missing_banks"])
        self.assertEqual(x["shadow_status"],"recorded")
        self.assertEqual(x["shadow_picks"],0)
        self.assertEqual(x["shadow_delivery"],"pending")
    def test_every_bank_received_with_shadow_stays_silent(self):
        with self.db.transaction() as tx:
            for i in range(1,14):
                tx.put(BANK+"bank:"+str(i),{"received_at":NOW.isoformat(),"events":0,"queued":0})
            tx.put(SHADOW+DAY+":report",{"picks":[]})
        with patch.object(h,"schedule",return_value={"is_open":True}):
            result=h.worker(self.db,self.sent.append,NOW)
        self.assertEqual(result["status"],"assessed")
        self.assertEqual(result["banks"],13)
        self.assertEqual(self.sent,[])
    def test_worker_missing_bank_reports_once_and_never_sends_entry(self):
        with patch.object(h,"schedule",return_value={"is_open":True}):
            first=h.worker(self.db,self.sent.append,NOW)
            repeat=h.worker(self.db,self.sent.append,NOW+timedelta(minutes=1))
        self.assertEqual(first["status"],"assessed")
        self.assertEqual(first["delivery"],["sent"])
        self.assertEqual(repeat["delivery"],[])
        self.assertEqual(len(self.sent),1)
        self.assertIn("/webhook/swing-bank-v1",self.sent[0])
        self.assertNotIn("MODEL_ENTRY",self.sent[0])
        with self.db.transaction() as tx:
            row=tx.execute("SELECT status FROM mf_outbox WHERE id=?",
                           (h.PREFIX+DAY+":telegram",)).fetchone()
        self.assertEqual(row[0],"sent")
    def test_missing_shadow_even_with_all_banks_is_reported(self):
        with self.db.transaction() as tx:
            for i in range(1,14):
                tx.put(BANK+"bank:"+str(i),{"received_at":NOW.isoformat(),"events":0,"queued":0})
        with patch.object(h,"schedule",return_value={"is_open":True}):
            x=h.worker(self.db,self.sent.append,NOW)
        self.assertEqual(x["shadow"],"not_recorded")
        self.assertEqual(x["delivery"],["sent"])
        self.assertIn("Server Swing Shadow: not_recorded",self.sent[0])
    def test_shadow_error_reason_exposed_without_credentials(self):
        with self.db.transaction() as tx:
            tx.put(SHADOW+DAY+":last_error",{
                "status":"provider_batch_failed",
                "failed_batches":2,
                "causes":{"alpaca_http_429":2},
                "at":NOW.isoformat()})
        report=h.assess(self.db,NOW)
        self.assertEqual(report["shadow_error_status"],"provider_batch_failed")
        message=h.message(report)
        self.assertIn("Swing Shadow 오류: provider_batch_failed",message)
        self.assertIn("alpaca_http_429 2",message)
        self.assertNotIn("Bearer",message)
    def test_weekend_and_early_time_do_not_send(self):
        with patch.object(h,"schedule",return_value={"is_open":True}):
            self.assertEqual(h.worker(self.db,self.sent.append,NOW-timedelta(minutes=15))["status"],
                             "outside_1440_mountain")
        weekend=datetime(2026,11,14,21,40,tzinfo=timezone.utc)
        self.assertFalse(h.scheduled(weekend))
        with patch.object(h,"schedule",return_value={"is_open":False}):
            self.assertEqual(h.worker(self.db,self.sent.append,NOW)["status"],"exchange_closed")
        self.assertEqual(self.sent,[])

if __name__=="__main__":unittest.main()
