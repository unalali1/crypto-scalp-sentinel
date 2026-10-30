"""Read-only paper performance report: python -m scalp_sentinel.report."""
import json
import os
from .tracker import COLUMNS
from .performance import summarize


def read_report(conn):
    # Do not mix historical V1/V2 target-touch records with cost-model results.
    rows=conn.execute("SELECT "+",".join(COLUMNS)+" FROM scalp_signal_outcomes WHERE journal_json ? 'cost_model'").fetchall()
    trades=[dict(zip(COLUMNS,row)) for row in rows]
    trades.extend(row[0] for row in conn.execute("SELECT trade FROM scalp_shadow_outcomes").fetchall())
    conn.commit()
    aggregate=[dict(t,symbol='ALL',side='ALL',journal_json=dict(t.get('journal_json') or {},regime='ALL')) for t in trades]
    return {"totals":summarize(aggregate),"groups":summarize(trades),"paper_only":True,"automatic_promotion":False}


if __name__=="__main__":
    import psycopg
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        print(json.dumps(read_report(conn),ensure_ascii=False,indent=2))
