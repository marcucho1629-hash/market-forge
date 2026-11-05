import os
import tempfile
import unittest
from unittest.mock import patch, Mock
from fastapi import FastAPI
from fastapi.testclient import TestClient
from scanner.store import Store
from scanner.preopen import runtime as r


class Routes(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(self.tmp.name+'/db');self.send=Mock();self.app=FastAPI()
        r.install(self.app,lambda:self.store,self.send);self.client=TestClient(self.app)
        self.env=patch.dict(os.environ,{'CHART_NEWS_WEBHOOK_SECRET':'local-test-only'})
        self.env.start();self.addCleanup(self.env.stop)

    def post(self,action):
        return self.client.post('/preopen-watch/control',data={'secret':'local-test-only','action':action})

    def test_auth_required(self):
        self.assertEqual(self.client.post('/preopen-watch/worker',json={'secret':'wrong'}).status_code,401)
        self.assertEqual(self.client.post('/preopen-watch/control',data={'secret':'wrong'}).status_code,401)
        self.send.assert_not_called()

    def test_status_does_not_activate_or_send(self):
        self.assertEqual(self.post('status').status_code,200)
        with self.store.transaction() as tx:self.assertIsNone(tx.get(r.PREFIX+'control'))
        self.send.assert_not_called()

    def test_enable_requires_data_and_telegram(self):
        self.assertEqual(self.post('enable').status_code,409)
        with self.store.transaction() as tx:
            tx.put(r.PREFIX+'probe',dict(at=r.utc().isoformat(),config_hash=r.config_hash(r.config()),counts={'DATA_PRESENT':150}))
        self.assertEqual(self.post('enable').status_code,409)
        self.assertEqual(self.post('connect').status_code,200)
        self.assertEqual(self.post('enable').status_code,200)
        with self.store.transaction() as tx:self.assertTrue(tx.get(r.PREFIX+'control')['enabled'])

    def test_connect_once_and_disable(self):
        self.post('connect');self.post('connect');self.assertEqual(self.send.call_count,1)
        self.post('disable')
        with self.store.transaction() as tx:self.assertFalse(tx.get(r.PREFIX+'control')['enabled'])


if __name__=='__main__':unittest.main()
