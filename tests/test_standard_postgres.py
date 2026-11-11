"""Real PostgreSQL isolation, restart and at-most-once notification regression."""
import os
import uuid
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import psycopg
from psycopg import sql
from urllib.parse import urlencode
from scalp_sentinel.storage import Cooldowns
from scalp_sentinel.journal import Journal
from scalp_sentinel.main import Runner
from scalp_sentinel.forward_report import maintain_reports, read_window
from scalp_sentinel.execution import prepare
from test_standard_forward import settings, signal, snap, NOW


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
