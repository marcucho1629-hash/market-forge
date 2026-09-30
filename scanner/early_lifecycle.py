"""Offline EARLY lifecycle reference. No networking or order/Telegram side effects.

Caller persists returned state keyed by feed + 5m bar_start + MC/SCMP.
An EARLY cancellation means a candidate failed confirmation, not a trading exit.
"""
from dataclasses import dataclass, replace
from datetime import datetime

@dataclass(frozen=True)
class State:
    phase: str = 'watching'
    last_observed: datetime | None = None
    first_early: datetime | None = None
    chase_blocked: bool = False

def advance(state, *, observed, received, bar_start, bar_end, raw_candidate,
            distance_atr, confirmed_flag=None, max_chase_atr=0.75):
    if not all(t.tzinfo is not None for t in (observed,received,bar_start,bar_end)):
        raise ValueError('timezone-aware timestamps required')
    if (bar_end-bar_start).total_seconds()!=300:
        raise ValueError('5m source bar required')
    if state.last_observed is not None and observed <= state.last_observed:
        return state, None
    if not 0 <= (received-observed).total_seconds() <= 15:
        return state, None
    if observed < bar_start:
        return state,None
    if state.phase in ('confirmed','invalidated','closed_without_entry'):
        return state,None
    new=replace(state,last_observed=observed)
    if observed >= bar_end:
        # Only an explicit closed snapshot can confirm/invalidate. A missing packet
        # does not prove the condition was false. No late EARLY entry is synthesized.
        if confirmed_flag is None:
            return new,None
        if state.phase=='early':
            return replace(new,phase='confirmed' if confirmed_flag else 'invalidated'), 'CONFIRM_EARLY' if confirmed_flag else 'INVALIDATE_EARLY'
        return replace(new,phase='confirmed' if confirmed_flag else 'closed_without_entry'), 'CONFIRMED_ENTRY' if confirmed_flag else None
    if confirmed_flag is not None:
        raise ValueError('closed confirmation received before source bar end')
    if state.phase=='early':
        return new,None
    if state.chase_blocked or not raw_candidate:
        return new,None
    if distance_atr is None or not 0 <= distance_atr <= max_chase_atr:
        # Never wait for a late retracement to silently turn a rejected impulse
        # into the first EARLY alert for that same source bar.
        return replace(new,chase_blocked=True),None
    return replace(new,phase='early',first_early=observed),'EARLY_ENTRY'
