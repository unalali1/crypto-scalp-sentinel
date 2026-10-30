"""Durable setup deduplication, isolated shadow outcomes and replay archive."""
import gzip
import json
import logging
from .tracker import ACTIVE, MINUTE, advance_trade, COLUMNS, _persist
from .performance import settle_funding

log=logging.getLogger("sentinel.journal")


def paper_trade(signal, now_ms):
    return dict(symbol=signal.symbol, side=signal.side, sent_at_ms=now_ms,
        entry_price=signal.entry,stop_price=signal.stop,tp1_price=signal.tp1,tp2_price=signal.tp2,
        status="AWAITING_ENTRY",filled_at_ms=None,tp1_at_ms=None,tp2_at_ms=None,stop_at_ms=None,
        last_checked_ms=((now_ms+MINUTE-1)//MINUTE)*MINUTE-MINUTE,
        finished_at_ms=None,strategy_version=signal.strategy_version,
        journal_json=dict(signal.journal))


def catch_up(trade, client, now_ms):
    last=((now_ms-2000)//MINUTE-1)*MINUTE
    for _ in range(2):
        start=trade["last_checked_ms"]+MINUTE
        if start>last or trade["status"] not in ACTIVE:
            break
        end=min(last,start+999*MINUTE)
        bars=client.get("/fapi/v1/klines",symbol=trade["symbol"],interval="1m",
                        startTime=start,endTime=end+MINUTE-1,limit=1000)
        bars=[b for b in bars if start<=int(b[0])<=end]
        if not bars:
            break
        trade=advance_trade(trade,bars)
    return trade


def funding(trade, client, now_ms):
    p=trade.get("journal_json") or {}
    if trade["status"] in ACTIVE or p.get("costs_complete") or not p.get("cost_model"):
        return
    if p.get("net_r_low") is None:
        p["costs_complete"]=True
        return
    # Wait until the outcome candle has closed before querying funding.
    if now_ms < trade["finished_at_ms"]+MINUTE+5000:
        return
    events=client.get("/fapi/v1/fundingRate",symbol=trade["symbol"],
        startTime=trade.get("filled_at_ms") or trade["sent_at_ms"],
        endTime=trade["finished_at_ms"]+MINUTE-1,limit=1000)
    settle_funding(trade,events)


class Journal:
    def __init__(self, conn=None):
        self.conn=conn
        self.memory_setups=set()
        self.memory_trades=[]
        if conn is not None:
            conn.execute("""CREATE TABLE IF NOT EXISTS scalp_setup_claims (
                cohort TEXT NOT NULL, setup_id TEXT NOT NULL, symbol TEXT NOT NULL,
                claimed_ms BIGINT NOT NULL, delivery TEXT NOT NULL DEFAULT 'reserved',
                PRIMARY KEY(cohort,setup_id))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS scalp_shadow_outcomes (
                cohort TEXT NOT NULL, setup_id TEXT NOT NULL, trade JSONB NOT NULL,
                status TEXT NOT NULL, sent_at_ms BIGINT NOT NULL,
                PRIMARY KEY(cohort,setup_id))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS scalp_replay_frames (
                symbol TEXT NOT NULL, observed_ms BIGINT NOT NULL,
                payload BYTEA NOT NULL, PRIMARY KEY(symbol,observed_ms))""")
            conn.execute("CREATE INDEX IF NOT EXISTS scalp_replay_time ON scalp_replay_frames(observed_ms)")
            conn.execute("CREATE INDEX IF NOT EXISTS scalp_outcomes_status ON scalp_signal_outcomes(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS scalp_shadow_status ON scalp_shadow_outcomes(status)")
            conn.execute("""CREATE TABLE IF NOT EXISTS scalp_assessments (
                symbol TEXT NOT NULL, observed_ms BIGINT NOT NULL, version TEXT NOT NULL,
                side TEXT NOT NULL, checks JSONB NOT NULL, eligible BOOLEAN NOT NULL,
                reason TEXT, PRIMARY KEY(symbol,observed_ms,version,side))""")
            conn.execute("CREATE INDEX IF NOT EXISTS scalp_assessment_time ON scalp_assessments(observed_ms)")
            conn.execute("""CREATE TABLE IF NOT EXISTS scalp_decisions (
                symbol TEXT NOT NULL, observed_ms BIGINT NOT NULL, cohort TEXT NOT NULL,
                side TEXT NOT NULL, reason TEXT NOT NULL,
                PRIMARY KEY(symbol,observed_ms,cohort,side))""")
            conn.execute("CREATE INDEX IF NOT EXISTS scalp_decision_time ON scalp_decisions(observed_ms)")
            conn.commit()

    def decision(self,signal,cohort,now_ms,reason):
        log.info('signal_decision cohort=%s symbol=%s side=%s reason=%s',cohort,signal.symbol,signal.side,reason)
        if self.conn is not None:
            self.conn.execute("""INSERT INTO scalp_decisions VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT(symbol,observed_ms,cohort,side) DO UPDATE SET reason=excluded.reason""",
                (signal.symbol,now_ms,cohort,signal.side,reason))
            self.conn.commit()

    def lock(self):
        if self.conn is None:
            return True
        result=self.conn.execute("SELECT pg_try_advisory_lock(739201728)").fetchone()[0]
        self.conn.commit()
        return result

    def alive(self):
        if self.conn is not None:
            self.conn.execute("SELECT 1")
            self.conn.commit()

    def claim(self, cohort, signal, now_ms):
        key=(cohort,signal.setup_id)
        if self.conn is None:
            if key in self.memory_setups:
                return False
            self.memory_setups.add(key)
            return True
        row=self.conn.execute("""INSERT INTO scalp_setup_claims
            (cohort,setup_id,symbol,claimed_ms) VALUES (%s,%s,%s,%s)
            ON CONFLICT DO NOTHING RETURNING setup_id""",
            (cohort,signal.setup_id,signal.symbol,now_ms)).fetchone()
        self.conn.commit()
        return row is not None

    def delivery(self, cohort, signal, status):
        if self.conn is not None:
            self.conn.execute("UPDATE scalp_setup_claims SET delivery=%s WHERE cohort=%s AND setup_id=%s",
                              (status,cohort,signal.setup_id))
            self.conn.commit()

    def pending_delivery(self):
        if self.conn is None:
            return False
        # A committed outcome proves delivery was recorded even if the worker
        # died before updating the claim. Otherwise do not guess or resend.
        self.conn.execute("""UPDATE scalp_setup_claims c SET delivery='sent'
            WHERE cohort='primary' AND delivery IN ('reserved','uncertain')
            AND EXISTS (SELECT 1 FROM scalp_signal_outcomes o
                        WHERE o.journal_json->>'setup_id'=c.setup_id)""")
        found=self.conn.execute("SELECT 1 FROM scalp_setup_claims WHERE cohort='primary' AND delivery IN ('reserved','uncertain') LIMIT 1").fetchone()
        self.conn.commit()
        return found is not None

    def active(self, cohort):
        if self.conn is None:
            return [t for t in self.memory_trades if t.get("cohort")==cohort and t["status"] in ACTIVE]
        if cohort=="primary":
            rows=self.conn.execute("SELECT symbol,side FROM scalp_signal_outcomes WHERE status IN (%s,%s,%s)",ACTIVE).fetchall()
            self.conn.commit()
            return [dict(symbol=s,side=d) for s,d in rows]
        rows=self.conn.execute("SELECT trade FROM scalp_shadow_outcomes WHERE cohort=%s AND status IN (%s,%s,%s)",(cohort,*ACTIVE)).fetchall()
        self.conn.commit()
        return [r[0] for r in rows]

    def cooldown(self, cohort, signal, minutes, now_ms):
        if self.conn is None:
            times=[t["sent_at_ms"] for t in self.memory_trades if t.get("cohort")==cohort
                   and t["symbol"]==signal.symbol and t["side"]==signal.side]
        else:
            rows=self.conn.execute("""SELECT max(sent_at_ms) FROM scalp_shadow_outcomes
                WHERE cohort=%s AND trade->>'symbol'=%s AND trade->>'side'=%s""",
                (cohort,signal.symbol,signal.side)).fetchone()
            self.conn.commit()
            times=[rows[0]] if rows[0] is not None else []
        return not times or now_ms-max(times)>=minutes*MINUTE

    def recent_count(self, cohort, now_ms):
        if self.conn is None:
            return sum(t.get("cohort")==cohort and t["sent_at_ms"]>now_ms-15*MINUTE for t in self.memory_trades)
        if cohort=="primary":
            n=self.conn.execute("SELECT count(*) FROM scalp_signal_outcomes WHERE sent_at_ms>%s",(now_ms-15*MINUTE,)).fetchone()[0]
        else:
            n=self.conn.execute("SELECT count(*) FROM scalp_shadow_outcomes WHERE cohort=%s AND sent_at_ms>%s",(cohort,now_ms-15*MINUTE)).fetchone()[0]
        self.conn.commit()
        return n

    def add_shadow(self, cohort, signal, now_ms):
        t=paper_trade(signal,now_ms)
        t["strategy_version"]=cohort
        t["cohort"]=cohort
        if self.conn is None:
            self.memory_trades.append(t)
            return
        self.conn.execute("""INSERT INTO scalp_shadow_outcomes
            (cohort,setup_id,trade,status,sent_at_ms) VALUES (%s,%s,%s::jsonb,%s,%s)
            ON CONFLICT DO NOTHING""",(cohort,signal.setup_id,json.dumps(t),t["status"],now_ms))
        self.conn.commit()

    def archive(self,symbol,snapshot,now_ms,assessments):
        if self.conn is None:
            return
        payload=gzip.compress(json.dumps(snapshot,separators=(",",":")).encode(),compresslevel=6)
        self.conn.execute("INSERT INTO scalp_replay_frames VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                          (symbol,now_ms,payload))
        for s in assessments:
            self.conn.execute("""INSERT INTO scalp_assessments
                VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s) ON CONFLICT DO NOTHING""",
                (symbol,now_ms,s.strategy_version,s.side,json.dumps(s.checks),s.eligible,None))
        self.conn.commit()

    def track_shadows(self,client,now_ms):
        if self.conn is None:
            return
        rows=self.conn.execute("""SELECT cohort,setup_id,trade FROM scalp_shadow_outcomes
            WHERE status IN (%s,%s,%s) OR (trade->'journal_json'->>'costs_complete')='false'
            ORDER BY sent_at_ms LIMIT 200""",ACTIVE).fetchall()
        self.conn.commit()
        for cohort,setup,t in rows:
            try:
                t=catch_up(t,client,now_ms)
                funding(t,client,now_ms)
                self.conn.execute("UPDATE scalp_shadow_outcomes SET trade=%s::jsonb,status=%s WHERE cohort=%s AND setup_id=%s",
                                  (json.dumps(t),t["status"],cohort,setup))
                self.conn.commit()
            except Exception as exc:
                self.conn.rollback()
                log.warning("shadow_track_failed cohort=%s kind=%s",cohort,type(exc).__name__)

    def settle_primary(self,client,now_ms):
        if self.conn is None:
            return
        rows=self.conn.execute("SELECT "+",".join(COLUMNS)+" FROM scalp_signal_outcomes "
            "WHERE status NOT IN (%s,%s,%s) AND journal_json->>'costs_complete'='false' ORDER BY sent_at_ms LIMIT 50",ACTIVE).fetchall()
        self.conn.commit()
        for row in rows:
            t=dict(zip(COLUMNS,row))
            try:
                funding(t,client,now_ms)
                _persist(self.conn,t)
            except Exception as exc:
                self.conn.rollback()
                log.warning("funding_pending symbol=%s kind=%s",t["symbol"],type(exc).__name__)

    def prune(self,now_ms,days):
        if self.conn is None:
            return
        cutoff=now_ms-days*86400000
        # Bounded deletions avoid long transactions after downtime.
        for table in ("scalp_replay_frames","scalp_assessments","scalp_decisions"):
            self.conn.execute(f"DELETE FROM {table} WHERE ctid IN (SELECT ctid FROM {table} WHERE observed_ms<%s LIMIT 10000)",(cutoff,))
        self.conn.execute("DELETE FROM scalp_setup_claims WHERE claimed_ms<%s AND (delivery='sent' OR cohort<>'primary')",(cutoff,))
        self.conn.commit()
