"""Authenticated OHLCV gateway contract; independent of chart-alert payloads.

No provider is silently chosen. A data vendor adapter or TradingView OHLCV producer
must supply the documented complete payloads. Price-only alerts cannot substitute.
"""
import os
from urllib.parse import urlparse
import requests


def provider_name():
    # Preserve already-configured gateway deployments; new setup defaults to Alpaca.
    return os.getenv('SCANNER_PROVIDER', 'http' if os.getenv('SCANNER_DATA_URL') else 'alpaca')


def get_provider(store=None):
    name=provider_name()
    if name=='alpaca':
        from .alpaca import AlpacaProvider
        return AlpacaProvider(store=store)
    if name=='http': return HTTPProvider()
    raise RuntimeError('SCANNER_PROVIDER must be alpaca or http')


class HTTPProvider:
    def __init__(self):
        self.base=os.environ.get('SCANNER_DATA_URL','').rstrip('/')
        self.token=os.environ.get('SCANNER_DATA_TOKEN','')
        if urlparse(self.base).scheme!='https' or not self.token:
            raise RuntimeError('Configure HTTPS SCANNER_DATA_URL and SCANNER_DATA_TOKEN')

    def fetch(self,kind,now,symbols=None):
        response=requests.get(self.base+'/'+kind,
            params={'asof':now.isoformat(),'symbols':','.join(symbols or [])},
            headers={'Authorization':'Bearer '+self.token},timeout=(5,20),allow_redirects=False)
        if response.status_code!=200:
            raise RuntimeError('Market data provider returned non-200 status')
        return response.json()
