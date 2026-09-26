from unittest.mock import patch
import unittest
import test_tv_routes
import test_engine_relay

class RelayRoutes(unittest.TestCase):
    setUp=test_tv_routes.Routes.setUp
    tearDown=test_tv_routes.Routes.tearDown
    def test_auth(self):
        self.assertEqual(self.client.post('/webhook/scanner-v3',json={}).status_code,401)
        self.assertEqual(self.client.get('/scanner/v3/status').status_code,401)
    def test_shadow_roundtrip_no_secret_in_log(self):
        fixture=test_engine_relay.RelayTests();fixture.setUp()
        try:
            row=fixture.row
            with patch.object(__import__('index'),'now_utc',return_value=fixture.now),patch.object(__import__('index'),'send_telegram') as send:
                result=self.client.post('/webhook/scanner-v3',json={'secret':'testsecret','source':'MF_V13_31','schema_version':3,'bank':1,'kind':'pulse','observations':[row]})
            self.assertEqual(result.status_code,200,result.text);self.assertEqual(result.json()['mode'],'shadow');send.assert_not_called()
            with self.db.transaction() as tx:self.assertNotIn('testsecret',str(tx.events('2026-09-25')))
        finally:fixture.tearDown()
