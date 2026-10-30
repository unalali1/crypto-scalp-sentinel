import copy
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from scalp_sentinel.main import Runner
from scalp_sentinel.journal import Journal
from scalp_sentinel.engine import candidate
from test_v2_engine import fixture
from test_execution_research import config
from test_client import FakeClient


class Delivery(unittest.TestCase):
    def test_worker_discovers_five_minute_and_only_monitors_candidates_between(self):
        client=FakeClient()
        r=Runner(config(symbols=('TEST','BTCUSDT')),client)
        try:
            with patch('scalp_sentinel.main.time.time',return_value=client.now/1000):
                r.tick(client.now)
            self.assertEqual(r.watch,set())
            n=len(client.calls)
            client.now+=60000
            with patch('scalp_sentinel.main.time.time',return_value=client.now/1000):
                r.tick(client.now)
            new=client.calls[n:]
            self.assertEqual(len(new),2)
            self.assertTrue(all(p['symbol']=='BTCUSDT' for _,p in new))
        finally:r.close()
    def runner(self):
        r=Runner.__new__(Runner)
        r.cfg=config(fee_bps=0,slippage_bps=0)
        r.journal=Journal()
        r.storage=SimpleNamespace(allowed=lambda *_:True)
        r.snapshots={}
        snap,c=fixture()
        self.s=candidate('TEST','LONG',snap,'0.001',c)
        self.s.strategy_version='v2.1'
        self.now=self.s.snapshot_ts+63000
        r.client=SimpleNamespace(get=lambda *_a,**_k:dict(bidPrice=self.s.entry-.001,askPrice=self.s.entry+.001,time=self.now))
        return r

    def test_dry_run_and_shadow_never_send_telegram(self):
        r=self.runner()
        with patch('scalp_sentinel.main.time.time',return_value=self.now/1000),patch('scalp_sentinel.main.send_telegram') as send:
            self.assertEqual(r.deliver([copy.deepcopy(self.s)],'primary',self.now),1)
            self.assertEqual(r.deliver([copy.deepcopy(self.s)],'v3-shadow',self.now),1)
            send.assert_not_called()

    def test_same_symbol_open_blocks_next_signal(self):
        r=self.runner()
        with patch('scalp_sentinel.main.time.time',return_value=self.now/1000):
            self.assertEqual(r.deliver([self.s],'primary',self.now),1)
            new=copy.deepcopy(self.s)
            new.setup_id='different-pattern'
            self.assertEqual(r.deliver([new],'primary',self.now+60000),0)

    def test_stale_trigger_never_claimed_or_sent(self):
        r=self.runner()
        with patch('scalp_sentinel.main.time.time',return_value=self.now/1000+180):
            self.assertEqual(r.deliver([self.s],'primary',self.now),0)
            self.assertFalse(r.journal.memory_setups)

    def test_portfolio_cap_is_enforced(self):
        r=self.runner()
        r.journal.memory_trades=[dict(cohort='primary',symbol=str(i),side='LONG',status='OPEN',sent_at_ms=0) for i in range(3)]
        with patch('scalp_sentinel.main.time.time',return_value=self.now/1000):
            self.assertEqual(r.deliver([self.s],'primary',self.now),0)
