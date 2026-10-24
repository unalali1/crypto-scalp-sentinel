"""Deterministic six-condition signal engine. No future-candle access."""
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR


@dataclass(frozen=True)
class Candle:
    o: float
    h: float
    l: float
    c: float
    v: float
    ts: int


def candles(rows):
    return [Candle(float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5]), int(k[0])) for k in rows]


def ema(values, period):
    alpha = 2 / (period + 1)
    prev = values[0]
    result = []
    for v in values:
        prev = alpha * v + (1 - alpha) * prev
        result.append(prev)
    return result


def atr(candles_, period=14):
    tr = []
    for i in range(1, len(candles_)):
        v, prev = candles_[i], candles_[i - 1]
        tr.append(max(v.h - v.l, abs(v.h - prev.c), abs(v.l - prev.c)))
    return sum(tr[-period:]) / period


def price_round(value: float, tick: str, up: bool) -> float:
    step = Decimal(tick)
    if step <= 0:
        raise ValueError("Bad tick size")
    x = Decimal(str(value)) / step
    return float(x.to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR) * step)


@dataclass
class Signal:
    symbol: str
    side: str
    score: int
    checks: dict[str, bool]
    entry: float
    stop: float
    tp1: float
    tp2: float
    atr5: float
    rvol: float
    cvd3: float
    cvd6: float
    oi_change_pct: float
    spread_bps: float
    tick_size: str
    snapshot_ts: int
    entry_trigger: float
    eligible: bool


def candidate(symbol, direction, snapshot, tick_size, cfg):
    data = {tf: candles(snapshot[tf]) for tf in ("1m", "5m", "15m", "1h", "4h")}
    one, five, fifteen, hour = (data[x] for x in ("1m", "5m", "15m", "1h"))
    close15 = [x.c for x in fifteen]
    e21, e50 = ema(close15, 21)[-1], ema(close15, 50)[-1]
    e21_hour = ema([x.c for x in hour], 21)[-1]
    e9_5 = ema([x.c for x in five], 9)[-1]
    last5 = five[-1]
    last1 = one[-1]
    vol_avg = sum(x.v for x in five[-21:-1]) / 20
    rvol = last5.v / vol_avg if vol_avg > 0 else 0
    a5 = atr(five)
    ticker = snapshot["ticker"]
    book = snapshot["book"]
    bid, ask = float(book["bidPrice"]), float(book["askPrice"])
    if bid <= 0 or ask <= bid:
        return None
    spread_bps = (ask - bid) / ((ask + bid) / 2) * 10_000
    if float(ticker["quoteVolume"]) < cfg.min_quote_volume_usdt or spread_bps > cfg.max_spread_bps:
        return None

    taker = sorted(snapshot["taker"], key=lambda v: int(v["timestamp"]))
    delta = [float(x["buyVol"]) - float(x["sellVol"]) for x in taker]
    if len(delta) < 6:
        return None
    cvd3, cvd6 = sum(delta[-3:]), sum(delta[-6:])
    oi = sorted(snapshot["oi"], key=lambda v: int(v["timestamp"]))
    if len(oi) < 5:
        return None
    oi_change_pct = (float(oi[-1]["sumOpenInterest"]) / float(oi[-5]["sumOpenInterest"]) - 1) * 100

    entry = last1.c  # last CLOSED one-minute candle, not an executable quote
    last5_pivot_high = max(x.h for x in five[-9:-1])
    last5_pivot_low = min(x.l for x in five[-9:-1])
    prev3_1 = one[-4:-1]
    if direction == "SHORT":
        trend = fifteen[-1].c < e21 < e50 and hour[-1].c < e21_hour
        structure = (last5.c < e9_5 and last5.c < last5.o and
                     last5.c < min(x.l for x in five[-4:-1]))
        cvd_oi = cvd3 < 0 and cvd3 < sum(delta[-6:-3]) and oi_change_pct > -0.8
        volume = rvol >= cfg.min_rvol and last5.c < last5.o
        trigger = last1.c < min(x.l for x in prev3_1) and last1.c < last1.o
        stop = price_round(max(last5_pivot_high, last5.h) + a5 * 0.25, tick_size, up=True)
        risk = stop - entry
        tp1 = price_round(entry - risk * 1.5, tick_size, up=False)
        tp2 = price_round(entry - risk * 2.5, tick_size, up=False)
        entry_trigger = min(x.l for x in prev3_1)
    else:
        trend = fifteen[-1].c > e21 > e50 and hour[-1].c > e21_hour
        structure = (last5.c > e9_5 and last5.c > last5.o and
                     last5.c > max(x.h for x in five[-4:-1]))
        cvd_oi = cvd3 > 0 and cvd3 > sum(delta[-6:-3]) and oi_change_pct > -0.8
        volume = rvol >= cfg.min_rvol and last5.c > last5.o
        trigger = last1.c > max(x.h for x in prev3_1) and last1.c > last1.o
        stop = price_round(min(last5_pivot_low, last5.l) - a5 * 0.25, tick_size, up=False)
        risk = entry - stop
        tp1 = price_round(entry + risk * 1.5, tick_size, up=True)
        tp2 = price_round(entry + risk * 2.5, tick_size, up=True)
        entry_trigger = max(x.h for x in prev3_1)

    risk_ok = (0 < risk and cfg.min_stop_atr <= risk / a5 <= cfg.max_stop_atr
               and risk / entry <= 0.018 and
               (entry - tp1 if direction == "SHORT" else tp1 - entry) >= 1.5 * risk - float(tick_size))
    checks = {
        "15m trend": bool(trend),
        "5m structure": bool(structure),
        "CVD + OI": bool(cvd_oi),
        "relative volume": bool(volume),
        "1m trigger": bool(trigger),
        "ATR + RR": bool(risk_ok),
    }
    score = sum(checks.values())
    mandatory = all(checks[k] for k in ("15m trend", "5m structure", "CVD + OI", "1m trigger", "ATR + RR"))
    eligible = mandatory and score >= cfg.score_threshold
    return Signal(symbol, direction, score, checks, entry, stop, tp1, tp2,
                  a5, rvol, cvd3, cvd6, oi_change_pct, spread_bps, tick_size,
                  last1.ts, entry_trigger, eligible)


def evaluate(symbol, snapshot, tick_size, cfg):
    output = []
    for direction in ("LONG", "SHORT"):
        signal = candidate(symbol, direction, snapshot, tick_size, cfg)
        if signal:
            output.append(signal)
    return output
