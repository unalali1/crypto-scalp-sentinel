import copy
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from scalp_sentinel.main import Runner
from scalp_sentinel.standard_quality import rank
from scalp_sentinel.journal import SCORE_LOW_COHORT, paper_trade
from scalp_sentinel.execution import prepare
from scalp_sentinel.forward_report import score_comparison, add_review_readiness
from scalp_sentinel.notifier import weakest_components, format_signal
from test_standard_forward import settings, signal, snap, NOW, bar
from scalp_sentinel.tracker import advance_trade


def force_score(value):
    def ranking(s,*args):
        rank(s,*args)
        s.quality_score=value
        s.journal['quality_score']=value
        return value
    return ranking


class ScoreFilter(unittest.TestCase):
    def test_review_needs_both_a_week_and_100_settled_high_score_trades(self):
        for days,count,expected in ((6.99,100,False),(7,99,False),(7,100,True)):
            report=dict(first_observed_ms=NOW,groups={'above':{'resolved':count}})
            self.assertEqual(add_review_readiness(report,NOW+days*86400000)['review_ready'],expected)

    def test_threshold_is_inclusive_and_silent_route_is_deduplicated(self):
        for score,expected in ((59.99,0),(60,1),(60.01,1)):
            with self.subTest(score=score):
                r=Runner(settings(standard_min_score=60,standard_exit_shadows=False),
                         SimpleNamespace(get=lambda *_a,**_kw:snap()['book']))
                r.snapshots={'TEST':snap()}
                try:
                    with patch('scalp_sentinel.main.time.time',return_value=NOW/1000), \
                         patch('scalp_sentinel.main.rank',side_effect=force_score(score)):
                        self.assertEqual(r.deliver([signal()],'primary',NOW),expected)
                        self.assertEqual(r.deliver([signal()],'primary',NOW),0)
                    self.assertEqual(len(r.journal.memory_trades),1)
                    t=r.journal.memory_trades[0]
                    self.assertEqual(t['cohort'],SCORE_LOW_COHORT if score<60 else 'primary')
                    self.assertEqual(t['journal_json']['score_threshold'],60)
                    # Silent ideas use the same execution policy and resolve.
                    ts=t['last_checked_ms']+60000
                    entered=advance_trade(t,[bar(ts,t['entry_price']+.02,t['entry_price']+.05,t['entry_price']-.02,t['entry_price']+.03)])
                    self.assertEqual(entered['status'],'OPEN')
                finally:r.close()

    def test_weakest_two_use_fraction_of_available_points(self):
        result=weakest_components({'quality_components':{'btc':4,'trend':6,'structure':3,'costs':8}})
        self.assertIn('Destek/dirence kadar alan',result)
        self.assertIn('Trend gücü',result)
        self.assertNotIn('BTC',result)
        s=signal(); prepare(s,snap()['book'],NOW,settings()); rank(s,snap(),{'TEST':snap()},[],settings())
        self.assertIn('Teknik uygunluk puanı:',format_signal(s,'Asia/Shanghai'))
        self.assertNotIn('Kalite:',format_signal(s,'Asia/Shanghai'))

    def test_comparison_excludes_old_policy_and_open_results(self):
        s=signal(); prepare(s,snap()['book'],NOW,settings())
        s.journal.update(notification_policy='technical-score-v1',score_threshold=60,score_group='above')
        win=paper_trade(s,NOW); win.update(status='TIMEOUT',filled_at_ms=NOW,finished_at_ms=NOW+60000)
        win['journal_json'].update(costs_complete=True,net_r_low=1,net_r_high=1)
        low=copy.deepcopy(win); low['journal_json'].update(score_group='below',net_r_low=-1,net_r_high=-1)
        active=copy.deepcopy(win); active.update(status='OPEN',finished_at_ms=None)
        old=copy.deepcopy(win); old['journal_json']['notification_policy']='independent-ideas-v1'
        report=score_comparison([win,low,active,old])
        self.assertEqual(report['groups']['above']['signals'],2)
        self.assertEqual(report['groups']['above']['resolved'],1)
        self.assertEqual(report['groups']['above']['mean_net_r_low'],1)
        self.assertEqual(report['groups']['below']['mean_net_r_low'],-1)
