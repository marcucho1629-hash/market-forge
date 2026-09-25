"""Exchange schedule, including holidays, DST and scheduled early closes."""
from functools import lru_cache
from datetime import timedelta
from .core import ET,stamp

@lru_cache(maxsize=32)
def schedule(day):
    import pandas_market_calendars as mcal
    table=mcal.get_calendar('NYSE').schedule(start_date=day,end_date=day)
    if table.empty:return {'date':day,'is_open':False,'source':'NYSE/pandas_market_calendars'}
    row=table.iloc[0]
    return {'date':day,'is_open':True,'open':row.market_open.isoformat(),'close':row.market_close.isoformat(),'source':'NYSE/pandas_market_calendars'}

def current_session(now):
    s=schedule(now.astimezone(ET).date().isoformat())
    return s, bool(s['is_open'] and stamp(s['open'])<=now<stamp(s['close']))
