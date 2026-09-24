import os
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timezone
from fastapi.testclient import TestClient
import index
from scanner.store import Store

class Routes(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.db=Store(self.temp.name+'/db')
        self.patches=[patch.object(index,'storage',return_value=self.db),
                      patch.object(index,'now_utc',return_value=datetime(2026,9,23,14,tzinfo=timezone.utc)),
                      patch.dict(os.environ,{'TRADINGVIEW_WEBHOOK_SECRET':'testsecret','CRON_SECRET':'testadmin','TV_INFORMATION_MODE':'shadow'})]
        for p in self.patches:p.start()
        self.client=TestClient(index.app)
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.temp.cleanup()
    def test_no_duplicate_routes(self):
        routes=[(r.path,tuple(sorted(r.methods))) for r in index.app.routes]
        self.assertEqual(len(routes),len(set(routes)))
    def test_unauthenticated_rejected(self):
        for path in ['/webhook/tradingview','/tradingview/session','/tradingview/deliver','/scanner/chart-news/shadow','/scanner/chart-news/documents','/scanner/chart-news/review']:
            self.assertEqual(self.client.post(path,json={}).status_code,401)
    def test_calendar_today_only(self):
        result=self.client.post('/tradingview/session',headers={'Authorization':'Bearer testadmin'},json={'date':'2026-09-22','is_open':False})
        self.assertEqual(result.status_code,422)
    def test_shadow_delivery_has_no_side_effect(self):
        with patch.object(index,'send_telegram') as send:
            result=self.client.post('/tradingview/deliver',headers={'Authorization':'Bearer testadmin'})
        self.assertEqual(result.json()['delivery'],[])
        send.assert_not_called()
