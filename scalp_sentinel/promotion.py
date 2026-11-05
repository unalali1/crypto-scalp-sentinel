"""Predeclared, read-only paper promotion diagnostics; never enables alerts."""
from collections import Counter, defaultdict
from random import Random

DAY_MS = 86_400_000
DECIDED_ZERO = {"NO_ENTRY", "INVALIDATED"}
ACTIVE = {"AWAITING_ENTRY", "OPEN", "TP1_HIT"}


def daily_block_lower(rows, alpha, draws=5000, seed=0):
    """Resample active UTC days, keeping every alert on a day together."""
    if not rows or not 0 < alpha < 1 or draws < 100:
        return None
    days=defaultdict(list)
    for row in rows:
        days[row["sent_at_ms"]//DAY_MS].append(row["net_r_low"])
    blocks=list(days.values())
    rng=Random(seed)
    means=[]
    for _ in range(draws):
        sample=[blocks[rng.randrange(len(blocks))] for _ in blocks]
        means.append(sum(sum(block) for block in sample)/sum(len(block) for block in sample))
    means.sort()
    return means[max(0,int(alpha*draws)-1)]


def promotion_report(trades, cohort, *, trials=7, draws=5000, seed=0,
                     null_percentile=None):
    """Return gate reasons; callers must supply an independent null comparison.

    `trades` are one cohort's observed paper alerts. No result promotes itself.
    A null percentile is only valid from a separately documented matched study.
    """
    if trials<1:
        raise ValueError("trials must include every tried cohort")
    decided=[]
    gap=0
    incomplete=0
    all_rows=list(trades)
    for trade in all_rows:
        status=trade["status"]
        if status=="DATA_GAP":
            gap+=1
            continue
        if status in ACTIVE:
            continue
        p=trade.get("journal_json") or {}
        if status in DECIDED_ZERO:
            value=cost=0.0
        elif p.get("costs_complete") and p.get("net_r_low") is not None and p.get("execution_cost_r") is not None:
            value=float(p["net_r_low"])
            cost=float(p["execution_cost_r"])
        else:
            incomplete+=1
            continue
        decided.append(dict(symbol=trade["symbol"],side=trade["side"],
                            sent_at_ms=trade["sent_at_ms"],
                            regime=p.get("regime","unknown"),
                            net_r_low=value,cost_r=cost))
    reasons=[]
    n=len(decided)
    if n<150:
        reasons.append("insufficient_decided_alerts")
    observed_days=(max(r["sent_at_ms"] for r in decided)-min(r["sent_at_ms"] for r in decided))/DAY_MS if n else 0
    if observed_days<56:
        reasons.append("insufficient_calendar_span")
    regimes=Counter(r["regime"] for r in decided)
    if sum(count>=20 for label,count in regimes.items() if label!="unknown")<2:
        reasons.append("insufficient_regimes")
    alpha=.05/trials
    lower=daily_block_lower(decided,alpha,draws,seed) if n>=150 and observed_days>=56 else None
    if lower is None or lower<=0:
        reasons.append("lower_bound_not_positive")
    mean=sum(r["net_r_low"] for r in decided)/n if n else None
    doubled_cost_mean=sum(r["net_r_low"]-r["cost_r"] for r in decided)/n if n else None
    if doubled_cost_mean is None or doubled_cost_mean<=0:
        reasons.append("double_cost_not_positive")
    positive=sum(max(0,r["net_r_low"]) for r in decided)
    symbol_gains=defaultdict(float)
    day_gains=defaultdict(float)
    for r in decided:
        gain=max(0,r["net_r_low"])
        symbol_gains[r["symbol"]]+=gain
        day_gains[r["sent_at_ms"]//DAY_MS]+=gain
    concentration=max(max(symbol_gains.values(),default=0),max(day_gains.values(),default=0))/positive if positive else None
    if concentration is None or concentration>=.25:
        reasons.append("positive_return_concentration")
    terminal=sum(t["status"] not in ACTIVE for t in all_rows)
    gap_rate=gap/terminal if terminal else None
    if gap_rate is None or gap_rate>=.02:
        reasons.append("data_gap_rate")
    if incomplete:
        reasons.append("incomplete_costs_or_outcomes")
    if null_percentile is None or not 0<=null_percentile<=1 or null_percentile<.95:
        reasons.append("independent_null_not_passed")
    return dict(cohort=cohort,pass_all=False if reasons else True,reasons=reasons,
                decided=n,observed_days=observed_days,regimes=dict(regimes),
                mean_net_r_low=mean,lower_bound=lower,alpha_per_cohort=alpha,
                doubled_cost_mean=doubled_cost_mean,
                positive_gain_concentration=concentration,data_gap_rate=gap_rate,
                incomplete=incomplete,null_percentile=null_percentile,
                note="Research gate only; matched null must be supplied from untouched data; not account returns.")


def main():
    """Inspect observed outcomes; null comparison is deliberately unavailable here."""
    import argparse
    import json
    import os
    from datetime import datetime
    from .tracker import COLUMNS

    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start",required=True,help="UTC ISO timestamp, inclusive")
    parser.add_argument("--end",required=True,help="UTC ISO timestamp, exclusive")
    parser.add_argument("--trials",type=int,default=7,help="all tried cohorts, at least seven")
    args=parser.parse_args()
    def timestamp(value):
        return int(datetime.fromisoformat(value.replace("Z","+00:00")).timestamp()*1000)
    start,end=timestamp(args.start),timestamp(args.end)
    if start>=end or args.trials<7:
        parser.error("invalid period or trial count")
    import psycopg
    groups=defaultdict(list)
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        rows=conn.execute("SELECT "+",".join(COLUMNS)+" FROM scalp_signal_outcomes "
            "WHERE sent_at_ms >= %s AND sent_at_ms < %s AND journal_json ? 'cost_model'",
            (start,end)).fetchall()
        groups["primary"].extend(dict(zip(COLUMNS,row)) for row in rows)
        rows=conn.execute("SELECT cohort,trade FROM scalp_shadow_outcomes "
                          "WHERE sent_at_ms >= %s AND sent_at_ms < %s",(start,end)).fetchall()
        for cohort,trade in rows:
            groups[cohort].append(trade)
    reports=[promotion_report(rows,cohort,trials=max(args.trials,len(groups)))
             for cohort,rows in sorted(groups.items())]
    print(json.dumps(dict(window_start_ms=start,window_end_ms=end,reports=reports,
          note="Null controls unavailable: no cohort can pass from this report alone."),indent=2))


if __name__=="__main__":
    main()
