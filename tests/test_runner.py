import copy
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from scalp_sentinel.main import Runner
from scalp_sentinel.journal import Journal
from scalp_sentinel.engine import candidate
from test_v2_engine import fixture
from test_execution_research import config
from test_client import FakeClient
from scalp_sentinel.binance_client import DataPending


class Delivery(unittest.TestCase):
    def test_context_alerts_are_batched_without_creating_trade_signals(self):
        r=Runner.__new__(Runner)
        r.cfg=config(dry_run=False)
        r.journal=Journal()
        r.fib_digest_queue={}
        r.last_fib_digest_bucket=10
        zones=[]
        confluences=[]
        for symbol in ('BTCUSDT','ETHUSDT'):
            zones.append(dict(alert_id=f'{symbol}:zone',zone_id=f'{symbol}:zone',
                symbol=symbol,low=99,high=101,center=100,price=100,distance=0,
                sources=['D open','W close'],role='support',strength='strong',atr1h=2))
            confluences.append(dict(alert_id=f'{symbol}:pivot',symbol=symbol,
                side='LONG',timeframe='5m',indicators=['RSI','MACD'],
                zone_low=99,zone_high=101,pivot_ts=1))
        with patch('scalp_sentinel.main.send_telegram') as send:
            self.assertEqual(r.deliver_confluences(confluences,10*900000),1)
            self.assertEqual(r.deliver_price_zones(zones,10*900000),1)
        self.assertEqual(send.call_count,2)
        self.assertIn('BTCUSDT',send.call_args_list[0].args[2])
        self.assertIn('ETHUSDT',send.call_args_list[1].args[2])
        self.assertFalse(r.journal.memory_trades)

    def test_eligible_signal_is_delivered_before_slow_other_symbol(self):
        client=FakeClient()
        snap,thresholds=fixture()
        signal=candidate('TEST','LONG',snap,'0.001',thresholds)
        now=signal.snapshot_ts+63000
        client.now=now
        client.symbol_info=lambda: {s:{'filters':[{'filterType':'PRICE_FILTER','tickSize':'0.001'}]}
                                    for s in ('TEST','SLOW','BTCUSDT')}
        delivered=threading.Event()
        slow_timed_out=[]
        def snapshot(sym,asof):
            if sym=='TEST':
                result=copy.deepcopy(snap)
                result['asof_ms']=asof
                return result
            if sym=='SLOW':
                if not delivered.wait(.5):
                    slow_timed_out.append(True)
            raise DataPending(sym,'oi',900000,0)
        def get(path,**_kwargs):
            if path.endswith('/time'):return {'serverTime':now}
            return {'bidPrice':signal.entry-.001,'askPrice':signal.entry+.001,'time':now}
        client.snapshot=snapshot
        client.get=get
        r=Runner(config(symbols=('TEST','SLOW'),shadow_enabled=False,
                        fee_bps=0,slippage_bps=0),client)
        original=r.deliver
        def deliver(signals,cohort,asof):
            count=original(signals,cohort,asof)
            if cohort=='primary' and count:delivered.set()
            return count
        r.deliver=deliver
        try:
            with patch('scalp_sentinel.main.time.time',return_value=now/1000),\
                 patch('scalp_sentinel.main.detect_confluences',return_value=[]),\
                 patch('scalp_sentinel.main.detect_price_zones',return_value=[]):
                self.assertEqual(r.tick(now),1)
            self.assertTrue(delivered.is_set())
            self.assertFalse(slow_timed_out)
        finally:r.close()

    def test_pending_discovery_is_retried_next_minute(self):
        client=FakeClient()
        r=Runner(config(symbols=('TEST','BTCUSDT')),client)
        original=client.snapshot
        def snapshot(sym,now):
            if sym=='TEST' and now==client.now:
                raise DataPending(sym,'oi5',500000,40)
            return original(sym,now)
        try:
            with patch.object(client,'snapshot',side_effect=snapshot),patch('scalp_sentinel.main.time.time',return_value=client.now/1000):
                r.tick(client.now)
            self.assertIn('TEST',r.pending)
            client.now+=60000
            with patch('scalp_sentinel.main.time.time',return_value=client.now/1000):
                r.tick(client.now)
            self.assertNotIn('TEST',r.pending)
            self.assertIn('TEST',r.snapshots)
        finally:r.close()
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
