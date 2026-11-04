"""Read-only paper performance report: python -m scalp_sentinel.report."""
import json
import os
import time
from collections import Counter, defaultdict
from .tracker import COLUMNS
from .performance import summarize


def _median(values):
    if not values:
        return None
    values=sorted(values)
    n=len(values)
    return (values[(n-1)//2]+values[n//2])/2


def delivery_funnel(trades, decisions, assessments, arrivals, since_ms):
    """Thirty-day signal path; arrival order is diagnostic, not a simulated fill."""
    cohorts=defaultdict(lambda: {"reasons":Counter(), "outcomes":Counter(),
        "sent":0,"filled":0,"decided":0,"net_r_low_sum":0.0,
        "net_r_high_sum":0.0,"spread_bps":[],"quote_age_ms":[],"candle_age_ms":[]})
    recent_reasons=defaultdict(Counter)
    last_day_start=since_ms+29*86400000
    for observed_ms,cohort,reason in decisions:
        cohorts[cohort]["reasons"][reason]+=1
        if observed_ms>=last_day_start:
            recent_reasons[cohort][reason]+=1
    for trade in trades:
        if trade["sent_at_ms"]<since_ms:
            continue
        c=cohorts[trade["cohort"]]
        c["sent"]+=1
        c["outcomes"][trade["status"]]+=1
        c["filled"]+=trade.get("filled_at_ms") is not None
        p=trade.get("journal_json") or {}
        for key in ("spread_bps","quote_age_ms","candle_age_ms"):
            if p.get(key) is not None:
                c[key].append(p[key])
        if trade["status"] in ("NO_ENTRY","INVALIDATED"):
            c["decided"]+=1
        elif p.get("costs_complete") and p.get("net_r_low") is not None and p.get("net_r_high") is not None:
            c["decided"]+=1
            c["net_r_low_sum"]+=p["net_r_low"]
            c["net_r_high_sum"]+=p["net_r_high"]
    results={}
    for cohort,c in sorted(cohorts.items()):
        decided=c["decided"]
        results[cohort]=dict(decisions=dict(c["reasons"]),outcomes=dict(c["outcomes"]),
            sent=c["sent"],filled=c["filled"],decided=decided,
            mean_net_r_per_decided_alert_low=c["net_r_low_sum"]/decided if decided else None,
            mean_net_r_per_decided_alert_high=c["net_r_high_sum"]/decided if decided else None,
            median_spread_bps=_median(c["spread_bps"]),
            median_quote_age_ms=_median(c["quote_age_ms"]),
            median_candle_age_ms=_median(c["candle_age_ms"]))
    eligible=Counter()
    for version,passed in assessments:
        if passed:
            eligible[version]+=1
    scans=defaultdict(list)
    for observed,completed,symbol,side,score,rvol in arrivals:
        scans[observed].append((completed,symbol,side,score,rvol))
    contested=first_lower=0
    for candidates in scans.values():
        if len(candidates)<2:
            continue
        contested+=1
        first=min(candidates,key=lambda c:(c[0],c[1],c[2]))
        best=max(candidates,key=lambda c:(c[3],c[4]))
        first_lower+=first[3]<best[3] or (first[3]==best[3] and first[4]<best[4])
    return dict(window_start_ms=since_ms,cohorts=results,
        last_24h_decisions=dict((cohort,dict(counts)) for cohort,counts in sorted(recent_reasons.items())),
        eligible_by_version=dict(eligible),
        arrival_order=dict(multi_candidate_scans=contested,first_below_best_rank=first_lower,
            note="Eligibility and score order only; does not prove a better fill or outcome."),
        note="Paper/reference fills, not exchange execution. Assessments and decisions expire with retention.")


def read_report(conn,now_ms=None):
    # Do not mix historical V1/V2 target-touch records with cost-model results.
    rows=conn.execute("SELECT "+",".join(COLUMNS)+" FROM scalp_signal_outcomes WHERE journal_json ? 'cost_model'").fetchall()
    trades=[dict(dict(zip(COLUMNS,row)),cohort="primary") for row in rows]
    trades.extend(dict(row[1],cohort=row[0]) for row in
                  conn.execute("SELECT cohort,trade FROM scalp_shadow_outcomes").fetchall())
    since_ms=(int(time.time()*1000) if now_ms is None else now_ms)-30*86400000
    decisions=conn.execute("SELECT observed_ms,cohort,reason FROM scalp_decisions WHERE observed_ms >= %s",
                           (since_ms,)).fetchall()
    assessments=conn.execute("SELECT version,eligible FROM scalp_assessments WHERE observed_ms >= %s",
                             (since_ms,)).fetchall()
    arrivals=conn.execute("""SELECT observed_ms,completed_ms,symbol,side,score,rvol
        FROM scalp_candidate_arrivals WHERE observed_ms >= %s AND cohort='v2.1'""",
                          (since_ms,)).fetchall()
    conn.commit()
    aggregate=[dict(t,symbol='ALL',side='ALL',journal_json=dict(t.get('journal_json') or {},regime='ALL')) for t in trades]
    return {"totals":summarize(aggregate),"groups":summarize(trades),
            "last_30d_execution_funnel":delivery_funnel(trades,decisions,assessments,arrivals,since_ms),
            "paper_only":True,"automatic_promotion":False}


if __name__=="__main__":
    import psycopg
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        print(json.dumps(read_report(conn),ensure_ascii=False,indent=2))
