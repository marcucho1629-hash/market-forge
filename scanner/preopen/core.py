"""Pure pre-open research watchlist. Does not call any entry or exit engine."""
import hashlib
import json
import math
from datetime import datetime, timedelta
from pathlib import Path


def config():
    return json.loads(Path(__file__).with_name('config.json').read_text())


def config_hash(c):
    return hashlib.sha256(json.dumps(c, sort_keys=True).encode()).hexdigest()[:16]


def stamp(s):
    return datetime.fromisoformat(s.replace('Z', '+00:00'))


def validate(rows, minutes, start, end):
    found = {}
    for r in rows:
        t = stamp(r['t'])
        if not t.tzinfo or t.second or t.microsecond or t.minute % minutes:
            raise ValueError('invalid_timestamp')
        if t < start or t + timedelta(minutes=minutes) > end:
            continue
        vals = [r[k] for k in ('o', 'h', 'l', 'c', 'v', 'n', 'vw')]
        if not all(isinstance(x, (int, float)) and math.isfinite(x) for x in vals):
            raise ValueError('invalid_numeric')
        if not (0 < r['l'] <= min(r['o'], r['c']) <= max(r['o'], r['c']) <= r['h'] and r['v'] > 0 and r['n'] > 0 and r['vw'] > 0):
            raise ValueError('invalid_ohlcv')
        if t in found and found[t] != r:
            raise ValueError('conflicting_duplicate')
        found[t] = r
    return [found[t] for t in sorted(found)]


def reconcile(one, five):
    used = set()
    for r in five:
        t = stamp(r['t'])
        parts = [p for p in one if t <= stamp(p['t']) < t + timedelta(minutes=5)]
        if not parts:
            return False
        used.update(p['t'] for p in parts)
        values = dict(o=parts[0]['o'], h=max(p['h'] for p in parts), l=min(p['l'] for p in parts), c=parts[-1]['c'], v=sum(p['v'] for p in parts), n=sum(p['n'] for p in parts))
        if any(not math.isclose(v, r[k], rel_tol=1e-8, abs_tol=1e-8) for k, v in values.items()):
            return False
    return len(used) == len(one)


def ema(values, period):
    out = []
    for x in values:
        out.append(x if not out else out[-1] + 2 / (period + 1) * (x - out[-1]))
    return out


def evaluate(ticker, seed, one, five, start, cutoff, observed_at, c):
    out = dict(ticker=ticker, feed='IEX', catalyst='UNKNOWN', technical_pass=False,
               observed_at=observed_at.isoformat(), cutoff=cutoff.isoformat(), reasons=[])
    if observed_at > cutoff:
        return out | dict(quality='LATE_RECEIPT', reasons=['received_after_cutoff'])
    # Use the last 5-minute boundary fully completed before this capture.
    end = min(cutoff, observed_at).replace(second=0, microsecond=0)
    end -= timedelta(minutes=end.minute % 5)
    try:
        seed = validate(seed, 5, start - timedelta(days=14), start)
        one = validate(one, 1, start, end)
        five = validate(five, 5, start, end)
    except (KeyError, TypeError, ValueError):
        return out | dict(quality='INVALID', reasons=['invalid_or_conflicting_data'])
    expected = int((end - start).total_seconds() / 60)
    coverage = len(one) / expected if expected > 0 else 0
    notional = sum(r['vw'] * r['v'] for r in one)
    out.update(seed_bars=len(seed), minute_bars=len(one), native_bars=len(five), coverage=coverage, iex_notional=notional)
    reasons = []
    if len(seed) < c['min_seed_5m']: reasons.append('seed_insufficient')
    if len(five) < c['min_pm_5m']: reasons.append('premarket_bars_insufficient')
    if notional < c['min_notional']: reasons.append('iex_liquidity_insufficient')
    last = stamp(five[-1]['t']) + timedelta(minutes=5) if five else None
    if not last or (cutoff - last).total_seconds() > c['max_age_seconds']: reasons.append('stale')
    ends = {stamp(r['t']) + timedelta(minutes=5) for r in five}
    if not all(end - timedelta(minutes=i * 5) in ends for i in range(3)): reasons.append('recent_native_windows_missing')
    if not reconcile(one, five): reasons.append('aggregation_mismatch')
    out.update(data_end=last.isoformat() if last else None, strict_minute_policy_pass=coverage >= .8 and all(end - timedelta(minutes=i) in {stamp(r['t']) for r in one} for i in range(1, 16)))
    if reasons:
        return out | dict(quality='INSUFFICIENT', reasons=reasons)
    out['quality'] = 'SPARSE_RECONCILED' if len(one) < expected else 'RECONCILED'
    all_bars = seed + five
    closes = [r['c'] for r in all_bars]
    e9, e21 = ema(closes, 9), ema(closes, 21)
    # Intraday ATR excludes the overnight gap: every seed/PM boundary resets prev close.
    ranges = []
    for i, r in enumerate(all_bars):
        same_day = i and stamp(all_bars[i-1]['t']).date() == stamp(r['t']).date()
        prev = all_bars[i-1]['c'] if same_day else r['o']
        ranges.append(max(r['h']-r['l'], abs(r['h']-prev), abs(r['l']-prev)))
    atr = sum(ranges[-14:]) / 14
    if atr <= 0:
        return out | dict(quality='INVALID', reasons=['atr_zero'])
    pv = vol = 0
    vwaps = []
    for r in five:
        pv += r['vw'] * r['v']; vol += r['v']; vwaps.append(pv / vol)
    p = five[-1]['c']; d = 1 if e9[-1] > e21[-1] else -1
    slope9 = d * (e9[-1] - e9[-4]) > 0
    slope21 = d * (e21[-1] - e21[-4]) > 0
    favorable = sum(d*(r['c']-v) > 0 for r, v in zip(five[-3:], vwaps[-3:]))
    recent, before = five[-3:], five[-6:-3]
    high_structure = d * (max(r['h'] for r in recent) - max(r['h'] for r in before)) > 0
    low_structure = d * (min(r['l'] for r in recent) - min(r['l'] for r in before)) > 0
    moves = [abs(closes[i]-closes[i-1]) for i in range(len(closes)-6, len(closes))]
    efficiency = abs(closes[-1]-closes[-7]) / sum(moves) if sum(moves) else 0
    wick = sum(1-abs(r['c']-r['o'])/(r['h']-r['l']) if r['h']>r['l'] else 1 for r in five[-6:])/6
    signs = [a > b for a,b in zip(e9[-7:], e21[-7:])]
    crosses = sum(a != b for a,b in zip(signs, signs[1:]))
    chop = crosses >= 3 or (efficiency < .25 and wick > .60)
    ext = dict(ema=d*(p-e9[-1])/atr, vwap=d*(p-vwaps[-1])/atr, impulse=d*(p-closes[-4])/atr)
    extended = any(ext[k] > c[name] for k,name in [('ema','ema_chase_atr'),('vwap','vwap_chase_atr'),('impulse','impulse_chase_atr')])
    if not(slope9 and slope21 and favorable >= 2 and high_structure and low_structure): reasons.append('direction_or_structure')
    if chop: reasons.append('choppy')
    if extended: reasons.append('extended_wait_pullback')
    stop = min(r['l'] for r in recent)-.15*atr if d==1 else max(r['h'] for r in recent)+.15*atr
    targets = [r['h'] if d==1 else r['l'] for r in all_bars[:-3] if d*((r['h'] if d==1 else r['l'])-p)>0]
    target = (min(targets) if d==1 else max(targets)) if targets else None
    rr = d*(target-p)/(d*(p-stop)) if target is not None and d*(p-stop)>0 else None
    # Unknown historical RVOL stays UNKNOWN and earns no points.
    pullback = any(min(abs(r['l']-a),abs(r['h']-a),abs(r['l']-b),abs(r['h']-b)) <= .25*atr and d*(r['c']-a)>0 for r,a,b in zip(recent,e9[-3:],e21[-3:]))
    vwsigns = [d*(r['c']-v)>0 for r,v in zip(five[-6:],vwaps[-6:])]
    points = dict(ema=10+10*int(slope9)+10*int(slope21),
                  vwap=(10 if favorable==3 else 5 if favorable==2 else 0)+5*int(d*(vwaps[-1]-vwaps[-4])>0)+5*int(sum(a!=b for a,b in zip(vwsigns,vwsigns[1:]))<=1),
                  structure=5*int(high_structure)+5*int(low_structure)+5*int(pullback)+5*int(d*(p-stop)>0 and high_structure and low_structure),
                  volume=5*int(five[-1]['v'] >= 1.2*sum(r['v'] for r in five[-4:-1])/3),
                  momentum=5*int(d*(closes[-1]-closes[-4]) >= .3*atr)+5*int(efficiency>=.5),
                  location=5*int(not extended and rr is not None and rr>=1.5))
    score = sum(points.values())
    if score < c['min_score']: reasons.append('score_below_minimum')
    out.update(direction='BULLISH' if d==1 else 'BEARISH', price=p, score=score, grade='A' if score>=85 else 'B' if score>=75 else 'C', points=points, atr=atr,
               ema9=e9[-1], ema21=e21[-1], vwap=vwaps[-1], vwap_name='IEX_PM_VWAP_0800', extension_atr=ext,
               structural_stop=stop, target=target, rr=rr, rvol='UNKNOWN', reasons=reasons,
               status='CHOPPY' if chop else 'EXTENDED' if extended else 'CLEAN' if not reasons else 'INVALID',
               setup='Pullback / Breakout WATCH', technical_pass=not reasons, actual_entry='NOT_YET')
    return out


def rank(rows, c):
    picks = []
    for side in ('BULLISH','BEARISH'):
        choices = [r for r in rows if r.get('technical_pass') and r.get('direction')==side]
        picks.extend(sorted(choices, key=lambda r:(-r['score'], -stamp(r['data_end']).timestamp(), r['extension_atr']['ema'], r['ticker']))[:c['top_per_side']])
    return picks
