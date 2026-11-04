"""Pre-delivery price and cost gates shared by primary and shadow cohorts."""
from math import isfinite
from .engine import price_round


def prepare(signal, book, now_ms, cfg):
    bid,ask=float(book["bidPrice"]),float(book["askPrice"])
    if not all(isfinite(x) and x>0 for x in (bid,ask,signal.entry,signal.atr5)) or ask<=bid:
        return "invalid_quote"
    if now_ms-(signal.snapshot_ts+60000)>90000 or now_ms<signal.snapshot_ts+60000:
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
        candle_age_ms=now_ms-(signal.snapshot_ts+60000),
        expires_at_ms=now_ms+cfg.entry_window_minutes*60000,
        price_min=signal.entry-cfg.max_entry_drift_atr*signal.atr5,
        price_max=signal.entry+cfg.max_entry_drift_atr*signal.atr5,
        estimated_cost_r=cost_r)
    signal.journal['parameters']={k:getattr(cfg,k) for k in (
        'score_threshold','min_rvol','min_stop_atr','max_stop_atr',
        'breakout_rvol','min_delta_ratio','min_efficiency','correlation_threshold')}
    return None
