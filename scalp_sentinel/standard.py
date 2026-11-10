"""Two independent, closed-5m-bar scalp setups. No flow or OI dependency."""
from math import isfinite

from .engine import Signal, atr, candles, ema, price_round


FIVE_MINUTES = 300_000


def assess(symbol, snapshot, tick_size, cfg):
    five = candles(snapshot["5m"])
    fifteen = candles(snapshot["15m"])
    if len(five) < 60 or len(fifteen) < 60:
        raise ValueError("insufficient_closed_candles")
    bid = float(snapshot["book"]["bidPrice"])
    ask = float(snapshot["book"]["askPrice"])
    quote_volume = float(snapshot["ticker"]["quoteVolume"])
    if not all(isfinite(v) and v > 0 for v in (bid, ask, quote_volume)) or ask <= bid:
        raise ValueError("invalid_book_or_volume")
    spread = (ask - bid) / ((ask + bid) / 2) * 10_000
    if quote_volume < cfg.min_quote_volume_usdt or spread > cfg.max_spread_bps:
        return [], (["quote_volume"] if quote_volume < cfg.min_quote_volume_usdt else []) + (["spread"] if spread > cfg.max_spread_bps else [])

    closes = [x.c for x in five]
    closes15 = [x.c for x in fifteen]
    e9, e21, e50 = (ema(closes, period)[-1] for period in (9, 21, 50))
    prev9 = ema(closes[:-1], 9)[-1]
    prev21 = ema(closes[:-1], 21)[-1]
    trend21, trend50 = (ema(closes15, period)[-1] for period in (21, 50))
    a5 = atr(five)
    if not isfinite(a5) or a5 <= 0:
        raise ValueError("invalid_atr")
    avg_volume = sum(x.v for x in five[-21:-1]) / 20
    rvol = five[-1].v / avg_volume if avg_volume > 0 else 0
    output = []
    for side in ("LONG", "SHORT"):
        sign = 1 if side == "LONG" else -1
        trend = sign * (fifteen[-1].c - trend21) > 0 and sign * (trend21 - trend50) > 0
        alignment = sign * (e9 - e21) > 0 and sign * (e21 - e50) > 0
        confirmation = (sign * (five[-1].c - five[-1].o) > 0
                        and sign * (five[-1].c - five[-2].c) > 0)
        # A touch of the 21 EMA, followed by a closed candle back across the 9 EMA.
        touched = any(bar.l <= prev21 if sign == 1 else bar.h >= prev21
                      for bar in five[-3:])
        pullback = (touched and sign * (five[-2].c - prev9) <= 0
                    and sign * (five[-1].c - e9) > 0)
        pull_checks = {"15m trend": trend, "5m EMA alignment": alignment,
                       "21 EMA pullback": pullback, "closed 5m reaction": confirmation}
        output.append(_signal(symbol, side, "ema-pullback", five, a5, rvol,
                              spread, tick_size, cfg, pull_checks,
                              "15m EMA trend; 5m EMA21 pullback and EMA9 reclaim"))

        # Breakout candle must precede the retest. The level excludes both.
        breakout = five[-2]
        previous = five[-14:-2]
        level = max(x.h for x in previous) if sign == 1 else min(x.l for x in previous)
        broke = (sign * (breakout.c - level) > 0 and breakout.v >= 1.15 * avg_volume)
        retest = (five[-1].l <= level <= five[-1].c if sign == 1
                  else five[-1].c <= level <= five[-1].h)
        close_near = sign * (five[-1].c - level) <= 1.5 * a5
        breakout_checks = {"15m trend": trend, "volume breakout": broke,
                           "closed 5m retest": retest and close_near,
                           "closed 5m reaction": confirmation}
        output.append(_signal(symbol, side, "breakout-retest", five, a5, rvol,
                              spread, tick_size, cfg, breakout_checks,
                              "15m EMA trend; volume breakout and 5m level retest",
                              level=level))
    return output, []


def _signal(symbol, side, strategy, five, a5, rvol, spread, tick, cfg, checks, reason, level=None):
    sign = 1 if side == "LONG" else -1
    entry = five[-1].c
    recent = five[-4:]
    pivot = min(x.l for x in recent) if sign == 1 else max(x.h for x in recent)
    if level is not None:
        pivot = min(pivot, level) if sign == 1 else max(pivot, level)
    stop_raw = (min(pivot - .15 * a5, entry - 1.0 * a5) if sign == 1
                else max(pivot + .15 * a5, entry + 1.0 * a5))
    stop = price_round(stop_raw, tick, up=sign == -1)
    risk = sign * (entry - stop)
    risk_ok = (isfinite(entry) and entry > 0 and risk > 0
               and cfg.min_stop_atr <= risk / a5 <= cfg.max_stop_atr
               and risk / entry <= .018 and .001 <= a5 / entry <= .025)
    checks["ATR / stop / volatility"] = bool(risk_ok)
    tp1 = price_round(entry + sign * 1.5 * risk, tick, up=sign == 1)
    tp2 = price_round(entry + sign * 2.5 * risk, tick, up=sign == 1)
    signal = Signal(symbol, side, sum(checks.values()), checks, entry, stop, tp1, tp2,
                    a5, rvol, 0, 0, 0, spread, tick, five[-1].ts, entry,
                    all(checks.values()))
    signal.strategy_version = strategy
    signal.setup_id = f"{symbol}:{side}:{strategy}:{five[-1].ts}"
    signal.timeframe_ms = FIVE_MINUTES
    signal.technical_reason = reason
    return signal
