"""V2 closed-candle retest, cross-timeframe, and risk regression tests."""
import unittest
from types import SimpleNamespace
from scalp_sentinel.engine import candidate, prefilter_rejections


def fixture(side="LONG"):
    long = side == "LONG"
    sign = 1 if long else -1
    anchor = 10 if long else 20
    def bar(m, o, h, l, c, v=100, duration=60000):
        return [m * 60000, str(o), str(h), str(l), str(c), str(v), m * 60000 + duration - 1]
    trend = []
    for i in range(140):
        close = anchor + sign * (i - 139) * 0.004
        trend.append(bar(i * 15, close, close + 0.02, close - 0.02, close, duration=900000))
    five = []
    for i in range(140):
        five.append(bar(i * 5, anchor, anchor + 0.025, anchor - 0.025, anchor, duration=300000))
    level = anchor + 0.025 if long else anchor - 0.025
    # Breakout at index 137, fully closed by the 1m post-breakout retest.
    if long:
        five[137] = bar(685, anchor + 0.025, anchor + 0.085, anchor + 0.018, anchor + 0.060, 190, 300000)
        five[138] = bar(690, anchor + 0.060, anchor + 0.080, anchor + 0.018, anchor + 0.050, 130, 300000)
        five[139] = bar(695, anchor + 0.050, anchor + 0.075, anchor + 0.024, anchor + 0.065, 130, 300000)
    else:
        five[137] = bar(685, anchor - 0.025, anchor - 0.018, anchor - 0.085, anchor - 0.060, 190, 300000)
        five[138] = bar(690, anchor - 0.060, anchor - 0.018, anchor - 0.080, anchor - 0.050, 130, 300000)
        five[139] = bar(695, anchor - 0.050, anchor - 0.024, anchor - 0.075, anchor - 0.065, 130, 300000)
    one = [bar(i, anchor, anchor + 0.004, anchor - 0.004, anchor)
           for i in range(560, 700)]
    # Retest candle 697 occurs after 5m breakout close at minute 690.
    if long:
        one[-3] = bar(697, anchor + 0.042, anchor + 0.055, anchor + 0.021, anchor + 0.048)
        one[-2] = bar(698, anchor + 0.048, anchor + 0.061, anchor + 0.043, anchor + 0.051)
        one[-1] = bar(699, anchor + 0.051, anchor + 0.070, anchor + 0.050, anchor + 0.065)
    else:
        one[-3] = bar(697, anchor - 0.042, anchor - 0.021, anchor - 0.055, anchor - 0.048)
        one[-2] = bar(698, anchor - 0.048, anchor - 0.043, anchor - 0.061, anchor - 0.051)
        one[-1] = bar(699, anchor - 0.051, anchor - 0.050, anchor - 0.070, anchor - 0.065)
    taker = [
        {"timestamp": i * 300000,
         "buyVol": str(140 if long and i >= 9 else 70 if not long and i >= 9 else 100),
         "sellVol": "100"} for i in range(12)
    ]
    oi = [{"timestamp":i*900000,"sumOpenInterest":str(1_000_000 + i*10_000)}
          for i in range(6)]
    snap = {
        "1m":one,"5m":five,"15m":trend,"1h":trend,"4h":trend,
        "taker":taker,"oi":oi,"ticker":{"quoteVolume":"50000000"},
        "book":{"bidPrice":str(anchor - 0.002),"askPrice":str(anchor + 0.002)},
    }
    cfg=SimpleNamespace(v2_enabled=True,min_quote_volume_usdt=10_000_000,
        max_spread_bps=10,min_rvol=1.15,min_stop_atr=0.6,max_stop_atr=3.0,
        score_threshold=5)
    return snap, cfg


class V2Engine(unittest.TestCase):
    def test_long_with_retest(self):
        s,c=fixture("LONG")
        x=candidate("TEST","LONG",s,"0.001",c)
        self.assertEqual(x.strategy_version,"v2")
        self.assertTrue(x.eligible,x.checks)
        self.assertTrue(x.stop < x.entry < x.tp1 < x.tp2)
    def test_short_with_retest(self):
        s,c=fixture("SHORT")
        x=candidate("TEST","SHORT",s,"0.001",c)
        self.assertTrue(x.eligible,x.checks)
        self.assertTrue(x.stop > x.entry > x.tp1 > x.tp2)
    def test_prefilter_diagnostics_identify_liquidity_and_spread(self):
        s,c=fixture("LONG")
        s["ticker"]["quoteVolume"]="1"
        s["book"]={"bidPrice":"100","askPrice":"101"}
        self.assertEqual(set(prefilter_rejections(s,c)),{"quote_volume","spread"})

    def test_no_retest_blocks(self):
        s,c=fixture("LONG")
        for row in s["1m"][-6:-1]:
            row[3] = "10.10"
        x=candidate("TEST","LONG",s,"0.001",c)
        self.assertFalse(x.eligible)
        self.assertFalse(x.checks["1m trigger"])
    def test_opposing_4h_blocks(self):
        s,c=fixture("LONG")
        for row in s["4h"]:row[4]="8.0"
        x=candidate("TEST","LONG",s,"0.001",c)
        self.assertFalse(x.checks["15m trend"])
        self.assertFalse(x.eligible)
    def test_negative_short_oi_blocks(self):
        s,c=fixture("SHORT")
        for i,row in enumerate(s["oi"]):
            row["sumOpenInterest"]=str(1_000_000-i*10_000)
        x=candidate("TEST","SHORT",s,"0.001",c)
        self.assertFalse(x.checks["CVD + OI"])
        self.assertFalse(x.eligible)
    def test_no_volume_not_automatically_failure_for_long(self):
        s,c=fixture("LONG")
        for row in s["5m"][-3:]:row[5]="25"
        x=candidate("TEST","LONG",s,"0.001",c)
        self.assertFalse(x.checks["relative volume"])
        self.assertTrue(x.eligible,x.checks)
