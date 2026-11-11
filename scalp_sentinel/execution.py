"""Pre-delivery price and cost gates shared by primary and shadow cohorts."""
from math import isfinite
from .engine import price_round
from .standard_quality import enhanced, risk_plan


def prepare(signal, book, now_ms, cfg):
    if enhanced(cfg):
        return prepare_standard(signal, book, now_ms, cfg)
    bid,ask=float(book["bidPrice"]),float(book["askPrice"])
    if not all(isfinite(x) and x>0 for x in (bid,ask,signal.entry,signal.atr5)) or ask<=bid:
        return "invalid_quote"
    bar_close=signal.snapshot_ts+getattr(signal,"timeframe_ms",60000)
    if now_ms-bar_close>90000 or now_ms<bar_close:
        return "stale_trigger"
    # USD-M bookTicker supplies transaction time; a missing timestamp cannot
    # prove that a quote is still valid at the delivery decision.
    if book.get("time") is None:
        return "missing_quote_time"
    if not -2000 <= now_ms-int(book["time"]) <= 15000:
        return "stale_quote"
    spread=(ask-bid)/((ask+bid)/2)*10000
    if spread>cfg.max_spread_bps:
        return "spread"
    quote=ask if signal.side=="LONG" else bid
    if abs(quote-signal.entry)>cfg.max_entry_drift_atr*signal.atr5:
        return "price_drift"
    # A price reference rounded to a valid exchange tick, with R recomputed.
    signal.entry=price_round(signal.entry,signal.tick_size,up=signal.side=="SHORT")
    risk=abs(signal.entry-signal.stop)
    sign=1 if signal.side=="LONG" else -1
    if sign*(signal.entry-signal.stop)<=0:
        return "invalid_stop"
    cost_r=2*(cfg.fee_bps+cfg.slippage_bps)/10000*signal.entry/risk
    if cost_r>cfg.max_cost_r:
        return "cost_to_risk"
    signal.tp1=price_round(signal.entry+sign*1.5*risk,signal.tick_size,up=sign==1)
    signal.tp2=price_round(signal.entry+sign*2.5*risk,signal.tick_size,up=sign==1)
    signal.spread_bps=spread
    signal.journal=dict(getattr(signal,"research",{}),cost_model="paper-v1",
        entry_mode="limit_reference",entry_window_ms=cfg.entry_window_minutes*60000,
        max_hold_ms=cfg.max_hold_minutes*60000,invalidate_target_before_entry=True,
        fee_bps=cfg.fee_bps,slippage_bps=cfg.slippage_bps,tp1_fraction=.5,
        quote_at_decision=quote,decision_ms=now_ms,setup_id=getattr(signal,"setup_id",""),
        spread_bps=spread,quote_age_ms=now_ms-int(book['time']),
        candle_age_ms=now_ms-bar_close,
        expires_at_ms=now_ms+cfg.entry_window_minutes*60000,
        price_min=signal.entry-cfg.max_entry_drift_atr*signal.atr5,
        price_max=signal.entry+cfg.max_entry_drift_atr*signal.atr5,
        estimated_cost_r=cost_r)
    signal.journal['parameters']={k:getattr(cfg,k) for k in (
        'score_threshold','min_rvol','min_stop_atr','max_stop_atr',
        'breakout_rvol','min_delta_ratio','min_efficiency','correlation_threshold')}
    return None


def prepare_standard(signal, book, now_ms, cfg):
    """Passive limit reference plus a separately priced market scenario.

    Passive entry has no spread/slippage debit; the exit allowance includes
    half-spread and slippage once. Market entry embeds both in its price.
    Fees are conservatively taker on both legs (not a maker rebate promise).
    """
    try:
        bid, ask, bq, aq = (float(book[k]) for k in ('bidPrice','askPrice','bidQty','askQty'))
        timestamp = int(book['time'])
    except (KeyError, TypeError, ValueError, OverflowError):
        return 'missing_or_invalid_book'
    if not all(isfinite(v) and v > 0 for v in (bid,ask,bq,aq,signal.entry,signal.atr5)) or ask <= bid:
        return 'invalid_quote'
    close = signal.snapshot_ts + signal.timeframe_ms
    if not 0 <= now_ms-close <= 90000:
        return 'stale_trigger'
    if not -2000 <= now_ms-timestamp <= 15000:
        return 'stale_quote'
    spread = (ask-bid)/((ask+bid)/2)*10000
    if spread > cfg.max_spread_bps:
        return 'spread'
    sign = 1 if signal.side == 'LONG' else -1
    reference = getattr(signal, 'reference_entry', signal.entry)
    signal.reference_entry = reference
    quote = ask if sign == 1 else bid
    if abs(quote-reference) > cfg.max_entry_drift_atr*signal.atr5:
        return 'price_drift'
    entry = min(reference, bid) if sign == 1 else max(reference, ask)
    entry = price_round(entry, signal.tick_size, up=sign == -1)
    risk = sign*(entry-signal.stop)
    if not isfinite(risk) or risk <= 0 or not cfg.min_stop_atr <= risk/signal.atr5 <= cfg.max_stop_atr:
        return 'invalid_stop'
    exit_bps = spread/2 + cfg.slippage_bps
    cost_r = (2*cfg.fee_bps+exit_bps)/10000*entry/risk
    if cost_r > cfg.max_cost_r:
        return 'cost_to_risk'
    market = price_round(quote*(1+sign*cfg.slippage_bps/10000), signal.tick_size, up=sign == 1)
    market_risk = sign*(market-signal.stop)
    signal.entry = entry
    signal.tp1 = price_round(entry+sign*1.5*risk, signal.tick_size, up=sign == 1)
    signal.tp2 = price_round(entry+sign*2.5*risk, signal.tick_size, up=sign == 1)
    signal.spread_bps = spread
    signal.journal = dict(getattr(signal,'research',{}), cost_model='paper-v2',
        entry_mode='passive_limit', entry_window_ms=cfg.entry_window_minutes*60000,
        max_hold_ms=cfg.max_hold_minutes*60000, invalidate_target_before_entry=True,
        fee_bps=cfg.fee_bps, slippage_bps=cfg.slippage_bps, exit_friction_bps=exit_bps,
        tp1_fraction=.5, decision_ms=now_ms, setup_id=signal.setup_id,
        quote_at_decision=quote, reference_entry=reference, spread_bps=spread,
        bid=bid, ask=ask, bid_qty=bq, ask_qty=aq,
        top_book_notional_usdt=min(bid*bq, ask*aq),
        quote_age_ms=now_ms-timestamp, candle_age_ms=now_ms-close,
        expires_at_ms=now_ms+cfg.entry_window_minutes*60000,
        price_min=reference-cfg.max_entry_drift_atr*signal.atr5,
        price_max=reference+cfg.max_entry_drift_atr*signal.atr5,
        fill_delay_ms=30000, fill_penetration=float(signal.tick_size), tick_size=signal.tick_size,
        estimated_cost_r=cost_r, estimated_net_tp1_rr=(1.5-cost_r)/(1+cost_r),
        market_scenario=dict(entry=market, reference_drift_bps=sign*(market-reference)/reference*10000,
            cost_r=(2*cfg.fee_bps+exit_bps)/10000*market/market_risk if market_risk>0 else None,
            note='Indicative quote; delayed fill is tracked separately; no guaranteed execution'))
    signal.journal.update(risk_plan(entry,signal.stop,cost_r,cfg))
    return None
