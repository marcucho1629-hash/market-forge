"""Shared 05:30 SIP premarket -> RTH IEX underlying continuity.

Symbols are Cboe extended-options underlyings from the existing 05:30 watch,
NOT verified as actually tradeable options on the user's Robinhood account.
No option volume, quotes, broker permissions or orders are inferred.
"""
SYMBOLS = ("SPY","QQQ","IWM","GLD","XLF","AAPL","AMD","AMZN","AVGO",
           "BABA","BAC","F","GOOG","GOOGL","HOOD","INTC","META",
           "MRVL","MSFT","MU","NFLX","NOK","NVDA","ORCL","PFE",
           "PLTR","SNDK","SPCX","TSLA","TSM")


def tracked_symbols(base):
    """Return existing universe plus every premarket underlying, without duplicates."""
    return list(dict.fromkeys([*base, *SYMBOLS]))
