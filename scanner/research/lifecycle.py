"""Explicit shadow lifecycle. No Telegram, network, or production state access."""
from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import sha256
import json, math

TERMINAL={'CANCELLED','EXPIRED','CONFIRMED','INVALIDATED','EXPIRED_CLOSE_MISSING'}

def candidate_id(ticker,feed,timeframe,source_bar_start,direction,core_version):
    if direction not in (-1,1):raise ValueError('direction required')
    if source_bar_start.tzinfo is None:raise ValueError('UTC-aware bar required')
    from datetime import timezone
    fields=[ticker.upper(),feed.upper(),str(timeframe),source_bar_start.astimezone(timezone.utc).isoformat(),direction,core_version]
    if not all(fields):raise ValueError('identity field missing')
    return sha256(json.dumps(fields,separators=(',',':')).encode()).hexdigest()

@dataclass(frozen=True)
class Candidate:
    identity:str
    experiment_id:str
    config_hash:str
    side:int
    prep_at:datetime
    bar_end:datetime
    phase:str='PREP'
    last_observed:datetime|None=None
    last_closed_observed:datetime|None=None
    send_at:datetime|None=None
    signal_price:float|None=None
    signal_atr:float|None=None
    reason:str|None=None
    confirmed_at:datetime|None=None
    chart_x_at:datetime|None=None
    mfe:float=0.
    mae:float=0.
    samples:int=0

    def metrics(self):
        return dict(MFE=self.mfe,MAE=self.mae,
            MFE_pct=100*self.mfe/self.signal_price if self.signal_price else None,
            MAE_pct=100*self.mae/self.signal_price if self.signal_price else None,
            MFE_ATR=self.mfe/self.signal_atr if self.signal_atr else None,
            MAE_ATR=self.mae/self.signal_atr if self.signal_atr else None,samples=self.samples)

def expire(s,now):
    if s.phase=='PREP' and (now-s.prep_at).total_seconds()>=60:
        return replace(s,phase='EXPIRED',reason='PREP_DEADLINE')
    if s.phase=='WOULD_SEND' and (now-s.bar_end).total_seconds()>60:
        return replace(s,phase='EXPIRED_CLOSE_MISSING',reason='CLOSE_MISSING')
    return s

def observe(s,*,identity,source_observed_at,webhook_emitted_at,server_received_at,core_ok,price,atr,kind='pulse',confirmed=None,chart_x=False):
    if identity!=s.identity:raise ValueError('cross-candidate observation')
    if kind not in ('pulse','closed'):raise ValueError('invalid observation kind')
    if not all(math.isfinite(v) and v>0 for v in (price,atr)):raise ValueError('invalid price/ATR')
    if not source_observed_at<=webhook_emitted_at<=server_received_at:raise ValueError('clock ordering')
    if (server_received_at-source_observed_at).total_seconds()>15:return expire(s,server_received_at)
    if kind=='closed':
        if type(confirmed) is not bool:raise ValueError('explicit close result required')
        if source_observed_at<s.bar_end:raise ValueError('premature close')
        if s.last_closed_observed and source_observed_at<=s.last_closed_observed:return expire(s,server_received_at)
    elif s.last_observed and source_observed_at<=s.last_observed:return expire(s,server_received_at)
    s=expire(s,server_received_at)
    fresh_price = s.last_observed is None or source_observed_at>s.last_observed
    if fresh_price and s.send_at and 0<=(source_observed_at-s.send_at).total_seconds()<=600:
        move=s.side*(price-s.signal_price)
        s=replace(s,mfe=max(s.mfe,move),mae=min(s.mae,move),samples=s.samples+1)
    s=replace(s,last_observed=max(s.last_observed,source_observed_at) if s.last_observed else source_observed_at,last_closed_observed=source_observed_at if kind=='closed' else s.last_closed_observed)
    # Confirmation remains a historical fact; a later X is a separate field.
    if s.phase=='CONFIRMED':return replace(s,chart_x_at=source_observed_at) if chart_x and not s.chart_x_at else s
    if s.phase in TERMINAL:return s
    if kind=='closed':
        if source_observed_at<s.bar_end:raise ValueError('premature close')
        if type(confirmed) is not bool:raise ValueError('explicit close result required')
        if s.phase=='WOULD_SEND':
            return replace(s,phase='CONFIRMED' if confirmed else 'INVALIDATED',reason='BAR_CONFIRMED' if confirmed else 'BAR_FAILED',confirmed_at=source_observed_at if confirmed else None)
        return replace(s,phase='EXPIRED',reason='NO_EARLY_BEFORE_CLOSE')
    if source_observed_at>=s.bar_end:return s
    if not core_ok:return replace(s,phase='CANCELLED' if s.phase=='PREP' else 'INVALIDATED',reason='CORE_FAILED')
    if s.phase=='WOULD_SEND':return s
    age=(source_observed_at-s.prep_at).total_seconds();received_age=(server_received_at-s.prep_at).total_seconds()
    if 15<=age<=45 and received_age<=45:
        return replace(s,phase='WOULD_SEND',send_at=source_observed_at,signal_price=price,signal_atr=atr,samples=1)
    return s

def timestamps(source_observed_at,webhook_emitted_at,server_received_at):
    return dict(source_observed_at=source_observed_at.isoformat(),webhook_emitted_at=webhook_emitted_at.isoformat(),server_received_at=server_received_at.isoformat(),observed_to_emit_sec=(webhook_emitted_at-source_observed_at).total_seconds(),emit_to_receive_sec=(server_received_at-webhook_emitted_at).total_seconds())
