import unittest
from test_client import FakeClient
from scalp_sentinel.replay import validate_frame,replay
from test_execution_research import config


class Replay(unittest.TestCase):
    def test_future_price_and_flow_rejected(self):
        for part in ('candle','flow'):
            c=FakeClient()
            s=c.snapshot('BTCUSDT',c.now)
            if part=='candle':s['1m'][-1][6]=c.now+10000
            else:s['oi5'][-1]['timestamp']=c.now+10000
            with self.assertRaises(ValueError):validate_frame(s,c.now)

    def test_flat_market_does_not_create_replay_winners(self):
        c=FakeClient()
        s=c.snapshot('BTCUSDT',c.now)
        s['tick_size']='0.01'
        for version in ('v2.1','v3'):
            self.assertEqual(replay([(c.now,'BTCUSDT',s)],config(),version),[])
