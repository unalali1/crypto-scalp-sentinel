"""Real PostgreSQL isolation, restart and at-most-once notification regression."""
import os
import copy
import uuid
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import psycopg
from psycopg import sql
from urllib.parse import urlencode
from scalp_sentinel.storage import Cooldowns
from scalp_sentinel.journal import Journal, SCORE_LOW_COHORT
from scalp_sentinel.main import Runner
from scalp_sentinel.forward_report import maintain_reports, read_window, read_score_study
from scalp_sentinel.execution import prepare
from test_standard_forward import settings, signal, snap, NOW
from test_score_filter import force_score


@unittest.skipUnless(os.environ.get('TEST_DATABASE_URL'),'requires isolated CI PostgreSQL')
class PostgresForward(unittest.TestCase):
    def setUp(self):
        self.admin=psycopg.connect(os.environ['TEST_DATABASE_URL'],autocommit=True)
        self.schema='test_scalp_'+uuid.uuid4().hex
        self.admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        self.dsn=os.environ['TEST_DATABASE_URL']+'?'+urlencode({'options':'-csearch_path='+self.schema})
        self.resources=[]

    def tearDown(self):
        for resource in self.resources:
            resource.close()
        self.admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))
        self.admin.close()

    def storage(self):
        c=Cooldowns(self.dsn,False); self.resources.append(c)
        return c

    def test_schema_repeated_restart_claims_outcomes_cooldown_and_lock(self):
        c=self.storage(); j=Journal(c.conn)
        self.assertTrue(j.lock())
        s=signal(); prepare(s,snap()['book'],NOW,settings())
        self.assertTrue(j.claim('primary',s,NOW))
        self.assertTrue(j.pending_delivery())
        with patch('scalp_sentinel.storage.time.time',return_value=NOW/1000):
            c.mark_sent(s.symbol,s.side,s)
        self.assertFalse(j.pending_delivery())
        j.record_standard_scan(NOW,dict(symbols=1,eligible=1,assessed=1,delivered=1,failed=0,decisions=[],reasons={}))
        # Reproduce the production legacy key with an existing outcome. The
        # next connection must migrate it without losing history or claims.
        c.conn.execute('ALTER TABLE scalp_signal_outcomes DROP CONSTRAINT scalp_outcomes_setup_key')
        c.conn.execute('ALTER TABLE scalp_signal_outcomes ADD CONSTRAINT scalp_signal_outcomes_symbol_side_signal_ts_key UNIQUE(symbol,side,signal_ts)')
        c.conn.commit()
        c2=self.storage(); j2=Journal(c2.conn)
        self.assertFalse(j2.lock())
        self.assertFalse(j2.claim('primary',s,NOW))
        self.assertEqual(j2.active('primary')[0]['journal_json']['risk_pct'],.35)
        with patch('scalp_sentinel.storage.time.time',return_value=NOW/1000):
            self.assertFalse(c2.allowed(s.symbol,s.side,90))
        self.assertEqual(read_window(c2.conn,NOW-1,NOW+1)['sent'],1)
        j2.prune(NOW,30)  # real parameterized LIKE and additive schemas
        c.close(); self.resources.remove(c)
        self.assertTrue(j2.lock())

    def test_daily_digest_once_across_restart_and_uncertain_send(self):
        c=self.storage(); j=Journal(c.conn)
        cfg=settings(daily_summary_enabled=True)
        day=1794391200000  # 2026-11-11 18:00 Beijing
        r=SimpleNamespace(journal=j,cfg=cfg,last_report=None,last_prune=None)
        with patch('scalp_sentinel.forward_report.send_telegram',side_effect=RuntimeError('uncertain')) as send:
            maintain_reports(r,day)
            c2=self.storage()
            r2=SimpleNamespace(journal=Journal(c2.conn),cfg=cfg,last_report=None,last_prune=None)
            maintain_reports(r2,day+60000)
            self.assertEqual(send.call_count,1)
        row=c2.conn.execute('SELECT delivery FROM scalp_daily_reports').fetchone()
        self.assertEqual(row[0],'uncertain')
        c2.conn.commit()

    def test_independent_ideas_persist_after_losses_open_peer_and_restart(self):
        cfg=settings(database_url=self.dsn,dry_run=False,telegram_token='test',telegram_chat_id='test',
                     max_open_signals=1,max_signals_per_run=1,standard_exit_shadows=False)
        client=SimpleNamespace(get=lambda *_a,**_kw:snap()['book'])
        r=Runner(cfg,client); self.resources.append(r)
        r.snapshots={'TEST':snap()}
        loss=signal(symbol='OLD'); prepare(loss,snap()['book'],NOW,settings())
        r.storage.mark_sent(loss.symbol,loss.side,loss,sent_at_ms=NOW-600000)
        r.storage.conn.execute("""UPDATE scalp_signal_outcomes SET status='STOPPED',finished_at_ms=%s,
            journal_json=journal_json || '{"net_r_low":-20,"costs_complete":true}'::jsonb
            WHERE symbol='OLD'""",(NOW-1000,))
        r.storage.conn.commit()
        first=signal(); second=signal()
        second.strategy_version='breakout-retest'
        second.setup_id=second.setup_id.replace('ema-pullback','breakout-retest')
        with patch('scalp_sentinel.main.send_telegram') as send:
            with patch('scalp_sentinel.main.time.time',return_value=NOW/1000):
                self.assertEqual(r.deliver([copy.deepcopy(first)],'primary',NOW),1)
            r2=Runner(cfg,client); self.resources.append(r2)
            r2.snapshots={'TEST':snap()}
            with patch('scalp_sentinel.main.time.time',return_value=(NOW+1000)/1000):
                self.assertEqual(r2.deliver([copy.deepcopy(first),second],'primary',NOW+1000),1)
            self.assertEqual(send.call_count,2)
        rows=r2.storage.conn.execute("SELECT journal_json->>'setup_id',status FROM scalp_signal_outcomes WHERE symbol='TEST'").fetchall()
        r2.storage.conn.commit()
        self.assertEqual({row[0] for row in rows},{first.setup_id,second.setup_id})
        self.assertTrue(all(row[1]=='AWAITING_ENTRY' for row in rows))
        self.assertEqual(read_window(r2.storage.conn,NOW-1,NOW+1001)['sent'],2)

    def test_uncertain_primary_delivery_never_retried_after_restart(self):
        cfg=settings(database_url=self.dsn,dry_run=False,telegram_token='test',telegram_chat_id='test',
                     standard_exit_shadows=False)
        class Client:
            def get(self,*args,**kwargs):return snap()['book']
        r=Runner(cfg,Client()); self.resources.append(r)
        r.snapshots={'TEST':snap()}
        with patch('scalp_sentinel.main.time.time',return_value=NOW/1000),patch('scalp_sentinel.main.send_telegram',side_effect=RuntimeError('uncertain')) as send:
            with self.assertRaises(RuntimeError):r.deliver([signal()],'primary',NOW)
            r2=Runner(cfg,Client()); self.resources.append(r2)
            r2.snapshots={'TEST':snap()}
            self.assertEqual(r2.deliver([signal()],'primary',NOW),0)
            self.assertEqual(send.call_count,1)

    def test_low_score_is_silent_durable_and_never_promoted_on_retry(self):
        cfg=settings(database_url=self.dsn,dry_run=False,telegram_token='test',telegram_chat_id='test',
                     standard_min_score=60,standard_exit_shadows=False)
        client=SimpleNamespace(get=lambda *_a,**_kw:snap()['book'])
        r=Runner(cfg,client); self.resources.append(r); r.snapshots={'TEST':snap()}
        with patch('scalp_sentinel.main.time.time',return_value=NOW/1000), \
             patch('scalp_sentinel.main.rank',side_effect=force_score(59.99)), \
             patch('scalp_sentinel.main.send_telegram') as send:
            self.assertEqual(r.deliver([signal()],'primary',NOW),0)
            send.assert_not_called()
        r2=Runner(cfg,client); self.resources.append(r2); r2.snapshots={'TEST':snap()}
        self.assertFalse(r2.journal.pending_delivery())
        self.assertEqual(len(r2.journal.active(SCORE_LOW_COHORT)),1)

        with patch('scalp_sentinel.main.time.time',return_value=NOW/1000), \
             patch('scalp_sentinel.main.rank',side_effect=force_score(60)), \
             patch('scalp_sentinel.main.send_telegram') as send:
            self.assertEqual(r2.deliver([signal()],'primary',NOW),0)
            fresh=signal(symbol='NEW'); r2.snapshots['NEW']=snap()
            self.assertEqual(r2.deliver([fresh],'primary',NOW),1)
            self.assertEqual(send.call_count,1)
        report=read_window(r2.storage.conn,NOW-1,NOW+1)
        self.assertEqual(report['sent'],1)
        self.assertEqual(report['score_comparison']['groups']['below']['signals'],1)
        self.assertEqual(report['score_comparison']['groups']['above']['signals'],1)
        self.assertEqual(report['paired_shadows'],[])
        study=read_score_study(r2.storage.conn,NOW+1)
        self.assertFalse(study['review_ready'])
        # Historical silent experiments are retained after ordinary shadow pruning.
        r2.storage.conn.execute("UPDATE scalp_shadow_outcomes SET status='NO_ENTRY', trade=trade || %s::jsonb WHERE cohort=%s",('{"status":"NO_ENTRY","journal_json":{"costs_complete":true}}',SCORE_LOW_COHORT))
        r2.storage.conn.commit()
        r2.journal.prune(NOW+31*86400000,30)
        count=r2.storage.conn.execute('SELECT count(*) FROM scalp_shadow_outcomes WHERE cohort=%s',(SCORE_LOW_COHORT,)).fetchone()[0]
        r2.storage.conn.commit()
        self.assertEqual(count,1)

    def test_silent_record_failure_rolls_back_setup_claim(self):
        c=self.storage(); j=Journal(c.conn)
        s=signal(); prepare(s,snap()['book'],NOW,settings())
        c.conn.execute("ALTER TABLE scalp_shadow_outcomes ADD CONSTRAINT reject_silent_test CHECK (cohort<>'standard-score-v1-low')")
        c.conn.commit()
        with self.assertRaises(psycopg.errors.CheckViolation):
            j.record_score_low(s,NOW)
        self.assertFalse(j.pending_delivery())
        c.conn.execute('ALTER TABLE scalp_shadow_outcomes DROP CONSTRAINT reject_silent_test')
        c.conn.commit()
        self.assertTrue(j.record_score_low(s,NOW))
        self.assertFalse(j.record_score_low(s,NOW))
