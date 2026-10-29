"""V2 tracking semantics preserve V1 and move stop to break-even after TP1."""
import unittest
from scalp_sentinel.tracker import advance_trade, MINUTE


def bar(m, h, l):
    return [m * MINUTE, "0", str(h), str(l), "0"]


def paper(version):
    return dict(id=1,side="LONG",sent_at_ms=1000,entry_price=100.0,
        stop_price=99.0,tp1_price=101.5,tp2_price=102.5,
        status="AWAITING_ENTRY",filled_at_ms=None,tp1_at_ms=None,
        tp2_at_ms=None,stop_at_ms=None,last_checked_ms=0,
        finished_at_ms=None,strategy_version=version)


class V2Journal(unittest.TestCase):
    def test_breakeven_after_tp1(self):
        x=advance_trade(paper("v2"),[
            bar(1,100.2,99.9),bar(2,101.7,100.8),bar(3,100.4,99.8)])
        self.assertEqual(x["status"],"TP1_THEN_BREAKEVEN")
        self.assertEqual(x["stop_at_ms"],3*MINUTE)
        self.assertIsNotNone(x["tp1_at_ms"])
    def test_v1_original_stop_retained(self):
        x=advance_trade(paper("v1"),[
            bar(1,100.2,99.9),bar(2,101.7,100.8),bar(3,100.4,99.8)])
        self.assertEqual(x["status"],"TP1_HIT")
    def test_same_candle_be_and_tp2_ambiguous(self):
        x=advance_trade(paper("v2"),[
            bar(1,100.2,99.9),bar(2,101.7,100.8),bar(3,102.7,99.8)])
        self.assertEqual(x["status"],"TP1_AMBIGUOUS_EXIT")
