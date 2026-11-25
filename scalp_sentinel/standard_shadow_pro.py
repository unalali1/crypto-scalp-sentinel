"""Improved shadow-only 5m scalp setups with regime, BTC, flow and structure gates."""
from math import isfinite

from .engine import Signal, atr, candles, ema, price_round
from .research import correlation, delta_ratio, return_series
from .standard_quality import regime_context

FIVE_MINUTES = 300_000
PRO_COHORT = "standard-pro-v1"


def _btc_aligned(side, symbol, btc_snapshot):
    """Require BTC not to be in shock or moving sharply against the trade side."""
    if not btc_snapshot or len(btc_snapshot.get("5m") or []) < 20 or len(btc_snapshot.get("15m") or []) < 25:
        return False, "missing_btc"
    btc_ctx = regime_context(btc_snapshot)
    if btc_ctx["regime"] == "SHOCK":
        return False, "btc_shock"
    if symbol == "BTCUSDT":
        return True, "self"
    sign = 1 if side == "LONG" else -1
    if sign * btc_ctx["slope_atr"] < -0.10:
        return False, "btc_15m_opposing"
    btc_5m = candles(btc_snapshot["5m"])
    btc_a5 = atr(btc_5m)
    last = btc_5m[-1]
    if btc_a5 > 0:
        impulse = sign * (last.c - last.o) / btc_a5
        taker = btc_snapshot.get("taker") or []
        btc_delta = delta_ratio(taker[-1:]) if taker else 0.0
        if impulse < -1.2 and sign * btc_delta < -0.15:
            return False, "btc_5m_impulse_opposing"
    return True, "aligned"


def _taker_flow(snapshot, side, min_bar_delta, min_three_delta):
    """Validate closed-bar taker buy/sell imbalance from kline taker volume."""
    taker = snapshot.get("taker") or []
    if len(taker) < 6:
        return False, 0.0, 0.0
    sign = 1 if side == "LONG" else -1
    d1 = delta_ratio(taker[-1:])
    d3 = delta_ratio(taker[-3:])
    ok = sign * d1 >= min_bar_delta and sign * d3 >= min_three_delta
    return ok, d1, d3


def _confirmed_obstacles(bars, side, entry):
    """Return distances from entry to confirmed 5-bar swing levels ahead of price."""
    if len(bars) < 10:
        return []
    sign = 1 if side == "LONG" else -1
    distances = []
    for i in range(2, len(bars) - 2):
        window = bars[i - 2:i + 3]
        val = bars[i].h if sign == 1 else bars[i].l
        is_pivot = val == (max(b.h for b in window) if sign == 1 else min(b.l for b in window))
        if is_pivot and sign * (val - entry) > 0:
            distances.append(sign * (val - entry))
    return distances


def _room_r(side, entry, risk, snapshot):
    """Compute distance in R to the nearest confirmed 15m or 1h structural obstacle."""
    if risk <= 0:
        return 0.0
    obstacles = _confirmed_obstacles(candles(snapshot["15m"]), side, entry)
    hourly_rows = snapshot.get("1h") or []
    if len(hourly_rows) >= 20:
        obstacles.extend(_confirmed_obstacles(candles(hourly_rows), side, entry))
    return min(obstacles) / risk if obstacles else 3.0


def _build_signal(symbol, side, strategy, five, a5, rvol, spread, tick, stop_raw,
                  checks, reason, d1, d3, ctx, room, entry_at_book=False):
    sign = 1 if side == "LONG" else -1
    entry = five[-1].c
    stop = price_round(stop_raw, tick, up=sign == -1)
    risk = sign * (entry - stop)
    risk_ok = (isfinite(entry) and entry > 0 and risk > 0
               and 0.55 <= risk / a5 <= 2.50
               and risk / entry <= 0.015 and 0.001 <= a5 / entry <= 0.025)
    checks["ATR / stop / volatility"] = bool(risk_ok)
    checks["structure room"] = room >= 1.15
    tp1_r, tp2_r = 1.1, 2.0
    tp1 = price_round(entry + sign * tp1_r * risk, tick, up=sign == 1)
    tp2 = price_round(entry + sign * tp2_r * risk, tick, up=sign == 1)
    eligible = all(checks.values())
    signal = Signal(symbol, side, sum(checks.values()), checks, entry, stop, tp1, tp2,
                    a5, rvol, d1, d3, 0.0, spread, tick, five[-1].ts, entry, eligible)
    signal.strategy_version = strategy
    signal.setup_id = f"{symbol}:{side}:{strategy}:{five[-1].ts}"
    signal.timeframe_ms = FIVE_MINUTES
    signal.technical_reason = reason
    signal.tp1_r = tp1_r
    signal.tp2_r = tp2_r
    signal.entry_at_book = entry_at_book
    signal.research = dict(
        ctx,
        pro_setup=strategy,
        taker_delta_1=round(d1, 4),
        taker_delta_3=round(d3, 4),
        room_to_structure_r=round(room, 3),
        strategy_revision="standard-pro-v1",
    )
    return signal


def assess_pro(symbol, snapshot, tick_size, btc_snapshot, cfg):
    """Evaluate improved shadow scalp setups with strict flow, BTC, regime and room gates."""
    if snapshot.get("asof_ms") is not None:
        now = snapshot["asof_ms"]
        for tf, duration in (("5m", 300000), ("15m", 900000)):
            rows = snapshot[tf]
            if any(int(r[6]) + 250 > now for r in rows) or int(rows[-1][0]) != now // duration * duration - duration:
                raise ValueError("unclosed_or_stale_candles")
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
    min_vol = getattr(cfg, "pro_min_quote_volume_usdt", 25_000_000.0)
    max_spread = getattr(cfg, "pro_max_spread_bps", 5.0)
    prefilter = []
    if quote_volume < min_vol:
        prefilter.append("pro_quote_volume")
    if spread > max_spread:
        prefilter.append("pro_spread")
    if len(snapshot.get("taker") or []) < 6:
        prefilter.append("pro_missing_taker")
    if prefilter:
        return [], prefilter

    a5 = atr(five)
    if not isfinite(a5) or a5 <= 0:
        raise ValueError("invalid_atr")
    ctx = regime_context(snapshot)
    if ctx["regime"] == "SHOCK":
        return [], ["pro_shock_regime"]

    closes5 = [b.c for b in five]
    closes15 = [b.c for b in fifteen]
    e21_5 = ema(closes5, 21)[-1]
    e21_15, e50_15 = ema(closes15, 21)[-1], ema(closes15, 50)[-1]
    hourly_rows = snapshot.get("1h") or []
    hour = candles(hourly_rows) if len(hourly_rows) >= 30 else []
    e21_1h = ema([b.c for b in hour], 21)[-1] if hour else None

    avg_vol = sum(b.v for b in five[-21:-1]) / 20
    last5 = five[-1]
    rvol = last5.v / avg_vol if avg_vol > 0 else 0.0
    span = last5.h - last5.l
    close_loc = (last5.c - last5.l) / span if span > 0 else 0.5
    body_ratio = abs(last5.c - last5.o) / span if span > 0 else 0.0

    # Compression baseline: median range of the 8 bars preceding the trigger bar.
    prior_ranges = sorted(b.h - b.l for b in five[-9:-1])
    median_prior_range = (prior_ranges[3] + prior_ranges[4]) / 2

    output = []
    for side in ("LONG", "SHORT"):
        sign = 1 if side == "LONG" else -1
        btc_ok, btc_reason = _btc_aligned(side, symbol, btc_snapshot)

        # Setup A: Liquidity Sweep & Reclaim (SFP)
        prior_box = five[-22:-2]
        sweep_ref = min(b.l for b in prior_box) if sign == 1 else max(b.h for b in prior_box)
        if sign == 1:
            swept = min(b.l for b in five[-2:]) < sweep_ref - 0.05 * a5
            reclaimed = last5.c > sweep_ref + 0.05 * a5 and close_loc >= 0.65 and last5.c > last5.o
            sfp_stop = min(b.l for b in five[-2:]) - 0.15 * a5
        else:
            swept = max(b.h for b in five[-2:]) > sweep_ref + 0.05 * a5
            reclaimed = last5.c < sweep_ref - 0.05 * a5 and close_loc <= 0.35 and last5.c < last5.o
            sfp_stop = max(b.h for b in five[-2:]) + 0.15 * a5
        sfp_vol = max(five[-1].v, five[-2].v) >= 1.35 * avg_vol if avg_vol > 0 else False
        flow_sfp, d1, d3 = _taker_flow(snapshot, side, 0.08, 0.03)
        not_runaway_against = ctx["regime"] == "RANGE" or sign * ctx["slope_atr"] >= -0.20
        sfp_room = _room_r(side, last5.c, max(1e-12, abs(last5.c - sfp_stop)), snapshot)
        sfp_checks = {
            "liquidity sweep": bool(swept and reclaimed),
            "sweep volume": bool(sfp_vol),
            "taker flow": bool(flow_sfp),
            "btc alignment": bool(btc_ok),
            "regime alignment": bool(not_runaway_against),
        }
        sfp_sig = _build_signal(
            symbol, side, "pro-sfp-reclaim", five, a5, rvol, spread, tick_size,
            sfp_stop, sfp_checks, f"5m liquidity sweep & reclaim; btc={btc_reason}",
            d1, d3, ctx, sfp_room, entry_at_book=False,
        )
        sfp_sig.max_cost_r_override = cfg.pro_max_cost_r
        sfp_sig.max_hold_minutes_override = cfg.pro_max_hold_minutes
        sfp_sig.max_spread_bps_override = max_spread
        output.append(sfp_sig)

        # Setup B: Volatility Compression -> Expansion Breakout (TREND regime only)
        trend_15m = sign * (fifteen[-1].c - e21_15) > 0 and sign * (e21_15 - e50_15) > 0
        trend_1h = True if e21_1h is None else sign * (hour[-1].c - e21_1h) > 0
        compressed = median_prior_range <= 0.90 * a5
        consolidation = five[-13:-1]
        break_level = max(b.h for b in consolidation) if sign == 1 else min(b.l for b in consolidation)
        broke = sign * (last5.c - break_level) >= 0.05 * a5
        strong_bar = ((close_loc >= 0.70 if sign == 1 else close_loc <= 0.30)
                      and body_ratio >= 0.45 and sign * (last5.c - last5.o) > 0)
        not_extended = abs(last5.c - e21_5) <= 2.2 * a5
        flow_vcp, d1_vcp, d3_vcp = _taker_flow(snapshot, side, 0.10, 0.04)
        vcp_stop = (min(b.l for b in five[-4:]) - 0.15 * a5 if sign == 1
                    else max(b.h for b in five[-4:]) + 0.15 * a5)
        vcp_room = _room_r(side, last5.c, max(1e-12, abs(last5.c - vcp_stop)), snapshot)
        vcp_checks = {
            "trend regime": bool(ctx["regime"] == "TREND" and trend_15m and trend_1h),
            "volatility compression": bool(compressed),
            "expansion breakout": bool(broke and strong_bar and not_extended),
            "breakout volume": bool(rvol >= 1.40),
            "taker flow": bool(flow_vcp),
            "btc alignment": bool(btc_ok),
        }
        vcp_sig = _build_signal(
            symbol, side, "pro-vcp-trend", five, a5, rvol, spread, tick_size,
            vcp_stop, vcp_checks, f"TREND 15m/1h aligned compression breakout; btc={btc_reason}",
            d1_vcp, d3_vcp, ctx, vcp_room, entry_at_book=True,
        )
        vcp_sig.max_cost_r_override = cfg.pro_max_cost_r
        vcp_sig.max_hold_minutes_override = cfg.pro_max_hold_minutes
        vcp_sig.max_spread_bps_override = max_spread
        output.append(vcp_sig)

    return output, []


def pro_priority_key(signal):
    """Deterministic cross-universe ranking key for eligible pro shadow setups."""
    j = getattr(signal, "journal", {}) or {}
    return (
        -round(float(j.get("quality_score", 0.0)), 4),
        float(j.get("estimated_cost_r", 1.0)),
        -float(signal.rvol),
        signal.symbol,
        signal.strategy_version,
    )


def select_pro_candidates(ready_signals, active_peers, snapshots, threshold):
    """Select uncorrelated pro shadow trades; never stack same-symbol or correlated bets."""
    selected = []
    seen_symbols = {p["symbol"] for p in active_peers}
    virtual_peers = list(active_peers)
    for sig in sorted(ready_signals, key=pro_priority_key):
        if sig.symbol in seen_symbols:
            yield sig, "pro_symbol_active"
            continue
        series = return_series(snapshots[sig.symbol]) if sig.symbol in snapshots else None
        if not series:
            yield sig, "pro_missing_series"
            continue
        is_corr = False
        for peer in virtual_peers:
            other_snap = snapshots.get(peer["symbol"])
            if not other_snap:
                is_corr = True
                break
            c = correlation(series, return_series(other_snap))
            if c is None or c * (1 if peer["side"] == sig.side else -1) >= threshold:
                is_corr = True
                break
        if is_corr:
            yield sig, "pro_correlated_cluster"
            continue
        seen_symbols.add(sig.symbol)
        virtual_peers.append({"symbol": sig.symbol, "side": sig.side})
        selected.append(sig)
        yield sig, None
