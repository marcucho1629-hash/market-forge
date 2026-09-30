from unittest.mock import patch
import unittest
import test_tv_routes,test_engine_relay
class EarlyRoutes(unittest.TestCase):
 setUp=test_tv_routes.Routes.setUp
 tearDown=test_tv_routes.Routes.tearDown
 def test_auth_and_early_source_roundtrip(self):
  self.assertEqual(self.client.get('/scanner/v4/status').status_code,401)
  self.assertEqual(self.client.post('/scanner/v4/control',json={'enabled':True}).status_code,401)
  f=test_engine_relay.RelayTests();f.setUp()
  try:
   row={**f.row,'chart_mc':False,'early_mc_raw':True,'early_scmp_raw':False,'early_long_distance_atr':.3,'early_short_distance_atr':-.3}
   with patch.object(__import__('index'),'now_utc',return_value=f.now),patch.object(__import__('index'),'send_telegram') as send:
    r=self.client.post('/webhook/scanner-v3',json={'source':'MF_V13_32','schema_version':4,'secret':'testsecret','bank':1,'kind':'pulse','observations':[row]})
   self.assertEqual(r.status_code,200,r.text);self.assertEqual(r.json()['mode'],'shadow');send.assert_not_called()
   with self.db.transaction() as tx:self.assertNotIn('testsecret',str(tx.events('2026-09-25')))
  finally:f.tearDown()
