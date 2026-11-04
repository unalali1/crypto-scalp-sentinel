import json
import unittest

from scalp_sentinel.journal import Journal
from scalp_sentinel.report import delivery_funnel


class DeliveryFunnel(unittest.TestCase):
    def test_unresolved_and_incomplete_costs_cannot_improve_net_return(self):
        base=dict(cohort='primary',sent_at_ms=100,filled_at_ms=110)
        trades=[dict(base,status='STOPPED',journal_json=dict(costs_complete=True,
                    net_r_low=-1.2,net_r_high=-1.2,spread_bps=4.0,quote_age_ms=100)),
                dict(base,status='NO_ENTRY',filled_at_ms=None,journal_json={}),
                dict(base,status='TP2_HIT',journal_json=dict(costs_complete=False,
                    net_r_low=2.0)),
                dict(base,status='OPEN',journal_json={})]
        result=delivery_funnel(trades,[('primary','sent'),('primary','spread')],
            [('v2.1',True),('v2.1',False)],
            [(90,91,'BTC','LONG',5,2.0),(90,92,'ETH','LONG',7,1.2)],0)
        primary=result['cohorts']['primary']
        self.assertEqual(primary['sent'],4)
        self.assertEqual(primary['decided'],2)
        self.assertEqual(primary['filled'],3)
        self.assertEqual(primary['decisions'],{'sent':1,'spread':1})
        self.assertAlmostEqual(primary['mean_net_r_per_decided_alert_low'],-.6)
        self.assertEqual(primary['median_quote_age_ms'],100)
        self.assertEqual(result['eligible_by_version'],{'v2.1':1})
        self.assertEqual(result['arrival_order']['first_below_best_rank'],1)

    def test_arrival_order_is_scoped_to_scan_and_retention_window(self):
        arrivals=[(10,11,'A','LONG',10,1.0),(10,12,'B','LONG',10,2.0),
                  (20,21,'C','SHORT',3,1.0),(20,22,'D','SHORT',2,3.0)]
        result=delivery_funnel([],[],[],arrivals,0)
        self.assertEqual(result['arrival_order']['multi_candidate_scans'],2)
        self.assertEqual(result['arrival_order']['first_below_best_rank'],1)

    def test_arrivals_are_batched_after_notification_path(self):
        class Conn:
            def __init__(self):
                self.calls=[]
                self.commits=0
            def execute(self,sql,args):
                self.calls.append((sql,args))
            def commit(self):
                self.commits+=1
        journal=Journal()
        journal.conn=Conn()
        journal.record_arrivals('v2.1',42,[('BTC','LONG',7,1.5,43),('ETH','SHORT',8,2.1,44)])
        self.assertEqual(len(journal.conn.calls),1)
        self.assertEqual(journal.conn.commits,1)
        self.assertEqual(len(json.loads(journal.conn.calls[0][1][2])),2)


if __name__=='__main__':
    unittest.main()
