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
        self.memory_confluences=set()
        self.memory_price_zones=set()
        self.memory_price_zone_state={}
        self.memory_price_zone_events=[]
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
            conn.execute("""CREATE TABLE IF NOT EXISTS scalp_swing_assessments (
                symbol TEXT NOT NULL, bar_close_ms BIGINT NOT NULL,
                side TEXT NOT NULL, evaluated_ms BIGINT NOT NULL,
                setup_id TEXT NOT NULL, checks JSONB NOT NULL,
                decision TEXT NOT NULL, details JSONB NOT NULL,
                PRIMARY KEY(symbol,bar_close_ms,side))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS scalp_decisions (
                symbol TEXT NOT NULL, observed_ms BIGINT NOT NULL, cohort TEXT NOT NULL,
                side TEXT NOT NULL, reason TEXT NOT NULL,
                PRIMARY KEY(symbol,observed_ms,cohort,side))""")
            conn.execute("CREATE INDEX IF NOT EXISTS scalp_decision_time ON scalp_decisions(observed_ms)")
            conn.execute("""CREATE TABLE IF NOT EXISTS scalp_candidate_arrivals (
                cohort TEXT NOT NULL, observed_ms BIGINT NOT NULL,
                completed_ms BIGINT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
                score INTEGER NOT NULL, rvol DOUBLE PRECISION NOT NULL,
                PRIMARY KEY(cohort,observed_ms,symbol,side))""")
            conn.execute("CREATE INDEX IF NOT EXISTS scalp_arrival_time ON scalp_candidate_arrivals(observed_ms)")
            conn.execute("""CREATE TABLE IF NOT EXISTS scalp_confluence_alerts (
                alert_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, side TEXT NOT NULL,
                timeframe TEXT NOT NULL, pivot_ts BIGINT NOT NULL,
                detected_ms BIGINT NOT NULL, delivery TEXT NOT NULL DEFAULT 'reserved')""")
            conn.execute("CREATE INDEX IF NOT EXISTS scalp_confluence_time ON scalp_confluence_alerts(detected_ms)")
            conn.execute("""CREATE TABLE IF NOT EXISTS scalp_price_zone_alerts (
                alert_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, zone_low DOUBLE PRECISION NOT NULL,
                zone_high DOUBLE PRECISION NOT NULL, detected_ms BIGINT NOT NULL,
                delivery TEXT NOT NULL DEFAULT 'reserved')""")
            conn.execute("CREATE INDEX IF NOT EXISTS scalp_price_zone_time ON scalp_price_zone_alerts(detected_ms)")
            conn.execute("ALTER TABLE scalp_price_zone_alerts ADD COLUMN IF NOT EXISTS symbol TEXT")
            conn.execute("ALTER TABLE scalp_price_zone_alerts ADD COLUMN IF NOT EXISTS alert_type TEXT NOT NULL DEFAULT 'immediate'")
            conn.execute("CREATE INDEX IF NOT EXISTS scalp_price_zone_symbol_time ON scalp_price_zone_alerts(symbol,detected_ms)")
            conn.execute("""CREATE TABLE IF NOT EXISTS scalp_price_zone_state (
                symbol TEXT NOT NULL, zone_key TEXT NOT NULL, zone_low DOUBLE PRECISION NOT NULL,
                zone_high DOUBLE PRECISION NOT NULL, atr1h DOUBLE PRECISION NOT NULL,
                armed BOOLEAN NOT NULL DEFAULT TRUE, last_alert_ms BIGINT,
                updated_ms BIGINT NOT NULL, PRIMARY KEY(symbol,zone_key))""")
            conn.commit()

    def record_arrivals(self,cohort,observed_ms,arrivals):
        if self.conn is None or not arrivals:
            return
        rows=[dict(symbol=sym,side=side,score=score,rvol=rvol,completed_ms=completed)
              for sym,side,score,rvol,completed in arrivals]
        self.conn.execute("""INSERT INTO scalp_candidate_arrivals
            (cohort,observed_ms,completed_ms,symbol,side,score,rvol)
            SELECT %s,%s,r.completed_ms,r.symbol,r.side,r.score,r.rvol
            FROM jsonb_to_recordset(%s::jsonb) AS r(
                symbol TEXT,side TEXT,score INTEGER,rvol DOUBLE PRECISION,completed_ms BIGINT)
            ON CONFLICT DO NOTHING""",(cohort,observed_ms,json.dumps(rows)))
        self.conn.commit()

    def decision(self,signal,cohort,now_ms,reason):
        log.info('signal_decision cohort=%s symbol=%s side=%s reason=%s',cohort,signal.symbol,signal.side,reason)
        if self.conn is not None:
            self.conn.execute("""INSERT INTO scalp_decisions VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT(symbol,observed_ms,cohort,side) DO UPDATE SET reason=excluded.reason""",
                (signal.symbol,now_ms,cohort,signal.side,reason))
            self.conn.commit()

    def claim_confluence(self,alert,now_ms):
        key=alert["alert_id"]
        if self.conn is None:
            if key in self.memory_confluences:
                return False
            self.memory_confluences.add(key)
            return True
        row=self.conn.execute("""INSERT INTO scalp_confluence_alerts
            (alert_id,symbol,side,timeframe,pivot_ts,detected_ms)
            VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING alert_id""",
            (key,alert["symbol"],alert["side"],alert["timeframe"],alert["pivot_ts"],now_ms)).fetchone()
        self.conn.commit()
        return row is not None

    def confluence_delivery(self,alert,status):
        if self.conn is not None:
            self.conn.execute("UPDATE scalp_confluence_alerts SET delivery=%s WHERE alert_id=%s",
                              (status,alert["alert_id"]))
            self.conn.commit()

    def observe_price_zones(self,symbol,zones,price,now_ms):
        for zone in zones:
            key=(symbol,zone["zone_id"])
            state=self.memory_price_zone_state.get(key,{"armed":True,"last_alert_ms":None})
            if not state["armed"] and max(0.0,zone["low"]-price,price-zone["high"]) > 0.5*zone["atr1h"]:
                state["armed"]=True
            state.update({"low":zone["low"],"high":zone["high"],"atr1h":zone["atr1h"],"updated_ms":now_ms})
            self.memory_price_zone_state[key]=state
        if self.conn is None or not zones:
            return
        # One round trip per symbol instead of an UPSERT and SELECT per zone.
        # A disarmed zone rearms only after price has moved beyond half its
        # current ATR, preserving the previous alert suppression rule.
        rows={z["zone_id"]:{"zone_key":z["zone_id"],"zone_low":z["low"],
              "zone_high":z["high"],"atr1h":z["atr1h"]} for z in zones}
        self.conn.execute("""INSERT INTO scalp_price_zone_state
            (symbol,zone_key,zone_low,zone_high,atr1h,updated_ms)
            SELECT %s,z.zone_key,z.zone_low,z.zone_high,z.atr1h,%s
            FROM jsonb_to_recordset(%s::jsonb) AS z(
                zone_key TEXT,zone_low DOUBLE PRECISION,
                zone_high DOUBLE PRECISION,atr1h DOUBLE PRECISION)
            WHERE TRUE
            ON CONFLICT(symbol,zone_key) DO UPDATE SET
                zone_low=excluded.zone_low,zone_high=excluded.zone_high,
                atr1h=excluded.atr1h,updated_ms=excluded.updated_ms,
                armed=scalp_price_zone_state.armed OR
                    GREATEST(0.0,excluded.zone_low-%s,%s-excluded.zone_high)
                    > 0.5*excluded.atr1h""",
            (symbol,now_ms,json.dumps(list(rows.values())),price,price))
        self.conn.commit()

    def claim_price_zone(self,alert,now_ms):
        symbol=alert["symbol"]
        zone_key=alert.get("zone_key",alert.get("zone_id",alert["alert_id"]))
        if self.conn is None:
            recent=[ts for sym,ts,_kind in self.memory_price_zone_events if sym==symbol and now_ms-ts<30*60*1000]
            if recent:
                return False
            state=self.memory_price_zone_state.setdefault((symbol,zone_key),{"armed":True})
            if not state["armed"]:
                return False
            state["armed"]=False
            state["last_alert_ms"]=now_ms
            event_id=f"{symbol}:{zone_key}:{now_ms}"
            alert["event_id"]=event_id
            self.memory_price_zones.add(event_id)
            self.memory_price_zone_events.append((symbol,now_ms,alert.get("alert_type","immediate")))
            return True
        last=self.conn.execute("""SELECT MAX(detected_ms) FROM scalp_price_zone_alerts
            WHERE symbol=%s AND detected_ms>%s""",(symbol,now_ms-30*60*1000)).fetchone()[0]
        if last is not None:
            return False
        state=self.conn.execute("""SELECT armed FROM scalp_price_zone_state
            WHERE symbol=%s AND zone_key=%s""",(symbol,zone_key)).fetchone()
        if state is not None and not state[0]:
            return False
        event_id=f"{symbol}:{zone_key}:{now_ms}"
        alert_type=alert.get("alert_type","immediate")
        row=self.conn.execute("""INSERT INTO scalp_price_zone_alerts
            (alert_id,symbol,zone_low,zone_high,detected_ms,alert_type)
            VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING alert_id""",
            (event_id,symbol,alert["low"],alert["high"],now_ms,alert_type)).fetchone()
        if row is None:
            self.conn.commit()
            return False
        self.conn.execute("""INSERT INTO scalp_price_zone_state
            (symbol,zone_key,zone_low,zone_high,atr1h,armed,last_alert_ms,updated_ms)
            VALUES (%s,%s,%s,%s,%s,FALSE,%s,%s)
            ON CONFLICT(symbol,zone_key) DO UPDATE SET armed=FALSE,
            last_alert_ms=excluded.last_alert_ms,updated_ms=excluded.updated_ms""",
            (symbol,zone_key,alert["low"],alert["high"],alert.get("atr1h",0),now_ms,now_ms))
        alert["event_id"]=event_id
        self.conn.commit()
        return True

    def recent_price_zone_count(self,now_ms):
        cutoff=now_ms-15*60*1000
        if self.conn is None:
            self.memory_price_zone_events=[item for item in self.memory_price_zone_events if item[1]>=cutoff]
            return sum(1 for _,ts,kind in self.memory_price_zone_events if ts>=cutoff and kind=="immediate")
        row=self.conn.execute("""SELECT COUNT(*) FROM scalp_price_zone_alerts
            WHERE detected_ms>=%s AND alert_type='immediate'""",(cutoff,)).fetchone()
        return int(row[0] or 0)

    def price_zone_delivery(self,alert,status):
        if self.conn is not None:
            self.conn.execute("UPDATE scalp_price_zone_alerts SET delivery=%s WHERE alert_id=%s",
                              (status,alert.get("event_id",alert["alert_id"])))
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

    def add_swing_shadow(self, signal, now_ms):
        """Atomically deduplicate and persist a paper swing; never sends alerts."""
        cohort="swing-shadow"
        key=(cohort,signal.setup_id)
        if self.conn is None:
            if key in self.memory_setups:
                return False
            self.memory_setups.add(key)
            trade=paper_trade(signal,now_ms)
            trade.update(cohort=cohort,strategy_version=signal.strategy_version)
            self.memory_trades.append(trade)
            return True
        trade=paper_trade(signal,now_ms)
        trade.update(cohort=cohort,strategy_version=signal.strategy_version)
        row=self.conn.execute("""INSERT INTO scalp_shadow_outcomes
            (cohort,setup_id,trade,status,sent_at_ms) VALUES (%s,%s,%s::jsonb,%s,%s)
            ON CONFLICT DO NOTHING RETURNING setup_id""",
            (cohort,signal.setup_id,json.dumps(trade),trade["status"],now_ms)).fetchone()
        self.conn.commit()
        return row is not None

    def add_swing_counterfactual(self, signal, now_ms, selection_reason):
        """Track an unselected executable setup without consuming swing limits."""
        cohort="swing-candidate-shadow"
        key=(cohort,signal.setup_id)
        trade=paper_trade(signal,now_ms)
        trade.update(cohort=cohort,strategy_version=signal.strategy_version)
        trade["journal_json"]["selection_reason"]=selection_reason
        if self.conn is None:
            if key in self.memory_setups or ("swing-shadow",signal.setup_id) in self.memory_setups:
                return False
            self.memory_setups.add(key)
            self.memory_trades.append(trade)
            return True
        row=self.conn.execute("""INSERT INTO scalp_shadow_outcomes
            (cohort,setup_id,trade,status,sent_at_ms)
            SELECT %s,%s,%s::jsonb,%s,%s
            WHERE NOT EXISTS (SELECT 1 FROM scalp_shadow_outcomes
                              WHERE cohort='swing-shadow' AND setup_id=%s)
            ON CONFLICT DO NOTHING RETURNING setup_id""",
            (cohort,signal.setup_id,json.dumps(trade),trade["status"],now_ms,
             signal.setup_id)).fetchone()
        self.conn.commit()
        return row is not None

    def swing_recent_count(self, now_ms):
        """Rolling 24h paper swing count, across all symbols."""
        cutoff=now_ms-24*3600000
        if self.conn is None:
            return sum(t.get("cohort")=="swing-shadow" and t["sent_at_ms"]>cutoff
                       for t in self.memory_trades)
        count=self.conn.execute("""SELECT count(*) FROM scalp_shadow_outcomes
            WHERE cohort='swing-shadow' AND sent_at_ms>%s""",(cutoff,)).fetchone()[0]
        self.conn.commit()
        return int(count)

    def swing_assessment(self, signal, now_ms, decision):
        if self.conn is None:
            return
        details=dict(entry=signal.entry,stop=signal.stop,tp1=signal.tp1,
                     tp2=signal.tp2,atr4=signal.atr4,rvol4=signal.rvol4,
                     spread_bps=signal.spread_bps,quote_volume=signal.quote_volume)
        self.conn.execute("""INSERT INTO scalp_swing_assessments
            (symbol,bar_close_ms,side,evaluated_ms,setup_id,checks,decision,details)
            VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb)
            ON CONFLICT(symbol,bar_close_ms,side) DO UPDATE SET
                evaluated_ms=excluded.evaluated_ms,checks=excluded.checks,
                decision=excluded.decision,
                details=excluded.details""",
            (signal.symbol,signal.snapshot_ts+4*3600000,signal.side,now_ms,
             signal.setup_id,json.dumps(signal.checks),decision,json.dumps(details)))
        self.conn.commit()

    def archive(self,symbol,snapshot,now_ms,assessments):
        if self.conn is None:
            return
        replay_snapshot={k:v for k,v in snapshot.items() if k not in ("1d","1w","1M")}
        payload=gzip.compress(json.dumps(replay_snapshot,separators=(",",":")).encode(),compresslevel=6)
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
        for table in ("scalp_replay_frames","scalp_assessments","scalp_decisions","scalp_candidate_arrivals"):
            self.conn.execute(f"DELETE FROM {table} WHERE ctid IN (SELECT ctid FROM {table} WHERE observed_ms<%s LIMIT 10000)",(cutoff,))
        self.conn.execute("DELETE FROM scalp_setup_claims WHERE claimed_ms<%s AND (delivery='sent' OR cohort<>'primary')",(cutoff,))
        self.conn.execute("DELETE FROM scalp_confluence_alerts WHERE ctid IN (SELECT ctid FROM scalp_confluence_alerts WHERE detected_ms<%s LIMIT 10000)",(cutoff,))
        self.conn.execute("DELETE FROM scalp_price_zone_alerts WHERE ctid IN (SELECT ctid FROM scalp_price_zone_alerts WHERE detected_ms<%s LIMIT 10000)",(cutoff,))
        self.conn.commit()
