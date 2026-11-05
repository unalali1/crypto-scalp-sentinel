import unittest
from scalp_sentinel.promotion import daily_block_lower, promotion_report, DAY_MS


def trade(i, **changes):
    base=dict(symbol=f"SYM{i%5}",side="LONG" if i%2 else "SHORT",
              sent_at_ms=(i//2)*DAY_MS,status="TP2_HIT",
              journal_json=dict(regime="trend" if i<80 else "range",
                                net_r_low=.2,execution_cost_r=.02,costs_complete=True))
    base.update(changes)
    return base


class Promotion(unittest.TestCase):
    def test_day_resampling_preserves_intraday_clusters(self):
        rows=[dict(sent_at_ms=0,net_r_low=1),dict(sent_at_ms=0,net_r_low=-1),
              dict(sent_at_ms=DAY_MS,net_r_low=1),dict(sent_at_ms=DAY_MS,net_r_low=-1)]
        self.assertEqual(daily_block_lower(rows,.05,seed=3),0)

    def test_pass_requires_independent_null_and_conservative_costs(self):
        rows=[trade(i) for i in range(160)]
        result=promotion_report(rows,"test",null_percentile=.97,draws=1000)
        self.assertTrue(result["pass_all"],result["reasons"])
        self.assertAlmostEqual(result["doubled_cost_mean"],.18)
        self.assertFalse(promotion_report(rows,"test",draws=1000)["pass_all"])

    def test_missing_funding_open_and_gap_do_not_become_winners(self):
        rows=[trade(i) for i in range(160)]
        rows[0]=trade(0,status="DATA_GAP")
        rows[1]=trade(1,status="OPEN")
        rows[2]=trade(2,journal_json=dict(net_r_low=5,costs_complete=False))
        rows[3]=trade(3,status="NO_ENTRY")
        r=promotion_report(rows,"test",null_percentile=1,draws=1000)
        self.assertEqual(r["decided"],157)
        self.assertEqual(r["incomplete"],1)
        self.assertAlmostEqual(r["data_gap_rate"],1/159)
        self.assertIn("incomplete_costs_or_outcomes",r["reasons"])

    def test_strong_mean_cannot_bypass_symbol_concentration_or_span(self):
        rows=[trade(i,symbol="ONE",sent_at_ms=i*DAY_MS//10) for i in range(160)]
        r=promotion_report(rows,"test",null_percentile=1,draws=1000)
        self.assertIn("positive_return_concentration",r["reasons"])
        self.assertIn("insufficient_calendar_span",r["reasons"])


if __name__=="__main__":
    unittest.main()
