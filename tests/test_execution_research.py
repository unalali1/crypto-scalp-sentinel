import copy
import unittest
from dataclasses import replace
from unittest.mock import patch
from scalp_sentinel.config import Config
from scalp_sentinel.engine import candidate
from scalp_sentinel.execution import prepare
from scalp_sentinel.research import candidate_v3, correlation, correlated, regime, v2_shadow_variants
from scalp_sentinel.journal import Journal
from test_v2_engine import fixture


def config(**kwargs):
    with patch.dict('os.environ',{},clear=True):
        cfg=Config.from_env()
    return replace(cfg,**kwargs)


class Execution(unittest.TestCase):
    def setUp(self):
        snap,cfg=fixture('LONG')
        self.s=candidate('TEST','LONG',snap,'0.001',cfg)
        self.cfg=config(fee_bps=0,slippage_bps=0)
        self.now=self.s.snapshot_ts+63000
        self.book={'bidPrice':self.s.entry-.001,'askPrice':self.s.entry+.001,'time':self.now}

    def test_close_quote_passes_and_saves_policy(self):
        self.assertIsNone(prepare(self.s,self.book,self.now,self.cfg))
        self.assertEqual(self.s.journal['entry_window_ms'],300000)
        self.assertEqual(self.s.journal['tp1_fraction'],.5)

    def test_reject_far_quote(self):
        self.book.update(bidPrice=self.s.entry+1,askPrice=self.s.entry+1.001)
        self.assertEqual(prepare(self.s,self.book,self.now,self.cfg),'price_drift')

    def test_reject_stale_quote_and_candle(self):
        self.book['time']=self.now-20000
        self.assertEqual(prepare(self.s,self.book,self.now,self.cfg),'stale_quote')
        self.assertEqual(prepare(self.s,self.book,self.now+180000,self.cfg),'stale_trigger')
        del self.book['time']
        self.assertEqual(prepare(self.s,self.book,self.now,self.cfg),'missing_quote_time')

    def test_small_stop_cannot_hide_execution_cost(self):
        self.s.stop=self.s.entry-.01
        self.assertEqual(prepare(self.s,self.book,self.now,config()),'cost_to_risk')

    def test_setup_dedup_survives_multiple_ticks_in_memory(self):
        j=Journal()
        self.assertTrue(j.claim('primary',self.s,self.now))
        self.assertFalse(j.claim('primary',self.s,self.now+60000))
        self.assertTrue(j.claim('v3-shadow',self.s,self.now))

    def test_invalid_configuration_fails_early(self):
        for cfg in (config(entry_window_minutes=60),config(fee_bps=-1),config(primary_strategy='unknown')):
            with self.assertRaises(ValueError):cfg.validate()


class Research(unittest.TestCase):
    def test_shadow_filters_only_keep_qualified_baseline_and_do_not_mutate_it(self):
        snap,thresholds=fixture()
        base=candidate('TEST','LONG',snap,'0.001',thresholds)
        base.research=regime(snap)
        original=copy.deepcopy(base)
        variants=v2_shadow_variants(base,snap,700*60000)
        self.assertIn('v2.1-volume',[v.strategy_version for v in variants])
        self.assertEqual(base,original)
        for row in snap['5m'][-3:]:row[5]='25'
        quiet=candidate('TEST','LONG',snap,'0.001',thresholds)
        quiet.research=regime(snap)
        self.assertTrue(quiet.eligible)
        self.assertNotIn('v2.1-volume',
                         [v.strategy_version for v in v2_shadow_variants(quiet,snap,700*60000)])

    def test_confirmed_three_phase_volume_can_pass_v3(self):
        snap,_=fixture()
        snap['oi5']=copy.deepcopy(snap['oi'])
        for row in snap['1m'][-6:-1]:row[5]='30'
        snap['1m'][-1][5]='40'
        s=candidate_v3('TEST','LONG',snap,'0.001',config(),snap)
        self.assertTrue(s.eligible,s.checks)

    def test_flat_regime_is_not_a_trend(self):
        s,_=fixture()
        for row in s['15m']:row[4]='10'
        self.assertEqual(regime(s)['regime'],'range')

    def test_missing_btc_blocks_v3_and_v2_is_not_mutated(self):
        snap,_=fixture()
        snap['oi5']=copy.deepcopy(snap['oi'])
        before=copy.deepcopy(snap)
        s=candidate_v3('TEST','LONG',snap,'0.001',config(),None)
        self.assertFalse(s.eligible)
        self.assertFalse(s.checks['BTC context'])
        self.assertEqual(snap,before)

    def test_weak_breakout_is_rejected_even_if_trigger_volume_high(self):
        snap,_=fixture()
        snap['oi5']=copy.deepcopy(snap['oi'])
        snap['5m'][137][5]='50'
        snap['1m'][-1][5]='1000'
        s=candidate_v3('TEST','LONG',snap,'0.001',config(),snap)
        self.assertFalse(s.checks['relative volume'])

    def test_correlation_uses_matched_timestamps(self):
        a={i:(i%7)/100 for i in range(60)}
        self.assertAlmostEqual(correlation(a,a),1)
        self.assertAlmostEqual(correlation(a,{k:-v for k,v in a.items()}),-1)
        self.assertIsNone(correlation(a,{1000+i:v for i,v in a.items()}))

    def test_same_symbol_and_unknown_open_exposure_block(self):
        snap,cfg=fixture()
        s=candidate('TEST','LONG',snap,'0.001',cfg)
        self.assertTrue(correlated(s,[{'symbol':'TEST','side':'SHORT'}],{'TEST':snap},.8))
        self.assertTrue(correlated(s,[{'symbol':'MISSING','side':'LONG'}],{'TEST':snap},.8))
