from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta, timezone
from math import isfinite
from statistics import mean
from zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')
VERSION = 'mf-server-1.1'


def stamp(value):
    d = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if d.tzinfo is None:
        raise ValueError('timestamps must include timezone')
    return d.astimezone(timezone.utc)


def finite(value, low=None, high=None):
    n = float(value)
    if not isfinite(n) or (low is not None and n < low) or (high is not None and n > high):
        raise ValueError('invalid numeric value')
    return n


def clip(x, lo=0, hi=100):
    return max(lo, min(hi, x))


@dataclass(frozen=True)
class Bar:
    end: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

    @classmethod
    def parse(cls, d):
        b = cls(stamp(d['end']), *(finite(d[k], 0) for k in ('open', 'high', 'low', 'close', 'volume')))
        if b.low <= 0 or not (b.low <= min(b.open, b.close) <= max(b.open, b.close) <= b.high):
            raise ValueError('invalid OHLC')
        return b


def bars_from(rows, asof, minutes):
    bars = [Bar.parse(r) for r in rows]
    if len(bars) > 2500:
        raise ValueError('too many bars')
    for i, b in enumerate(bars):
        if b.end > asof or b.end.second or b.end.microsecond or b.end.minute % minutes:
            raise ValueError('future, incomplete or misaligned bar')
        if i and b.end <= bars[i-1].end:
            raise ValueError('bars must be unique and ordered')
    return bars


def ema(values, period):
    out = [values[0]]
    for x in values[1:]:
        out.append(out[-1] + 2 / (period + 1) * (x - out[-1]))
    return out


def rsi(values, period=14):
    if len(values) < period + 1:
        return 50.0
    changes = [b-a for a,b in zip(values, values[1:])]
    gain = mean(max(x,0) for x in changes[:period])
    loss = mean(max(-x,0) for x in changes[:period])
    for x in changes[period:]:
        gain = (gain*(period-1)+max(x,0))/period
        loss = (loss*(period-1)+max(-x,0))/period
    return 100 - 100/(1+gain/loss) if loss else (100 if gain else 50)


def efficiency(values):
    travel = sum(abs(b-a) for a,b in zip(values,values[1:]))
    return abs(values[-1]-values[0])/travel if travel else 0.0


@dataclass
class Config:
    threshold: float = 90
    watch_threshold: float = 85
    top_k: int = 3
    cooldown_minutes: int = 20
    chop_efficiency: float = .30
    clean_efficiency: float = .55
    trend_efficiency: float = .72
    chase_atr: float = 2.0
    max_chase_atr: float = 3.0
    min_premarket_dollars: float = 1_000_000
    min_price: float = 5

    def __post_init__(self):
        if not 1 <= self.top_k <= 3 or not 0 <= self.watch_threshold <= self.threshold <= 100:
            raise ValueError('invalid score thresholds/top_k')


@dataclass
class State:
    market: str = 'CAUTION'
    bias: int = 0
    clean_count: int = 0
    last_end: str = ''
    last_entry_end: str = ''
    position: int = 0  # shadow position in silent mode; notification lifecycle, not a broker position
    entry_price: float = 0
    entry_atr: float = 0
    cooldown_until: str = ''
    failed_side: int = 0
    last_selected_bucket: str = ''
    setup_side: int = 0
    setup_count: int = 0


@dataclass
class Decision:
    ticker: str
    end: str
    direction: int = 0
    action: str = 'WAIT'
    market: str = 'CAUTION'
    technical: float = 0
    catalyst: float = 50
    context: float = 50
    final: float = 0
    price: float = 0
    atr: float = 0
    eligible: bool = False
    selected: bool = False
    reasons: list = field(default_factory=list)
    features: dict = field(default_factory=dict)
    version: str = VERSION


def score_hook(item, direction, asof, max_age_minutes=180):
    """Directional point-in-time score. Missing/stale is neutral, never fabricated."""
    if not item:
        return 50., 'missing'
    t = stamp(item['asof'])
    if t > asof or asof-t > timedelta(minutes=max_age_minutes):
        return 50., 'stale_or_future'
    key = 'bullish' if direction > 0 else 'bearish'
    return finite(item[key], 0, 100), 'available'


def market_context(benchmarks, end, session_open, sector=None):
    """SPY 40%, QQQ 40%, sector ETF 20%; absent components remain neutral."""
    bullish = 0.0
    available = []
    for symbol,weight in [('SPY',.4),('QQQ',.4),(sector,.2)]:
        rows = benchmarks.get(symbol,[]) if symbol else []
        seq = bars_from(rows,end,5)
        score = 50.0
        if len(seq)>=21 and end-seq[-1].end<=timedelta(minutes=5):
            current = [b for b in seq if b.end>session_open]
            if current and sum(b.volume for b in current)>0:
                values=[b.close for b in seq]
                vwap=sum((b.high+b.low+b.close)/3*b.volume for b in current)/sum(b.volume for b in current)
                e9,e21=ema(values,9)[-1],ema(values,21)[-1]
                side=1 if values[-1]>e9 else -1
                aligned=side*(e9-e21)>0 and side*(values[-1]-vwap)>0
                recent=[b.close for b in current[-9:]]
                de=efficiency(([current[0].open]+recent) if len(current)<9 else recent)
                score=clip(50+side*(25*aligned+25*de))
                available.append(symbol)
        bullish+=weight*score
    if not available: return None
    return {'asof':end.isoformat(),'bullish':bullish,'bearish':100-bullish}


def select_universe(rows, asof, cfg):
    ranked = {1: [], -1: []}
    seen = set()
    for row in rows:
        symbol = str(row['ticker']).upper()
        if symbol in seen:
            raise ValueError('duplicate universe ticker')
        seen.add(symbol)
        price = finite(row['price'], .00001)
        previous = finite(row['previous_close'], .00001)
        volume = finite(row['premarket_volume'], 0)
        base = finite(row['average_premarket_volume'], .00001)
        t = stamp(row['asof'])
        if t > asof or asof-t > timedelta(minutes=5):
            continue
        if price < cfg.min_price or price*volume < cfg.min_premarket_dollars:
            continue
        gap = 100*(price/previous-1)
        rs = finite(row['relative_strength_pct'])
        sector = finite(row['sector_change_pct'])
        bias = gap + .5*rs + .25*sector
        if abs(bias) < .10:
            continue
        side = 1 if bias > 0 else -1
        catalyst, availability = score_hook(row.get('catalyst'), side, asof)
        score = (.30*clip(side*gap*15+50) + .25*clip(volume/base*35)
                 + .20*catalyst + .15*clip(side*rs*20+50) + .10*clip(side*sector*20+50))
        ranked[side].append({'ticker': symbol, 'bias': side, 'score': round(score,2),
                             'catalyst_status': availability, 'asof': asof.isoformat()})
    return {name: sorted(ranked[side], key=lambda x:(-x['score'],x['ticker']))[:10]
            for side,name in ((1,'bullish'),(-1,'bearish'))}


def evaluate(ticker, bars, state, session_open, cfg, catalyst=None, context=None,
             opening_bars=None, opening_volume_baseline=None):
    """Closed five-minute bars plus optional closed one-minute opening execution bars.

    No symbol-specific overrides. Caller persists mutated state atomically.
    """
    s = state
    fast = bool(opening_bars)
    active = opening_bars if fast else bars
    end = active[-1].end if active else session_open
    d = Decision(ticker, end.isoformat(), market=s.market)
    if s.last_end and end <= stamp(s.last_end):
        d.reasons = ['duplicate_or_out_of_order']
        return d
    if len(bars) < 30 or not active:
        d.reasons = ['insufficient_warmup']
        return d
    session = [b for b in active if b.end > session_open]
    if not session:
        d.reasons = ['no_session_bars']
        return d
    interval = timedelta(minutes=1 if fast else 5)
    if session[0].end != session_open + interval or any(b.end-a.end != interval for a,b in zip(session,session[1:])):
        d.reasons = ['missing_session_bars']
        return d
    if fast and not session_open < end < session_open+timedelta(minutes=5):
        d.reasons = ['outside_fast_lane']
        return d
    s.last_end = end.isoformat()
    # Warmup uses prior-session five-minute candles. Only the opening trigger uses 1m.
    history = bars + session if fast else bars
    close = [b.close for b in history]
    e9, e21 = ema(close,9), ema(close,21)
    trs = [max(b.high-b.low, abs(b.high-a.close), abs(b.low-a.close)) for a,b in zip(bars,bars[1:])]
    atr = mean(trs[-14:])
    b = session[-1]
    d.price, d.atr = b.close, atr
    if atr <= 0 or sum(x.volume for x in session) <= 0:
        d.reasons = ['no_liquidity_or_atr']
        return d
    vwap = sum((x.high+x.low+x.close)/3*x.volume for x in session)/sum(x.volume for x in session)
    values = [x.close for x in session[-9:]]
    de = efficiency([session[0].open]+values) if len(session)<9 else efficiency(values)
    changes = [z-a for a,z in zip(values,values[1:])]
    flips = sum(a*z < 0 for a,z in zip(changes,changes[1:]))
    crosses = sum((a-vwap)*(z-vwap)<0 for a,z in zip(values,values[1:]))
    recent = session[-8:]
    width = (max(x.high for x in recent)-min(x.low for x in recent))/atr
    displacement = abs(values[-1]-values[0])/atr if len(values)>1 else abs(b.close-b.open)/atr
    side = 1 if b.close > e9[-1] else -1
    aligned = side*(e9[-1]-e21[-1])>0 and side*(b.close-vwap)>0
    strong_structure = aligned and side*(e21[-1]-vwap)>0
    body = abs(b.close-b.open)/max(b.high-b.low,1e-9)
    directional_body = side*(b.close-b.open)>0
    prior = session[-7:-1]
    reference = prior or bars[-3:-1]
    breaking = bool(reference) and (b.close>max(x.high for x in reference) if side>0 else b.close<min(x.low for x in reference))
    baseline = opening_volume_baseline if fast else mean(x.volume for x in bars[-21:-1])
    rv = b.volume/baseline if baseline and baseline>0 else 0
    momentum = side*(rsi(close)-50)
    acceleration = side*(rsi(close)-rsi(close[:-1]))
    span_pct = 100*(max(x.high for x in recent)-min(x.low for x in recent))/b.close
    wide = (width>=2 or span_pct>=.8) and de<.45 and flips>=3
    chop = wide or (de<cfg.chop_efficiency and (flips>=2 or crosses>=2)) or (displacement<.25 and len(session)>=4)
    # A single spike cannot unlock a wide whipsaw. Two confirming closes can.
    two_confirm = len(session)>=3 and all(side*(z.close-a.close)>0 for a,z in zip(session[-3:],session[-2:]))
    old_range = session[-8:-2]
    prior_break = bool(old_range) and (session[-2].close>max(x.high for x in old_range)
                                      if side>0 else session[-2].close<min(x.low for x in old_range))
    short_de = efficiency([x.close for x in session[-4:]])
    escape = breaking and prior_break and two_confirm and aligned and short_de>=.8 and body>=.6 and rv>=1.2
    raw_de = de
    if escape:
        de = max(de, short_de*.9)
    extension = abs(b.close-e9[-1])/atr
    if breaking and aligned:
        s.setup_count = s.setup_count+1 if s.setup_side==side else 1
        s.setup_side = side
    else:
        s.setup_count = 0
    chase = clip((extension-cfg.chase_atr)*15,0,30)
    if s.setup_count>=3:
        chase += 10
    fast_ok = (fast and len(session)>=2 and breaking and aligned and directional_body
               and body>=.7 and abs(b.close-b.open)/atr>=.30 and rv>=1.5
               and momentum>=8 and acceleration>0 and de>=.75 and extension<cfg.chase_atr)
    if chop and not escape:
        market = 'CHOPPY'
        s.clean_count = 0
    elif extension>=cfg.max_chase_atr:
        market = 'EXHAUSTED'
        s.clean_count = 0
    elif aligned and de>=cfg.clean_efficiency:
        s.clean_count += 1
        market = 'TREND' if de>=cfg.trend_efficiency and s.clean_count>=2 else 'CLEAN'
    else:
        market = 'CAUTION'
        s.clean_count = 0
    if fast_ok:
        market = 'CLEAN'
    old_bias, old_market = s.bias, s.market
    s.market = d.market = market
    if market in ('CLEAN','TREND'):
        s.bias = side
    d.direction = side
    d.features = dict(efficiency=round(raw_de,4), effective_efficiency=round(de,4), wide_whipsaw=wide, flips=flips,
                      vwap_crosses=crosses, range_atr=round(width,3), vwap=vwap,
                      ema9=e9[-1], ema21=e21[-1], rsi=rsi(close), rvol=rv,
                      chase_atr=extension, chase_penalty=chase, breakout=breaking,
                      fast_lane=fast_ok, chop_escape=escape, strong_structure=strong_structure)
    d.catalyst, d.features['catalyst_status'] = score_hook(catalyst,side,end)
    d.context, d.features['context_status'] = score_hook(context,side,end,15)
    d.technical = round(clip(25*de + 20*aligned + 15*clip(rv/1.5,0,1)
                            + 15*body + 15*breaking + 10*clip(momentum/20,0,1)),2)
    penalty = 5 if market=='CAUTION' else 0
    d.final = round(clip(.7*d.technical+.2*d.catalyst+.1*d.context-chase-penalty),2)
    if s.position:
        adverse = s.position*(b.close-s.entry_price)
        broken = s.position*(b.close-e21[-1])<0 and s.position*(b.close-vwap)<0
        if adverse <= -s.entry_atr or broken:
            d.action = 'HX' if adverse <= -s.entry_atr else 'X'
            d.direction = s.position
            d.reasons.append('shadow_position_exit')
            if adverse<0:
                s.cooldown_until = (end+timedelta(minutes=cfg.cooldown_minutes)).isoformat()
                s.failed_side = s.position
            s.position = 0
        else:
            d.action = 'HOLD'
            d.reasons.append('existing_shadow_position')
        return d
    if old_market=='TREND' and old_bias and side != old_bias and not (aligned and escape):
        d.reasons.append('strong_trend_counter_signal')
    if market=='CHOPPY': d.reasons.append('wide_whipsaw' if wide else 'directional_chop')
    if market=='EXHAUSTED' or chase>0: d.reasons.append('late_chase')
    if fast and not fast_ok: d.reasons.append('opening_confirmation')
    if not fast and len(session)<2 and not (breaking and aligned and body>=.7 and rv>=1.5 and de>=.75):
        d.reasons.append('opening_confirmation')
    if s.cooldown_until and end<stamp(s.cooldown_until) and side==s.failed_side:
        d.reasons.append('failed_signal_cooldown')
    if not aligned or not directional_body or (not breaking and not fast_ok):
        d.reasons.append('no_entry_setup')
    if d.final<cfg.threshold: d.reasons.append('below_quality_gate')
    hard = {'strong_trend_counter_signal','wide_whipsaw','directional_chop','opening_confirmation',
            'failed_signal_cooldown','no_entry_setup'}
    d.eligible = not hard.intersection(d.reasons) and market!='EXHAUSTED' and d.final>=cfg.threshold
    entry_action = 'MC' if side>0 else ('SCMP' if abs(b.close-b.open)/atr>=2.5 and rv>=2.5 and body>=.8 else 'MP')
    d.action = entry_action if d.eligible else ('WATCH' if d.final>=cfg.watch_threshold else 'WAIT')
    return d


def rank_and_activate(decisions, states, cfg):
    candidates = sorted((d for d in decisions if d.eligible),key=lambda d:(-d.final,d.ticker))
    for rank,d in enumerate(candidates,1):
        d.features['rank'] = rank
        if rank>cfg.top_k:
            d.reasons.append('outside_top_k')
            continue
        d.selected = True
        s = states[d.ticker]
        s.position, s.entry_price, s.entry_atr = d.direction,d.price,d.atr
        s.last_entry_end = d.end
        d.reasons.append('selected')
    return decisions
