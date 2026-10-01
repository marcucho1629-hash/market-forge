import unittest
from unittest.mock import patch
import test_tv_routes,test_fast_path

class FastRoutes(unittest.TestCase):
 setUp=test_tv_routes.Routes.setUp
 tearDown=test_tv_routes.Routes.tearDown
 def test_new_source_is_authenticated_and_shadow(self):
  f=test_fast_path.FastPath();f.setUp()
  try:
   payload={'source':'MF_V13_33','schema_version':5,'bank':1,'observations':[dict(f.row(),kind='pulse')]}
   self.assertEqual(self.client.post('/webhook/scanner-v3',json=payload).status_code,401)
   with patch.object(__import__('index'),'now_utc',return_value=f.t),patch.object(__import__('index'),'send_telegram') as send:
    response=self.client.post('/webhook/scanner-v3',json={**payload,'secret':'testsecret'})
   self.assertEqual(response.status_code,200,response.text);self.assertEqual(response.json()['mode'],'shadow');send.assert_not_called()
   with self.db.transaction() as tx:self.assertNotIn('testsecret',str(tx.events('2026-09-25')))
  finally:f.tearDown()
