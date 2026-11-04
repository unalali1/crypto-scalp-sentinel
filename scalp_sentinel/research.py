"""Frozen V3 hypothesis, initially shadow-only. No fitted win probabilities."""
from copy import deepcopy
from math import sqrt
from .engine import candles, ema, atr, _v2_candidate, confirm_15m_close


def regime(snapshot):
    bars = candles(snapshot["15m"])
    closes = [b.c for b in bars]
    a = atr(bars)
    moves = [abs(b-a) for a,b in zip(closes[-21:-1],closes[-20:])]
    efficiency = abs(closes[-1]-closes[-21])/sum(moves) if sum(moves) else 0
    avg = ema(closes,21)
    slope = (avg[-1]-avg[-5])/a if a else 0
    signs = [c-e for c,e in zip(closes[-12:],avg[-12:])]
    crosses = sum(a*b < 0 for a,b in zip(signs,signs[1:]))
    one = candles(snapshot["1m"])
    previous_atr = atr(one[:-1])
    shock = previous_atr <= 0 or max(b.h-b.l for b in one[-3:]) > 3*previous_atr
    label = "shock" if shock else "trend" if efficiency >= .25 and abs(slope) >= .1 and crosses <= 3 else "range"
    return dict(regime=label, efficiency=efficiency, slope_atr=slope, crosses=crosses, shock=shock)


def delta_ratio(rows):
    buy = sum(float(x["buyVol"]) for x in rows)
    sell = sum(float(x["sellVol"]) for x in rows)
    return (buy-sell)/(buy+sell) if buy+sell else 0


def candidate_v3(symbol, side, snapshot, tick, cfg, btc):
    base = _v2_candidate(symbol,side,snapshot,tick,cfg)
    if base is None:
        return None
    s = deepcopy(base)
    sign = 1 if side == "LONG" else -1
    context = regime(snapshot)
    s.strategy_version = "v3"
    five, one = candles(snapshot["5m"]), candles(snapshot["1m"])
    idx = next((i for i,b in enumerate(five) if b.ts == base.breakout_ts), None)
    breakout_volume = 0.0
    phase_volume = False
    if idx is not None and idx >= 20:
        avg = sum(b.v for b in five[idx-20:idx])/20
        breakout_volume = five[idx].v/avg if avg else 0
        # Only post-breakout bars near the broken level belong to retest.
        retest = [b for b in one[-6:-1] if b.ts >= base.breakout_ts+300000
                  and (abs(b.l-base.entry_trigger) <= .5*base.atr5 if sign==1
                       else abs(b.h-base.entry_trigger) <= .5*base.atr5)]
        retest_avg = sum(b.v for b in retest)/len(retest) if retest else 0
        phase_volume = (breakout_volume >= cfg.breakout_rvol and retest_avg > 0
                        and retest_avg <= five[idx].v/5 and one[-1].v >= retest_avg)
    flow = snapshot["taker"]
    current, previous = delta_ratio(flow[-3:]), delta_ratio(flow[-6:-3])
    oi = snapshot.get("oi5", [])
    oi15 = None
    if len(oi) >= 4:
        oi15 = (float(oi[-1]["sumOpenInterest"])/float(oi[-4]["sumOpenInterest"])-1)*100
    # Do not discard liquidation/covering regimes blindly: label them and
    # measure separately. Large OI contraction is a shock veto, not direction.
    flow_ok = sign*current >= cfg.min_delta_ratio and sign*(current-previous) > 0
    flow_ok = flow_ok and oi15 is not None and oi15 >= -.5
    btc_context = regime(btc) if btc else None
    btc_ok = bool(btc_context and not btc_context["shock"]
                  and sign*btc_context["slope_atr"] > -.1)
    checks = dict(s.checks)
    checks["relative volume"] = phase_volume
    checks["CVD + OI"] = flow_ok
    checks["market regime"] = (not context["shock"] and context["efficiency"] >= cfg.min_efficiency
                               and sign*context["slope_atr"] >= .1 and context["crosses"] <= 3)
    checks["BTC context"] = btc_ok
    s.checks = checks
    s.score = sum(checks.values())
    s.eligible = all(checks.values())
    s.research = dict(context, breakout_rvol=breakout_volume, delta_ratio=current,
                      oi15_pct=oi15, oi_regime="expansion" if oi15 is not None and oi15 >= 0 else "contraction")
    s.research.update({k:snapshot.get(k) for k in ('flow_source','oi_age_ms','oi5_age_ms')})
    return s


def v2_shadow_variants(base, snapshot, now_ms):
    """Predeclared V2.1 filters. They only record paper outcomes, never alerts."""
    if not base.eligible:
        return []
    variants = []
    hypotheses = (
        ("v2.1-volume", bool(base.checks["relative volume"]), "volume_required"),
        ("v2.1-trend", base.research["regime"] == "trend", "trend_regime_required"),
    )
    for version, passed, hypothesis in hypotheses:
        if passed:
            variant = deepcopy(base)
            variant.strategy_version = version
            variant.setup_id = f"{base.setup_id}:{hypothesis}"
            variant.research["hypothesis"] = hypothesis
            variants.append(variant)
    confirmed, closed_at = confirm_15m_close(base.side, snapshot, now_ms)
    if confirmed:
        variant = deepcopy(base)
        variant.strategy_version = "v2.1-15m-confirm"
        variant.setup_id = f"{base.setup_id}:15m-close:{closed_at}"
        variant.checks["15m close confirmation"] = True
        variant.score += 1
        variant.research["hypothesis"] = "15m_close_confirmation"
        variants.append(variant)
    return variants


def return_series(snapshot):
    bars = snapshot["5m"][-61:]
    return {int(b[0]):float(b[4])/float(a[4])-1 for a,b in zip(bars,bars[1:])}


def correlation(a, b):
    keys=sorted(a.keys() & b.keys())
    if len(keys)<30:
        return None
    x,y=[a[k] for k in keys],[b[k] for k in keys]
    mx,my=sum(x)/len(x),sum(y)/len(y)
    cov=sum((u-mx)*(v-my) for u,v in zip(x,y))
    scale=sqrt(sum((u-mx)**2 for u in x)*sum((v-my)**2 for v in y))
    return cov/scale if scale else None


def correlated(signal, peers, snapshots, threshold):
    """Side-adjusted correlation: opposing positions in inverse coins count too."""
    series = return_series(snapshots[signal.symbol])
    for peer in peers:
        if peer["symbol"] == signal.symbol:
            return True
        if peer["symbol"] not in snapshots:
            return True  # unknown open exposure fails closed
        corr = correlation(series, return_series(snapshots[peer["symbol"]]))
        if corr is None or corr*(1 if peer["side"]==signal.side else -1) >= threshold:
            return True
    return False
