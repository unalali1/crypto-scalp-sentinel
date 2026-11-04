"""Independent, closed-candle trend-pullback hypothesis for paper swing trades."""
from dataclasses import dataclass, field
from math import isfinite

from .engine import _clear_path, atr, candles, ema, price_round

HOUR=3_600_000
FOUR_HOURS=4*HOUR
DAY=24*HOUR


@dataclass
class SwingSetup:
    symbol: str
    side: str
    snapshot_ts: int
    setup_id: str
    entry: float
    stop: float
    tp1: float
    tp2: float
    tick_size: str
    atr4: float
    spread_bps: float
    quote_volume: float
    rvol4: float
    checks: dict[str,bool]
    eligible: bool
    priority: float
    strategy_version: str = "swing-trend-v1"
    journal: dict = field(default_factory=dict)


def daily_direction(rows):
    bars=candles(rows)
    if len(bars)<400:
        return None
    close=[b.c for b in bars]
    e50=ema(close,50)
    e200=ema(close,200)
    if close[-1]>e50[-1]>e200[-1] and e50[-1]>e50[-5]:
        return "LONG"
    if close[-1]<e50[-1]<e200[-1] and e50[-1]<e50[-5]:
        return "SHORT"
    return None


def evaluate_swing(symbol, snapshot, btc_daily, tick_size):
    """Assess both directions at a completed 4h/1h boundary; no 1m scalp gates."""
    daily=candles(snapshot["1d"])
    four=candles(snapshot["4h"])
    hour=candles(snapshot["1h"])
    if len(daily)<400 or len(four)<90 or len(hour)<20:
        return []
    last=four[-1]
    if hour[-1].ts+HOUR!=last.ts+FOUR_HOURS:
        return []
    a4=atr(four)
    if not isfinite(a4) or a4<=0:
        return []
    e20=ema([b.c for b in four],20)
    e50=ema([b.c for b in four],50)
    rvol4=last.v/(sum(b.v for b in four[-21:-1])/20) if sum(b.v for b in four[-21:-1])>0 else 0
    bid,ask=(float(snapshot["book"][k]) for k in ("bidPrice","askPrice"))
    if not all(isfinite(v) and v>0 for v in (bid,ask)) or ask<=bid:
        return []
    spread=(ask-bid)/((ask+bid)/2)*10_000
    quote_volume=float(snapshot["quote_volume"])
    market=daily_direction(snapshot["1d"])
    btc=daily_direction(btc_daily)
    prior=four[-4:-1]
    span=last.h-last.l
    if span<=0:
        return []
    result=[]
    for side in ("LONG","SHORT"):
        sign=1 if side=="LONG" else -1
        if sign==1:
            pullback=(min(b.l for b in prior)<=e20[-2]+.25*a4
                      and min(b.l for b in prior)>=e50[-2]-.5*a4)
            reclaim=(last.c>e20[-1] and last.c>last.o and last.c>prior[-1].c
                     and (last.c-last.l)/span>=.65)
            confirm=hour[-1].c>max(b.h for b in hour[-4:-1]) and hour[-1].c>hour[-1].o
            stop=price_round(min(b.l for b in four[-4:])-.25*a4,tick_size,up=False)
        else:
            pullback=(max(b.h for b in prior)>=e20[-2]-.25*a4
                      and max(b.h for b in prior)<=e50[-2]+.5*a4)
            reclaim=(last.c<e20[-1] and last.c<last.o and last.c<prior[-1].c
                     and (last.c-last.l)/span<=.35)
            confirm=hour[-1].c<min(b.l for b in hour[-4:-1]) and hour[-1].c<hour[-1].o
            stop=price_round(max(b.h for b in four[-4:])+.25*a4,tick_size,up=True)
        entry=hour[-1].c
        risk=sign*(entry-stop)
        tp1=price_round(entry+sign*1.5*risk,tick_size,up=sign==1) if risk>0 else entry
        tp2=price_round(entry+sign*3*risk,tick_size,up=sign==1) if risk>0 else entry
        checks={
            "daily_trend":market==side,
            "btc_regime":btc==side,
            "4h_pullback":pullback,
            "4h_reclaim":reclaim,
            "1h_confirmation":confirm,
            "4h_volume":rvol4>=1.1,
            "risk_and_path":bool(risk>0 and .8<=risk/a4<=3.5
                                 and risk/entry<=.08
                                 and _clear_path(four[-90:-1],side,entry,risk,tp1)),
            "liquidity":quote_volume>=30_000_000 and spread<=8,
        }
        result.append(SwingSetup(symbol,side,last.ts,
            f"swing-trend-v1:{symbol}:{side}:{last.ts}",entry,stop,tp1,tp2,
            tick_size,a4,spread,quote_volume,rvol4,checks,all(checks.values()),
            quote_volume/max(spread,1)))
    return result
