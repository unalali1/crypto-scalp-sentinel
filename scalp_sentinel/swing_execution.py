"""Paper-only swing entry and cost gates; no order or Telegram capability."""
from math import isfinite

from .engine import price_round
from .swing_engine import FOUR_HOURS

ENTRY_WINDOW_MS=2*3_600_000
MAX_HOLD_MS=5*24*3_600_000


def prepare_swing(setup,book,now_ms,fee_bps=5.0,slippage_bps=2.0):
    if not 0<=now_ms-(setup.snapshot_ts+FOUR_HOURS)<=20*60_000:
        return "stale_4h_close"
    if book.get("time") is None:
        return "missing_quote_time"
    try:
        bid,ask=float(book["bidPrice"]),float(book["askPrice"])
        quote_time=int(book["time"])
    except (KeyError,TypeError,ValueError):
        return "invalid_quote"
    if not all(isfinite(x) and x>0 for x in (bid,ask,setup.entry,setup.atr4)) or ask<=bid:
        return "invalid_quote"
    if not -2000<=now_ms-quote_time<=15_000:
        return "stale_quote"
    spread=(ask-bid)/((ask+bid)/2)*10_000
    if spread>8:
        return "spread"
    quote=ask if setup.side=="LONG" else bid
    if abs(quote-setup.entry)>.25*setup.atr4:
        return "price_drift"
    sign=1 if setup.side=="LONG" else -1
    setup.entry=price_round(setup.entry,setup.tick_size,up=sign<0)
    risk=sign*(setup.entry-setup.stop)
    if risk<=0:
        return "invalid_stop"
    cost_r=2*(fee_bps+slippage_bps)/10000*setup.entry/risk
    if cost_r>.15:
        return "cost_to_risk"
    setup.tp1=price_round(setup.entry+sign*1.5*risk,setup.tick_size,up=sign>0)
    setup.tp2=price_round(setup.entry+sign*3*risk,setup.tick_size,up=sign>0)
    setup.spread_bps=spread
    setup.journal=dict(cost_model="paper-swing-v1",regime="trend",
        setup_id=setup.setup_id,entry_mode="limit_reference",
        entry_window_ms=ENTRY_WINDOW_MS,max_hold_ms=MAX_HOLD_MS,
        expires_at_ms=now_ms+ENTRY_WINDOW_MS,
        invalidate_target_before_entry=True,tp1_fraction=.5,
        fee_bps=fee_bps,slippage_bps=slippage_bps,
        estimated_cost_r=cost_r,quote_at_decision=quote,decision_ms=now_ms,
        price_min=setup.entry-.25*setup.atr4,
        price_max=setup.entry+.25*setup.atr4,
        risk_fraction_reference=.005,portfolio_risk_fraction_reference=.01,
        daily_ts=None,trigger_4h_ts=setup.snapshot_ts,
        rvol4=setup.rvol4,spread_bps=spread)
    return None
