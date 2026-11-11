"""Regression coverage for executable references, risk and forward reporting."""
import copy
import unittest
from dataclasses import replace
from unittest.mock import patch, Mock
from types import SimpleNamespace
from scalp_sentinel.config import Config
from scalp_sentinel.standard import assess
from scalp_sentinel.standard_quality import rank, exposure, regime_context, risk_plan
from scalp_sentinel.execution import prepare
from scalp_sentinel.journal import Journal, paper_trade
from scalp_sentinel.tracker import advance_trade
from scalp_sentinel.performance import outcome_bounds, settle_funding
from scalp_sentinel.forward_report import summarize_window, format_digest
from scalp_sentinel.main import Runner
from scalp_sentinel.binance_client import BinanceClient, BinanceError
from test_standard import snapshot
from test_execution_research import config

NOW=1800000003000


def settings(**kwargs):
    return replace(config(),primary_strategy='standard',standard_enhanced=True,
        max_cost_r=.4, **kwargs)


def snap(side='LONG'):
    s=snapshot(side)
    for tf,duration in (('5m',300000),('15m',900000)):
        shift=NOW//duration*duration-duration-int(s[tf][-1][0])
        for row in s[tf]:
            row[0]+=shift
            row[6]+=shift
    close=float(s['5m'][-1][4])
    s['book']=dict(bidPrice=close-.01,askPrice=close+.01,bidQty=1000,askQty=1000,time=NOW)
    s['asof_ms']=NOW
    return s


def signal(side='LONG',symbol='TEST'):
    s=next(x for x in assess(symbol,snap(side),'0.01',settings())[0]
           if x.side==side and x.strategy_version=='ema-pullback')
    # Pricing tests exercise the economic contract independently of selection.
    return s


def bar(ts,o,h,l,c):
    return [ts,str(o),str(h),str(l),str(c),'100',ts+59999]


class StandardForward(unittest.TestCase):
    def test_passive_entry_is_on_correct_side_of_book_both_directions(self):
        for side in ('LONG','SHORT'):
            s=signal(side)
            book=snap(side)['book']
            self.assertIsNone(prepare(s,book,NOW,settings()))
            self.assertLessEqual(s.entry,book['bidPrice']) if side=='LONG' else self.assertGreaterEqual(s.entry,book['askPrice'])
            p=s.journal
            self.assertEqual(p['cost_model'],'paper-v2')
            expected=(2*p['fee_bps']+p['spread_bps']/2+p['slippage_bps'])/10000*s.entry/abs(s.entry-s.stop)
            self.assertAlmostEqual(p['estimated_cost_r'],expected)
            self.assertGreater(p['top_book_notional_usdt'],0)

    def test_book_data_fail_closed_and_trigger_expiry(self):
        for key,value in [('bidQty',0),('askQty','NaN'),('bidPrice','Infinity')]:
            b=snap()['book']; b[key]=value
            self.assertEqual(prepare(signal(),b,NOW,settings()),'invalid_quote')
        b=snap()['book']; del b['askQty']
        self.assertEqual(prepare(signal(),b,NOW,settings()),'missing_or_invalid_book')
        b=snap()['book']; b['time']=NOW-16000
        self.assertEqual(prepare(signal(),b,NOW,settings()),'stale_quote')
        self.assertEqual(prepare(signal(),snap()['book'],NOW+91000,settings()),'stale_trigger')

    def test_unclosed_and_stale_context_rejected(self):
        for tf in ('5m','15m'):
            s=snap(); s[tf][-1][6]=NOW+1
            with self.assertRaises(ValueError):assess('TEST',s,'0.01',settings())
        s=snap(); s['asof_ms']=NOW+300000
        with self.assertRaises(ValueError):assess('TEST',s,'0.01',settings())

    def test_rank_is_explainable_bounded_and_deterministic(self):
        s=signal(); data=snap()
        prepare(s,data['book'],NOW,settings())
        score=rank(s,data,{'TEST':data,'BTCUSDT':data},[],settings())
        self.assertGreaterEqual(score,0); self.assertLessEqual(score,100)
        self.assertEqual(len(s.journal['quality_components']),10)
        self.assertAlmostEqual(score,sum(s.journal['quality_components'].values()),places=1)
        self.assertEqual(score,rank(s,data,{'TEST':data,'BTCUSDT':data},[],settings()))

    def test_correlated_risk_and_missing_peer_are_blocked(self):
        s=signal(); data=snap()
        peer=dict(symbol='OTHER',side='LONG',journal_json={'risk_pct':.35})
        self.assertEqual(exposure(s,[peer],{'TEST':data,'OTHER':data},settings()),'correlated_risk_limit')
        self.assertEqual(exposure(s,[peer],{'TEST':data},settings()),'correlation_data_missing')
        self.assertEqual(exposure(s,[peer,dict(peer,symbol='THIRD')],{'TEST':data},settings()),'total_risk_limit')

    def test_risk_sizes_without_account_balance_and_margin_scales_correctly(self):
        p=risk_plan(100,99,.1,settings())
        self.assertAlmostEqual(p['notional_equity_multiple'],.0035/.011)
        self.assertAlmostEqual(p['margin_equity_pct']['10']/p['margin_equity_pct']['15'],1.5)
        self.assertNotIn('account_balance',p)

    def test_extreme_shock_pauses_both_setups(self):
        data=snap()
        data['5m'][-1][2]='110'
        data['5m'][-1][3]='90'
        result,_=assess('TEST',data,'0.01',settings())
        self.assertEqual(regime_context(data)['regime'],'SHOCK')
        self.assertTrue(all(not s.checks['extreme volatility pause'] for s in result))
        self.assertFalse(any(s.eligible for s in result))

    def test_delay_and_limit_touch_are_not_guaranteed_fills(self):
        s=signal(); prepare(s,snap()['book'],NOW,settings())
        trade=paper_trade(s,NOW)
        start=trade['last_checked_ms']+60000
        e=s.entry
        # A touch without penetration is not sufficient.
        out=advance_trade(trade,[bar(start,e+.02,e+.05,e,e+.03)])
        self.assertEqual(out['status'],'AWAITING_ENTRY')
        out=advance_trade(out,[bar(start+60000,e+.02,e+.05,e-.02,e+.03)])
        self.assertEqual(out['status'],'OPEN')
        self.assertEqual(out['filled_at_ms'],start+60000)

    def test_market_delay_embeds_entry_friction_once_and_rounds_tick(self):
        s=signal(); prepare(s,snap()['book'],NOW,settings(max_entry_drift_atr=.3))
        t=paper_trade(s,NOW); t['journal_json']['entry_mode']='market_delayed'
        ts=t['last_checked_ms']+60000
        out=advance_trade(t,[bar(ts,100.10,100.20,100.0,100.15)])
        self.assertEqual(out['status'],'OPEN')
        self.assertAlmostEqual(out['entry_price'],100.14)
        self.assertTrue(out['journal_json']['entry_friction_embedded'])
        out.update(status='STOPPED')
        p=out['journal_json']
        costs=outcome_bounds(out,99)
        expected=(out['entry_price']*p['fee_bps']+out['stop_price']*(p['fee_bps']+p['exit_friction_bps']))/10000/abs(out['entry_price']-out['stop_price'])
        self.assertAlmostEqual(costs['execution_cost_r'],expected)

    def test_stop_and_target_ambiguity_not_a_win_and_gap_stop_is_worse(self):
        s=signal(); prepare(s,snap()['book'],NOW,settings())
        t=paper_trade(s,NOW); ts=t['last_checked_ms']+60000
        t.update(status='OPEN',filled_at_ms=ts-60000)
        out=advance_trade(t,[bar(ts,s.entry,s.tp1+.01,s.stop-.01,s.entry)])
        self.assertEqual(out['status'],'AMBIGUOUS_EXIT')
        self.assertLess(out['journal_json']['net_r_low'],0)
        out=advance_trade(t,[bar(ts,s.stop-.10,s.tp1+.01,s.stop-.20,s.entry)])
        self.assertEqual(out['status'],'STOPPED')
        self.assertLess(out['journal_json']['gross_r_low'],-1)

    def test_invalid_candles_and_missing_minute_do_not_advance(self):
        s=signal(); prepare(s,snap()['book'],NOW,settings())
        t=paper_trade(s,NOW); ts=t['last_checked_ms']+60000
        for bars in ([bar(ts+60000,100,101,99,100)],[bar(ts,100,float('nan'),99,100)]):
            with self.assertRaises(ValueError):advance_trade(t,bars)

    def test_funding_settles_separately_and_no_entry_never_counts_as_win(self):
        s=signal(); prepare(s,snap()['book'],NOW,settings())
        t=paper_trade(s,NOW)
        t.update(status='TIMEOUT',filled_at_ms=NOW+60000,finished_at_ms=NOW+600000)
        t['journal_json'].update(outcome_bounds(t,s.entry))
        before=t['journal_json']['net_r_low']
        settle_funding(t,[dict(fundingTime=NOW+180000,fundingRate='.0001',markPrice='100')])
        self.assertLess(t['journal_json']['net_r_low'],before)
        self.assertTrue(t['journal_json']['costs_complete'])
        noentry=dict(t,status='NO_ENTRY',filled_at_ms=None,journal_json={'costs_complete':True})
        active=dict(t,status='OPEN',journal_json={})
        report=summarize_window([t,noentry,active],[],NOW-1,NOW+1)
        self.assertEqual(report['total']['ALL']['confirmed_net_wins'],0)
        self.assertEqual(report['total']['ALL']['resolved'],1)
        self.assertEqual(report['active'],1)
        self.assertIn('Sanal',format_digest('test',report))

    def test_late_best_candidate_wins_global_ranking(self):
        cfg=settings(symbols=('A','Z'),max_signals_per_run=1,standard_exit_shadows=False)
        data=snap()
        class Client:
            def symbol_info(self):
                return {s:dict(contractType='PERPETUAL',filters=[dict(filterType='PRICE_FILTER',tickSize='0.01')]) for s in ('A','Z')}
            def scalp_snapshot(self,symbol,asof,deadline=None):return copy.deepcopy(data)
            def get(self,path,**kwargs):
                if path.endswith('/time'):return {'serverTime':NOW}
                return data['book'] if kwargs.get('symbol') else [dict(data['book'],symbol=s) for s in ('A','Z')]
        def assess_one(symbol_,*args):
            s=signal(symbol=symbol_); s.eligible=True
            return [s],[]
        def ranking(s,*args):
            s.quality_score=90 if s.symbol=='A' else 10
            s.journal.update(quality_score=s.quality_score,quality_components={'fixture':s.quality_score})
            return s.quality_score
        r=Runner(cfg,Client())
        try:
            with patch('scalp_sentinel.main.time.time',return_value=NOW/1000),patch('scalp_sentinel.main.assess_standard',side_effect=assess_one),patch('scalp_sentinel.main.rank',side_effect=ranking),patch('scalp_sentinel.main.as_completed',side_effect=lambda jobs,**kw:iter(reversed(list(jobs)))):
                self.assertEqual(r.tick(NOW),1)
                self.assertEqual(r.journal.memory_trades[0]['symbol'],'A')
                self.assertEqual(r.tick(NOW+60000),0)
                self.assertEqual(len(r.journal.memory_trades),1)
        finally:r.close()

    def test_daily_limit_reserves_active_risk_and_incomplete_losses(self):
        j=Journal()
        j.memory_trades=[dict(cohort='primary',finished_at_ms=NOW,journal_json={'costs_complete':False,'risk_pct':.35,'net_r_low':.5})]
        self.assertAlmostEqual(j.daily_loss_pct(NOW,'Asia/Shanghai'),.35)

    def test_rate_limit_cooldown_avoids_retry_storm(self):
        c=BinanceClient('https://example.invalid')
        response=Mock(status_code=429,headers={'Retry-After':'3600'})
        import requests
        response.raise_for_status.side_effect=requests.HTTPError(response=response)
        session=Mock(); session.get.return_value=response
        with patch.object(c,'_session',return_value=session):
            with self.assertRaises(BinanceError):c.get('/fapi/v1/time')
            with self.assertRaisesRegex(BinanceError,'cooldown'):c.get('/fapi/v1/time')
        self.assertEqual(session.get.call_count,1)

    def test_market_delay_outside_original_drift_limit_is_no_entry(self):
        s=signal(); prepare(s,snap()['book'],NOW,settings())
        t=paper_trade(s,NOW); t['journal_json']['entry_mode']='market_delayed'
        ts=t['last_checked_ms']+60000
        out=advance_trade(t,[bar(ts,100.50,100.60,100.40,100.55)])
        self.assertEqual(out['status'],'NO_ENTRY')
        self.assertIsNone(out['filled_at_ms'])

    def test_live_invalid_database_url_fails_closed(self):
        from scalp_sentinel.storage import Cooldowns
        with self.assertRaises(ValueError):Cooldowns('host=localhost dbname=test',False)
