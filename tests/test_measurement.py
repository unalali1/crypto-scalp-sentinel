import unittest
from scalp_sentinel.tracker import advance_trade
from scalp_sentinel.performance import outcome_bounds, settle_funding, summarize
from test_v2_tracker import paper, bar


class Measurement(unittest.TestCase):
    def test_stop_before_fill_invalidates_v2(self):
        x = advance_trade(paper("v2"), [bar(1,99.5,98.5),bar(2,100.2,99.9)])
        self.assertEqual(x["status"], "INVALIDATED")
        self.assertIsNone(x["filled_at_ms"])

    def test_same_bar_tp1_be_tp2_is_ambiguous(self):
        x = advance_trade(paper("v2"), [bar(1,100.2,99.9),bar(2,102.7,99.5)])
        self.assertEqual(x["status"], "TP1_AMBIGUOUS_EXIT")
        self.assertIsNone(x["tp2_at_ms"])
        self.assertIsNotNone(x["tp1_at_ms"])

    def test_new_window_expires_and_target_invalidates(self):
        t = paper("v2.1")
        t["journal_json"] = {"entry_window_ms":180000, "invalidate_target_before_entry":True}
        self.assertEqual(advance_trade(t,[bar(1,102,101.6)])["status"], "INVALIDATED")
        x = advance_trade(t,[bar(i,100.5,100.1) for i in range(1,5)])
        self.assertEqual(x["status"], "NO_ENTRY")

    def test_partial_exit_economics_and_funding(self):
        t=paper("v2.1")
        t.update(status="TP1_THEN_BREAKEVEN",filled_at_ms=60000,
                 tp1_at_ms=120000,finished_at_ms=300000,
                 journal_json={"tp1_fraction":0.5,"fee_bps":5,"slippage_bps":2})
        p=outcome_bounds(t,100)
        self.assertAlmostEqual(p["gross_r_low"],.75)
        self.assertAlmostEqual(p["net_r_low"],.61)
        t["journal_json"].update(p)
        settle_funding(t,[{"fundingTime":240000,"fundingRate":"0.001","markPrice":"100"}])
        self.assertAlmostEqual(t["journal_json"]["net_r_low"],.56)
        settle_funding(t,[{"fundingTime":240000,"fundingRate":"0.001","markPrice":"100"}])
        self.assertAlmostEqual(t["journal_json"]["net_r_low"],.56)

    def test_missing_costs_excluded_from_net_summary(self):
        t=paper("v2.1")
        t.update(symbol="TEST",status="TP2_HIT",journal_json={"net_r_low":2,"costs_complete":False})
        self.assertEqual(summarize([t])[0]["resolved"],0)
