import unittest
from types import SimpleNamespace
from scalp_sentinel.engine import Candle, atr, ema, price_round, candidate, evaluate

class Indicators(unittest.TestCase):
    def test_ema(self): self.assertAlmostEqual(ema([3.0]*70, 21)[-1],3.0)
    def test_atr(self): self.assertAlmostEqual(atr([Candle(10,11,9,10,100,i) for i in range(40)]),2.0)
    def test_tick(self):
        self.assertEqual(price_round(80.222,"0.05",True),80.25)
        self.assertEqual(price_round(80.222,"0.05",False),80.20)

class Signals(unittest.TestCase):
    def make(self,side):
        down=side=="SHORT"
        def row(i,o,h,l,c,v=100): return [i*60000,str(o),str(h),str(l),str(c),str(v),i*60000+59999]
        slope=-0.005 if down else 0.005
        trend=[row(i,10.6+slope*i,10.63+slope*i,10.57+slope*i,10.6+slope*i) for i in range(120)]
        five=[row(i,10,10.03,9.97,10) for i in range(119)]
        one=[row(i,9.94 if down else 10.06,9.95 if down else 10.07,9.93 if down else 10.05,9.94 if down else 10.06) for i in range(119)]
        if down:
            five.append(row(119,9.99,10.01,9.92,9.93,180))
            one.append(row(119,9.94,9.945,9.905,9.91,190))
        else:
            five.append(row(119,10.01,10.08,9.99,10.07,180))
            one.append(row(119,10.06,10.095,10.055,10.09,190))
        snap={"1m":one,"5m":five,"15m":trend,"1h":trend,"4h":trend,
              "ticker":{"quoteVolume":"50000000"},
              "book":{"bidPrice":"9.909" if down else "10.089","askPrice":"9.911" if down else "10.091"},
              "taker":[{"timestamp":i*300000,"buyVol":str(70 if down and i>=9 else 130 if not down and i>=9 else 90 if down else 110),"sellVol":"100"} for i in range(12)],
              "oi":[{"timestamp":i*900000,"sumOpenInterest":str(1000000+i*1000)} for i in range(6)]}
        cfg=SimpleNamespace(min_quote_volume_usdt=10000000,max_spread_bps=10,min_rvol=1.15,min_stop_atr=0.6,max_stop_atr=3.0,score_threshold=6)
        return snap,cfg
    def test_long(self):
        s,c=self.make("LONG");z=candidate("TESTUSDT","LONG",s,"0.001",c)
        self.assertTrue(z.eligible,z.checks);self.assertTrue(z.stop<z.entry<z.tp1<z.tp2)
    def test_short(self):
        s,c=self.make("SHORT");z=candidate("TESTUSDT","SHORT",s,"0.001",c)
        self.assertTrue(z.eligible,z.checks);self.assertTrue(z.stop>z.entry>z.tp1>z.tp2)
    def test_bad_delta(self):
        s,c=self.make("SHORT")
        for v in s["taker"]:v["buyVol"]="110"
        z=candidate("TESTUSDT","SHORT",s,"0.001",c)
        self.assertFalse(z.eligible)
    def test_bad_liquidity(self):
        s,c=self.make("SHORT");s["ticker"]["quoteVolume"]="100"
        self.assertIsNone(candidate("TESTUSDT","SHORT",s,"0.001",c))
