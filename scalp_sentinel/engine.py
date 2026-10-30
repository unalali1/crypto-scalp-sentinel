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

# V2 is deliberately separate from the original six-check engine, enabling
# deterministic regression comparisons and immediate code rollback.
candidate_v1 = candidate
FIVE_MIN_MS = 5 * 60_000


def _clear_path(hourly, direction, entry, risk, tp1):
    """Block a TP1 obstructed by a confirmed, nearby hourly swing level."""
    if len(hourly) < 8:
        return False
    for i in range(2, len(hourly) - 2):
        window = hourly[i - 2:i + 3]
        if direction == "LONG":
            level = hourly[i].h
            confirmed = level >= max(x.h for j, x in enumerate(window) if j != 2)
            obstacle = entry + 0.35 * risk < level < tp1 + 0.05 * risk
        else:
            level = hourly[i].l
            confirmed = level <= min(x.l for j, x in enumerate(window) if j != 2)
            obstacle = tp1 - 0.05 * risk < level < entry - 0.35 * risk
        if confirmed and obstacle:
            return False
    return True


def _v2_candidate(symbol, direction, snapshot, tick_size, cfg):
    baseline = candidate_v1(symbol, direction, snapshot, tick_size, cfg)
    if baseline is None:
        return None

    one = candles(snapshot["1m"])
    five = candles(snapshot["5m"])
    fifteen = candles(snapshot["15m"])
    hour = candles(snapshot["1h"])
    four = candles(snapshot["4h"])
    a5 = baseline.atr5
    if a5 <= 0:
        return None
    bullish = direction == "LONG"
    sign = 1 if bullish else -1
    e21_15, e50_15 = ema([x.c for x in fifteen], 21)[-1], ema([x.c for x in fifteen], 50)[-1]
    e21_h = ema([x.c for x in hour], 21)[-1]
    e21_4, e50_4 = ema([x.c for x in four], 21)[-1], ema([x.c for x in four], 50)[-1]
    e9_5 = ema([x.c for x in five], 9)[-1]
    last1, last5 = one[-1], five[-1]

    trend = (
        sign * (fifteen[-1].c - e21_15) > 0
        and sign * (e21_15 - e50_15) > 0
        and sign * (hour[-1].c - e21_h) > 0
        and sign * (four[-1].c - e21_4) > 0
        and sign * (e21_4 - e50_4) > 0
    )

    # The base swing excludes the latest four 5m candles. A breakout must be
    # completed in one of the preceding three CLOSED bars (never an open bar).
    prior = five[-16:-4]
    level = max(x.h for x in prior) if bullish else min(x.l for x in prior)
    recent = five[-4:-1]
    breakout = next((bar for bar in recent if sign * (bar.c - level) >= 0.08 * a5), None)
    structure = bool(breakout and sign * (last5.c - level) > 0
                     and sign * (last5.c - e9_5) > 0)

    # Retest must occur AFTER the 5m breakout candle has closed, not as part
    # of the breakout itself. The last closed 1m candle rejects that level.
    after_break = breakout.ts + FIVE_MIN_MS if breakout else 0
    retest = [
        bar for bar in one[-6:-1]
        if bar.ts >= after_break
        and (bar.l <= level + 0.20 * a5 and bar.l >= level - 0.50 * a5
             if bullish else
             bar.h >= level - 0.20 * a5 and bar.h <= level + 0.50 * a5)
        and sign * (bar.c - level) > 0
    ]
    distance = sign * (last1.c - level)
    trigger = bool(structure and retest
                   and last1.ts >= after_break
                   and sign * (last1.c - last1.o) > 0
                   and sign * (last1.c - one[-2].c) > 0
                   and 0.05 * a5 <= distance <= 0.85 * a5)

    # Require increasing open interest for shorts to avoid thin, covering-led
    # dumps. A small OI decrease remains acceptable on long pullbacks.
    previous_delta = baseline.cvd6 - baseline.cvd3
    cvd_oi = bool(
        sign * baseline.cvd3 > 0
        and sign * (baseline.cvd3 - previous_delta) > 0
        and baseline.oi_change_pct >= (0.15 if not bullish else -0.35)
    )
    volume = baseline.rvol >= cfg.min_rvol

    # Structural stop below/above the recent 5m pullback AND beyond the
    # broken level, padded with ATR. Reject poor R geometry and congestion.
    swing = min(x.l for x in five[-4:]) if bullish else max(x.h for x in five[-4:])
    if bullish:
        stop_raw = min(swing, level - 0.45 * a5) - 0.20 * a5
    else:
        stop_raw = max(swing, level + 0.45 * a5) + 0.20 * a5
    stop = price_round(stop_raw, tick_size, up=not bullish)
    entry = last1.c
    risk = sign * (entry - stop)
    tp1 = price_round(entry + sign * risk * 1.5, tick_size, up=bullish)
    tp2 = price_round(entry + sign * risk * 2.5, tick_size, up=bullish)
    risk_ok = bool(
        risk > 0
        and cfg.min_stop_atr <= risk / a5 <= cfg.max_stop_atr
        and risk / entry <= 0.018
        and _clear_path(hour[-30:-1], direction, entry, risk, tp1)
    )

    checks = {
        "15m trend": bool(trend),     # Includes 1h and 4h agreement.
        "5m structure": bool(structure),
        "CVD + OI": bool(cvd_oi),
        "relative volume": bool(volume),
        "1m trigger": bool(trigger),  # Confirmed post-breakout retest.
        "ATR + RR": bool(risk_ok),    # Includes hourly obstacle check.
    }
    score = sum(checks.values())
    mandatory = all(checks[k] for k in (
        "15m trend", "5m structure", "CVD + OI", "1m trigger", "ATR + RR"))
    # Under-confirmed shorts are suspended, not sent as tradeable alerts.
    short_liquidity_ok = bullish or baseline.rvol >= 1.0
    eligible = mandatory and short_liquidity_ok and score >= cfg.score_threshold
    baseline.score, baseline.checks = score, checks
    baseline.entry, baseline.stop = entry, stop
    baseline.tp1, baseline.tp2 = tp1, tp2
    baseline.entry_trigger, baseline.eligible = level, eligible
    baseline.strategy_version = "v2"
    baseline.setup_id = f"{symbol}:{direction}:{breakout.ts}:{level}" if breakout else ""
    baseline.breakout_ts = breakout.ts if breakout else None
    return baseline


def candidate(symbol, direction, snapshot, tick_size, cfg):
    if not getattr(cfg, "v2_enabled", False):
        return candidate_v1(symbol, direction, snapshot, tick_size, cfg)
    return _v2_candidate(symbol, direction, snapshot, tick_size, cfg)

