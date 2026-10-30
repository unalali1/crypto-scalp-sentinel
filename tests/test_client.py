import unittest
from scalp_sentinel.binance_client import BinanceClient, BinanceError, DataPending


class FakeClient(BinanceClient):
    now=1800000003000
    def __init__(self):
        super().__init__('unused')
        self.calls=[]
        self.gap=False
        self.stale=False
    def symbol_info(self):
        return {s:{'filters':[{'filterType':'PRICE_FILTER','tickSize':'0.01'}]} for s in ('TEST','BTCUSDT')}
    def get(self,path,**params):
        self.calls.append((path,params))
        if path.endswith('/time'):return {'serverTime':self.now}
        if path.endswith('klines'):
            d={'1m':60000,'5m':300000,'15m':900000,'1h':3600000,'4h':14400000}[params['interval']]
            end=self.now//d*d
            rows=[[end-(140-i)*d,'100','101','99','100','10',end-(139-i)*d-1] for i in range(140)]
            if self.gap: rows.pop(100)
            return rows
        if 'Hist' in path or 'Ratio' in path:
            d=300000 if params['period']=='5m' else 900000
            end=self.now//d*d-(10*d if self.stale else 0)
            return [dict(timestamp=end-(39-i)*d,buyVol='12',sellVol='10',sumOpenInterest='1000') for i in range(40)]
        if 'bookTicker' in path:return {'bidPrice':'100','askPrice':'100.01'}
        return {'quoteVolume':'50000000'}


class ClientTests(unittest.TestCase):
    def test_cache_reuses_higher_timeframes_between_minutes(self):
        c=FakeClient()
        a=c.snapshot('TEST',c.now)
        n=len(c.calls)
        c.now+=60000
        c.snapshot('TEST',c.now)
        new=c.calls[n:]
        self.assertEqual(len(new),2) # 1m candles and fresh book only
        self.assertTrue(all(int(r['timestamp'])+300000<=a['asof_ms'] for r in a['taker']))

    def test_gap_and_stale_flow_fail_closed(self):
        for flag in ('gap','stale'):
            c=FakeClient()
            setattr(c,flag,True)
            with self.assertRaises(BinanceError):c.snapshot('TEST',c.now)
            if flag=='gap':self.assertFalse(c._cache)
            else:self.assertNotIn(('TEST','taker'),c._cache)

    def test_delayed_publication_retries_flow_without_reloading_all_candles(self):
        c=FakeClient()
        c.stale=True
        with self.assertRaises(DataPending):c.snapshot('TEST',c.now)
        self.assertIn(('TEST','4h'),c._cache)
        self.assertNotIn(('TEST','taker'),c._cache)
        # Model all statistics catching up; cached stale OI is retried too.
        c.stale=False
        for _ in range(3):
            try:
                s=c.snapshot('TEST',c.now)
                break
            except DataPending:
                continue
        else:self.fail('publication did not recover')
        self.assertTrue(s['taker'])
