"""Predeclared diagnostics and ranking, never a fitted probability of success."""
from math import log10
from .engine import atr, candles, ema
from .research import correlation, return_series


def enhanced(cfg):
    return getattr(cfg, 'standard_enhanced', False) and cfg.primary_strategy == 'standard'


def regime_context(snapshot):
    bars = candles(snapshot['15m'])
    closes = [b.c for b in bars]
    a = atr(bars)
    averages = ema(closes, 21)
    travel = sum(abs(b-a) for a, b in zip(closes[-21:-1], closes[-20:]))
    efficiency = abs(closes[-1]-closes[-21])/travel if travel else 0
    slope = (averages[-1]-averages[-5])/a if a else 0
    five = candles(snapshot['5m'])
    prior_atr = atr(five[:-3])
    shock_ratio = max(max(b.h-b.l, abs(b.h-p.c), abs(b.l-p.c))
                      for p, b in zip(five[-4:-1], five[-3:])) / prior_atr if prior_atr else 100
    label = 'SHOCK' if shock_ratio >= 4 else 'TREND' if efficiency >= .25 and abs(slope) >= .1 else 'RANGE'
    return dict(regime=label, efficiency=efficiency, slope_atr=slope,
                shock_ratio=shock_ratio, regime_would_block=label != 'TREND')


def exposure(signal, peers, snapshots, cfg):
    """Reserve suggested risk even for unfilled orders; no hedge-netting credit."""
    own = cfg.risk_per_trade_pct
    total = own + sum((p.get('journal_json') or {}).get('risk_pct', .5) for p in peers)
    if total > cfg.max_total_risk_pct + 1e-9:
        return 'total_risk_limit'
    linked = own
    series = return_series(snapshots[signal.symbol])
    for peer in peers:
        snap = snapshots.get(peer['symbol'])
        if not snap:
            return 'correlation_data_missing'
        corr = correlation(series, return_series(snap))
        if corr is None:
            return 'correlation_data_missing'
        if corr * (1 if signal.side == peer['side'] else -1) >= cfg.correlation_threshold:
            linked += (peer.get('journal_json') or {}).get('risk_pct', .5)
    return 'correlated_risk_limit' if linked > cfg.max_correlated_risk_pct + 1e-9 else None


def rank(signal, snapshot, snapshots, peers, cfg):
    p = signal.journal
    context = regime_context(snapshot)
    sign = 1 if signal.side == 'LONG' else -1
    closes = [float(r[4]) for r in snapshot['5m']]
    entry_distance = abs(signal.entry - ema(closes, 21)[-1])/signal.atr5
    # Use only confirmed historical pivots, excluding the trigger candle.
    bars = candles(snapshot['15m'])
    obstacles = []
    for i in range(2, len(bars)-2):
        window = bars[i-2:i+3]
        value = bars[i].h if sign == 1 else bars[i].l
        pivot = value == (max(b.h for b in window) if sign == 1 else min(b.l for b in window))
        if pivot and sign*(value-signal.entry) > 0:
            obstacles.append(sign*(value-signal.entry))
    risk = abs(signal.entry-signal.stop)
    room = min(obstacles)/risk if obstacles else 2.5
    series = return_series(snapshot)
    btc = snapshots.get('BTCUSDT')
    btc_corr = correlation(series, return_series(btc)) if btc else None
    btc_slope = regime_context(btc)['slope_atr'] if btc else 0
    aligned = btc_corr is not None and sign*btc_corr*btc_slope >= 0
    peers_corr = []
    for peer in peers:
        other = snapshots.get(peer['symbol'])
        c = correlation(series, return_series(other)) if other else None
        peers_corr.append(1 if c is None else max(0, c*(1 if signal.side == peer['side'] else -1)))
    cost = p['estimated_cost_r']
    clip = lambda x: max(0., min(1., x))
    components = dict(
        trend=15*clip(sign*context['slope_atr']/.5)*(.5+.5*context['efficiency']),
        structure=15*clip(room/1.5),
        volume=10*clip(signal.rvol/1.5),
        entry=10*clip(1-entry_distance/3),
        liquidity=5*clip(log10(max(1, float(snapshot['ticker']['quoteVolume']))/cfg.min_quote_volume_usdt+1)/2)
            +5*clip(log10(1+p['top_book_notional_usdt']/1000)/3),
        volatility=10*(1 if context['regime']=='TREND' else .4 if context['regime']=='RANGE' else 0),
        net_reward_risk=10*clip((min(1.5, room)-cost)/(1+cost)/1.5),
        costs=10*clip(1-cost/cfg.max_cost_r),
        btc=5*(1 if aligned else .5 if btc_corr is None else 0),
        diversification=5*(1-max(peers_corr, default=0)))
    components = {k:round(v, 3) for k, v in components.items()}
    signal.quality_score = round(sum(components.values()), 2)
    p.update(context, quality_score=signal.quality_score, quality_components=components,
             quality_model='standard-quality-v1', room_to_structure_r=room,
             entry_distance_atr=entry_distance, btc_correlation=btc_corr,
             btc_slope_atr=btc_slope, regime_filter_mode=cfg.regime_filter_mode)
    return signal.quality_score


def risk_plan(entry, stop, cost_r, cfg):
    """Risk budget includes modeled stop costs. No invented account balance."""
    distance = abs(entry-stop)/entry
    multiple = (cfg.risk_per_trade_pct/100)/(distance*(1+cost_r))
    return dict(risk_pct=cfg.risk_per_trade_pct, total_risk_limit_pct=cfg.max_total_risk_pct,
                correlated_risk_limit_pct=cfg.max_correlated_risk_pct,
                daily_loss_limit_pct=cfg.daily_loss_limit_pct,
                notional_equity_multiple=multiple,
                margin_equity_pct={str(l):100*multiple/l for l in (10, 15)},
                risk_note='Illustrative equity proportions; costs/funding/gaps can exceed budget')
