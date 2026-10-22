import hashlib
import io
import json
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
import smoke

class SmokeTests(unittest.TestCase):
    def setUp(self):
        self.env = {'VERCEL_ENV': 'preview', 'VERCEL_GIT_COMMIT_REF': smoke.BRANCH,
                    'TELEGRAM_BOT_TOKEN': 'fake', 'TELEGRAM_CHAT_ID': 'fake'}
        self.now = datetime(2026, 10, 21, tzinfo=timezone.utc)
        smoke._attempted = False

    def test_actual_engine_output(self):
        events = smoke.scenario()
        self.assertEqual([(x.action, x.price) for x in events], [('MC',368.35), ('FAST_X',367.20)])
        self.assertIn('인공 데이터', smoke.message())
        self.assertIn('아직 활성화되지', smoke.message())

    def test_guard(self):
        with patch.object(smoke, 'KEY_SHA256', hashlib.sha256(b'test').hexdigest()):
            smoke.authorize('test', self.env, self.now)
            for changed in ({'VERCEL_ENV':'production'}, {'VERCEL_GIT_COMMIT_REF':'main'}, {'TELEGRAM_BOT_TOKEN':''}):
                with self.assertRaises((PermissionError, RuntimeError)):
                    smoke.authorize('test', self.env | changed, self.now)
            with self.assertRaises(PermissionError): smoke.authorize('wrong', self.env, self.now)
            with self.assertRaises(PermissionError): smoke.authorize('test', self.env, smoke.EXPIRES)

    def test_send_and_no_duplicate_in_instance(self):
        calls=[]
        def fake(req, timeout):
            calls.append(json.loads(req.data))
            return io.BytesIO(b'{"ok":true,"result":{"message_id":123}}')
        with patch.dict(os.environ,self.env), patch.object(smoke,'authorize'):
            self.assertEqual(smoke.send_once('test',fake)['message_id'],123)
            with self.assertRaises(RuntimeError): smoke.send_once('test',fake)
        self.assertEqual(len(calls),1)

    def test_ambiguous_failure_not_retried_or_leaked(self):
        def fail(*args,**kwargs): raise ValueError('private token URL')
        with patch.dict(os.environ,self.env), patch.object(smoke,'authorize'):
            with self.assertRaisesRegex(RuntimeError,'Delivery not confirmed') as err:
                smoke.send_once('test',fail)
            self.assertNotIn('private',str(err.exception))
            with self.assertRaisesRegex(RuntimeError,'Already attempted'): smoke.send_once('test',fail)

if __name__ == '__main__': unittest.main()
