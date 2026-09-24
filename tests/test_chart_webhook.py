from unittest.mock import patch
import unittest
import test_tv_routes
from scanner.chart_news import webhook_payload


class ChartWebhook(unittest.TestCase):
    setUp = test_tv_routes.Routes.setUp
    tearDown = test_tv_routes.Routes.tearDown
    def payload(self):
        return dict(secret='testsecret',schema_version=1,source='MF_V13_29_CHART_NEWS',
                    ticker='META',direction='LONG',action='MC',sampling='closed_5m',
                    observed_at='2026-09-23T14:00:00Z',bar_end='2026-09-23T14:00:00Z',
                    bar_time=1790171700000,previous_bar_time=1790171400000,
                    benchmark_bar_time=1790171700000,benchmark_previous_bar_time=1790171400000,
                    price=100,ema9=99,ema21=98,vwap=99,ema9_slope_atr=.1,
                    symbol_return_pct=1,benchmark_return_pct=.2,relative_volume=3,
                    directional_momentum_atr=1,rsi=70,extension_atr=1,
                    choppy=False,wide_whipsaw=False,breakout_valid=True,pullback_valid=False)
    def test_pine_roundtrip_shadow(self):
        with patch.object(__import__('index'),'send_telegram') as send:
            r=self.client.post('/webhook/chart-news',json=self.payload())
        self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(r.json()['telegram_sent'],0)
        self.assertEqual(r.json()['decisions'][0]['chart_score'],75)
        send.assert_not_called()
        with self.db.transaction() as tx:
            self.assertNotIn('testsecret',str(tx.events('2026-09-23')))
    def test_reject_wrong_auth(self):
        p=self.payload();p['secret']='wrong'
        self.assertEqual(self.client.post('/webhook/chart-news',json=p).status_code,401)
    def test_stale_misaligned_and_exit(self):
        for change in [dict(bar_end='2026-09-23T13:50:00Z'),dict(benchmark_bar_time=1),
                       dict(benchmark_previous_bar_time=1),dict(action='SX'),dict(sampling='intrabar')]:
            with self.subTest(change=change):
                r=self.client.post('/webhook/chart-news',json={**self.payload(),**change})
                self.assertEqual(r.status_code,422,r.text)
