"""Closed-bar, directional and delivery checks; no historical performance claims."""
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from scalp_sentinel.binance_client import BinanceClient, BinanceError, DataPending
from scalp_sentinel.execution import prepare
from scalp_sentinel.notifier import format_signal
from scalp_sentinel.standard import assess
from scalp_sentinel.main import Runner
from test_execution_research import config


def cfg():
    return SimpleNamespace(min_quote_volume_usdt=15_000_000,max_spread_bps=10,
        min_stop_atr=.6,max_stop_atr=3,max_entry_drift_atr=.3,fee_bps=5,
        slippage_bps=2,max_cost_r=.40,entry_window_minutes=5,max_hold_minutes=45,
        score_threshold=5,min_rvol=1.15,breakout_rvol=1.15,min_delta_ratio=.05,
        min_efficiency=.25,correlation_threshold=.8)


def snapshot(side="LONG", breakout=False):
    sign=1 if side=="LONG" else -1
    def bar(i, o, c, high=None, low=None, v=100, tf=300000):
        h=max(o,c)+.08 if high is None else high
        l=min(o,c)-.08 if low is None else low
        return [i*tf,str(o),str(h),str(l),str(c),str(v),i*tf+tf-1]
    five=[]
    for i in range(100):
        c=100+sign*(i-99)*.025
        five.append(bar(i,c-.005*sign,c))
    fifteen=[]
    for i in range(100):
        c=100+sign*(i-99)*.06
        fifteen.append(bar(i,c-.01*sign,c,tf=900000))
    if breakout:
        # Level in the preceding 12 bars; the penultimate bar breaks on volume.
        ref=100-sign*.12
        for i in range(86,98):
            five[i]=bar(i,ref,ref+sign*.01,
                        high=ref+.08,low=ref-.08)
        five[-2]=bar(98,100-sign*.1,100+sign*.18,v=200)
        five[-1]=bar(99,100-sign*.02,100+sign*.22,
                     high=100.30,low=99.86)
    else:
        five[-2]=bar(98,100-sign*.22,100-sign*.16,
                     high=100.32 if sign==-1 else 100.0,
                     low=99.68 if sign==1 else 100.0)
        five[-1]=bar(99,100-sign*.15,100+sign*.10,
                     high=100.20,low=99.76)
    return {"5m":five,"15m":fifteen,"ticker":{"quoteVolume":"50000000"},
            "book":{"bidPrice":"100.09","askPrice":"100.11"}}


class StandardTests(unittest.TestCase):
    def test_ema_pullback_both_directions(self):
        for side in ("LONG","SHORT"):
            with self.subTest(side=side):
                signals,_=assess("TEST",snapshot(side),"0.01",cfg())
                signal=next(x for x in signals if x.side==side and x.strategy_version=="ema-pullback")
                self.assertTrue(signal.eligible,signal.checks)
                self.assertEqual(signal.timeframe_ms,300000)
                self.assertTrue(signal.stop<signal.entry<signal.tp1<signal.tp2 if side=="LONG"
                                else signal.stop>signal.entry>signal.tp1>signal.tp2)

    def test_breakout_retest_both_directions(self):
        for side in ("LONG","SHORT"):
            with self.subTest(side=side):
                signals,_=assess("TEST",snapshot(side,True),"0.01",cfg())
                signal=next(x for x in signals if x.side==side and x.strategy_version=="breakout-retest")
                self.assertTrue(signal.eligible,signal.checks)

    def test_liquidity_spread_and_stale_quote_fail_closed(self):
        snap=snapshot()
        snap['ticker']['quoteVolume']='1'
        snap['book']={'bidPrice':'100','askPrice':'101'}
        signals,reasons=assess('TEST',snap,'0.01',cfg())
        self.assertEqual(signals,[])
        self.assertEqual(set(reasons),{'quote_volume','spread'})
        signal=next(x for x in assess('TEST',snapshot(),'0.01',cfg())[0]
                    if x.side=='LONG' and x.strategy_version=='ema-pullback')
        now=signal.snapshot_ts+300000+5000
        self.assertEqual(prepare(signal,{'bidPrice':'100.09','askPrice':'100.11','time':now-16000},now,cfg()),'stale_quote')
        self.assertIsNone(prepare(signal,{'bidPrice':'100.09','askPrice':'100.11','time':now},now,cfg()))
        text=format_signal(signal,'Asia/Shanghai')
        self.assertIn('Strateji: ema-pullback',text)
        self.assertIn('son kapanmış 5dk',text)
        self.assertNotIn('OI 1 saat',text)

    def test_snapshot_rejects_unclosed_and_missing_latest_bar(self):
        class Client(BinanceClient):
            def __init__(self):super().__init__('unused'); self.calls=[]
            def get(self,path,**kwargs):
                self.calls.append((path,kwargs))
                if path.endswith('klines'):
                    duration={'5m':300000,'15m':900000}[kwargs['interval']]
                    end=1800000000000//duration*duration
                    return [[end-(100-i)*duration,'100','101','99','100','10',
                             end-(99-i)*duration-1] for i in range(100)]
                if path.endswith('bookTicker'):return {'bidPrice':'100','askPrice':'100.01'}
                return {'quoteVolume':'50000000'}
        client=Client()
        now=1800000003000
        result=client.scalp_snapshot('TEST',now)
        self.assertTrue(all(int(x[6])<now for x in result['5m']))
        self.assertFalse(any('openInterest' in path for path,_ in client.calls))
        with self.assertRaises(DataPending):client.scalp_snapshot('TEST',now+60000)

    def test_worker_runs_once_per_closed_bar_without_flow_data(self):
        now=1800000003000
        snap=snapshot()
        for tf,duration in (('5m',300000),('15m',900000)):
            shift=(now//duration*duration-duration)-int(snap[tf][-1][0])
            for bar in snap[tf]:
                bar[0]+=shift
                bar[6]+=shift
        class Client:
            calls=0
            def symbol_info(self):
                return {'TEST':{'contractType':'PERPETUAL','filters':[
                    {'filterType':'PRICE_FILTER','tickSize':'0.01'}]}}
            def scalp_snapshot(self,symbol,asof):
                self.calls+=1
                return snap
            def get(self,path,**kwargs):
                if path.endswith('/time'):return {'serverTime':now}
                return {'bidPrice':'100.09','askPrice':'100.11','time':now}
        client=Client()
        settings=replace(config(),primary_strategy='standard',symbols=('TEST',),
                         shadow_enabled=False,fee_bps=0,slippage_bps=0)
        runner=Runner(settings,client)
        try:
            with patch('scalp_sentinel.main.time.time',return_value=now/1000):
                self.assertEqual(runner.tick(now),1)
                self.assertEqual(runner.tick(now+60000),0)
            self.assertEqual(client.calls,1)
            self.assertEqual(len(runner.journal.memory_trades),1)
        finally:
            runner.close()


if __name__=='__main__':unittest.main()
