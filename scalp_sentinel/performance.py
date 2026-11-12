"""Paper economics. R is initial price risk, never leveraged account return."""
from collections import defaultdict


def outcome_bounds(trade, close):
    p = trade.get("journal_json") or {}
    entry, stop = trade["entry_price"], trade["stop_price"]
    risk = abs(entry - stop)
    side = -1 if trade["side"] == "SHORT" else 1
    fraction = p.get("tp1_fraction", 0.5)
    r1 = side * (trade["tp1_price"] - entry) / risk
    r2 = side * (trade["tp2_price"] - entry) / risk
    full = fraction * r1 + (1 - fraction) * r2
    partial = fraction * r1
    mark = side * (close - entry) / risk
    exit_r=side*(p.get("exit_price",stop)-entry)/risk
    remainder_r=side*(p.get("exit_price",entry)-entry)/risk
    states = {
        "STOPPED": (exit_r, exit_r), "TP2_HIT": (full, full),
        "TP1_THEN_BREAKEVEN": (partial+(1-fraction)*remainder_r,) * 2,
        "TP1_THEN_STOP": (partial+(1-fraction)*exit_r,) * 2,
        "TIMEOUT": (mark, mark),
        "TP1_TIMEOUT": (partial + (1-fraction)*mark,) * 2,
        "AMBIGUOUS_ENTRY": (-1, full), "AMBIGUOUS_EXIT": (-1, full),
        "TP1_AMBIGUOUS_EXIT": (partial, full),
    }
    if trade["status"] not in states:
        return {"net_r_low": None, "net_r_high": None, "costs_complete": True}
    low, high = states[trade["status"]]
    if trade['status']=='AMBIGUOUS_ENTRY' and p.get('ambiguous_gap_exit') is not None:
        low=min(low,side*(p['ambiguous_gap_exit']-entry)/risk)
    # Explicit estimated notional-cost allowance on both sides. Funding
    # is resolved separately from exchange history, never silently assumed 0.
    cost = 2 * (p.get("fee_bps", 5) + p.get("slippage_bps", 2)) / 10000 * entry / risk
    if p.get('cost_model') == 'paper-v2':
        # Gross R already contains the delayed market entry friction, if any.
        # Weighted exit notional follows from the price-only gross return.
        # For ambiguous exits charge the larger notional to both bounds.
        exit_notional=max(entry+side*low*risk,entry+side*high*risk)
        cost=(entry*p['fee_bps']+exit_notional*(p['fee_bps']+p['exit_friction_bps']))/10000/risk
    return {"gross_r_low": low, "gross_r_high": high,
            "execution_cost_r": cost, "net_r_low": low-cost,
            "net_r_high": high-cost, "costs_complete": False}


def settle_funding(trade, events):
    """Apply historical rates to remaining notional at each funding timestamp."""
    p = trade["journal_json"]
    if p.get("net_r_low") is None:
        return
    start = trade.get("filled_at_ms") or trade["sent_at_ms"]
    end = trade["finished_at_ms"] + 60000
    risk = abs(trade["entry_price"] - trade["stop_price"])
    sign = -1 if trade["side"] == "SHORT" else 1
    # Same-minute fills/exits/TP1 cannot be ordered against funding either.
    low_cost = high_cost = 0.0
    for event in events:
        ts = int(event["fundingTime"])
        if not start <= ts < end:
            continue
        value = sign * float(event["fundingRate"]) * float(event["markPrice"]) / risk
        tp = trade.get("tp1_at_ms")
        fraction = 1-p.get("tp1_fraction", 0.5) if tp is not None and ts >= tp+60000 else 1
        value *= fraction
        uncertain = (ts < start+60000 or ts >= trade["finished_at_ms"]
                     or (tp is not None and tp <= ts < tp+60000)
                     or "AMBIGUOUS" in trade["status"])
        low_cost += min(0, value) if uncertain else value
        high_cost += max(0, value) if uncertain else value
    p["funding_r_low"], p["funding_r_high"] = low_cost, high_cost
    p["net_r_low"] = p["gross_r_low"] - p["execution_cost_r"] - high_cost
    p["net_r_high"] = p["gross_r_high"] - p["execution_cost_r"] - low_cost
    p["costs_complete"] = True


def summarize(trades):
    groups = defaultdict(list)
    for t in trades:
        p = t.get("journal_json") or {}
        key = (t.get("strategy_version"), t.get("symbol"), t.get("side"), p.get("regime", "unknown"))
        groups[key].append(t)
    result = []
    for key, rows in sorted(groups.items()):
        closed = sorted((t for t in rows if t['status'] not in ('AWAITING_ENTRY','OPEN','TP1_HIT','NO_ENTRY','INVALIDATED')
                         and (t.get("journal_json") or {}).get("costs_complete")
                         and (t.get("journal_json") or {}).get("net_r_low") is not None),
                        key=lambda t: (t.get("finished_at_ms") or 0, t.get("sent_at_ms", 0)))
        low = [t["journal_json"]["net_r_low"] for t in closed]
        high = [t["journal_json"]["net_r_high"] for t in closed]
        # A signal with no entry earns zero, while unresolved/data-gap signals
        # must not be silently counted as either a win or a loss.
        no_entry = [t for t in rows if t["status"] in ("NO_ENTRY", "INVALIDATED")]
        decided = len(closed) + len(no_entry)
        equity = peak = dd = streak = max_streak = 0
        for r in low:
            equity += r
            peak = max(peak, equity)
            dd = max(dd, peak-equity)
            streak = streak+1 if r < 0 else 0
            max_streak = max(max_streak, streak)
        gains, losses = sum(max(0,x) for x in low), -sum(min(0,x) for x in low)
        result.append(dict(version=key[0], symbol=key[1], side=key[2], regime=key[3],
            signals=len(rows), filled=sum(t.get("filled_at_ms") is not None for t in rows),
            resolved=len(low), no_entry=len(no_entry), decision_count=decided,
            no_entry_rate=len(no_entry)/len(rows) if rows else None,
            confirmed_net_wins=sum(t['journal_json']['net_r_low']>0 and 'AMBIGUOUS' not in t['status'] for t in closed),
            pending_or_incomplete=len(rows)-decided,
            mean_net_r_per_alert_low=sum(low)/decided if decided else None,
            mean_net_r_per_alert_high=sum(high)/decided if decided else None,
            ambiguous=sum("AMBIGUOUS" in t["status"] for t in rows),
            mean_net_r_low=sum(low)/len(low) if low else None,
            mean_net_r_high=sum(high)/len(high) if high else None,
            profit_factor_low=gains/losses if losses else None,
            max_drawdown_r_low=dd, max_loss_streak=max_streak,
            note="Paper equal-risk sequence; not account equity; no automatic promotion"))
    return result
