import copy
import unittest
from unittest.mock import patch

from scalp_sentinel.binance_client import BinanceError
from scalp_sentinel.journal import Journal, paper_trade
from scalp_sentinel.swing_backtest import aggregate, asof, console_report, fetch_bars, replay
from scalp_sentinel.swing_engine import DAY, FOUR_HOURS, HOUR, SwingSetup, daily_direction, evaluate_swing
from scalp_sentinel.swing_execution import MAX_HOLD_MS, prepare_swing
from scalp_sentinel.swing_main import SwingRunner
from scalp_sentinel.swing_market import validate_bars
from scalp_sentinel.tracker import advance_trade


def bar(ts,open_,high,low,close,volume,period):
    return [ts,str(open_),str(high),str(low),str(close),str(volume),ts+period-1,
            str(volume*close),0,str(volume/2),str(volume*close/2),0]


def bullish_snapshot():
    t=600*DAY+4*HOUR
    days=[]
    for i in range(450):
        c=100+i*.067
        days.append(bar((t//DAY-450+i)*DAY,c-.1,c+.7,c-.7,c,1000,DAY))
    four=[]
    for i in range(120):
        c=100+i*.25
        o=c-.4
        l=c-1
        h=c+.7
        v=1000
        if i in (116,117,118):
            c,o,l,h=126.5,127,126,127.5
        if i==119:
            c,o,l,h,v=130.5,127,126.5,131,2000
        four.append(bar(t-(120-i)*FOUR_HOURS,o,h,l,c,v,FOUR_HOURS))
    hours=[]
    for i in range(40):
        c=126+i*.05
        o=c-.2
        l=c-.5
        h=c+.5
        if i==39:
            c,o,l,h=130.5,129,128.5,131
        hours.append(bar(t-(40-i)*HOUR,o,h,l,c,100,HOUR))
    return t,{"1d":days,"4h":four,"1h":hours,
              "book":{"bidPrice":130.47,"askPrice":130.53,"time":t+5*60000},
              "quote_volume":50_000_000}


class Swing(unittest.TestCase):
    def test_independent_closed_bar_candidate_and_btc_gate(self):
        t,snap=bullish_snapshot()
        found=evaluate_swing("ETHUSDT",snap,snap["1d"],"0.01")
        self.assertEqual(len(found),2)
        self.assertTrue(found[0].eligible,found[0].checks)
        self.assertFalse(found[1].eligible)
        neutral=[bar(i*DAY,100,101,99,100,1000,DAY) for i in range(450)]
        self.assertIsNone(daily_direction(neutral))
        self.assertFalse(evaluate_swing("ETHUSDT",snap,neutral,"0.01")[0].eligible)
        future=copy.deepcopy(snap)
        future["1h"].append(bar(t,130,132,129,131,100,HOUR))
        self.assertEqual(evaluate_swing("ETHUSDT",future,snap["1d"],"0.01"),[])

    def test_swing_paper_horizon_and_stale_quote(self):
        t,snap=bullish_snapshot()
        now=t+5*60000+3000
        setup=evaluate_swing("ETHUSDT",snap,snap["1d"],"0.01")[0]
        self.assertEqual(prepare_swing(setup,dict(snap["book"],time=t-20_000),now),"stale_quote")
        self.assertIsNone(prepare_swing(setup,snap["book"],now))
        self.assertEqual(setup.journal["max_hold_ms"],5*DAY)
        self.assertEqual(setup.journal["entry_window_ms"],2*HOUR)
        trade=paper_trade(setup,now)
        first=bar(t+6*60000,setup.entry,setup.entry+.1,setup.entry-.1,setup.entry,100,60000)
        self.assertEqual(advance_trade(trade,[first])["status"],"OPEN")

    def test_live_data_fails_closed_on_gap_and_unclosed_bar(self):
        rows=[bar(i*HOUR,100,101,99,100,100,HOUR) for i in range(25)]
        with self.assertRaises(BinanceError):
            validate_bars(rows[:10]+rows[11:],HOUR,25*HOUR+5*60000,20)
        self.assertEqual(asof(rows,25*HOUR,HOUR)[-1][0],24*HOUR)
        self.assertEqual(asof(rows,24*HOUR+HOUR//2,HOUR)[-1][0],23*HOUR)

    def test_worker_limits_paper_exposure_and_never_sends_trade_alert(self):
        t,snap=bullish_snapshot()
        now=t+5*60000
        class Client:
            def get(self,path,**_kw):
                return {"serverTime":now} if path.endswith("/time") else dict(snap["book"])

            def symbol_info(self):
                return {s:{"contractType":"PERPETUAL","filters":[{"filterType":"PRICE_FILTER","tickSize":"0.01"}]}
                        for s in ("BTCUSDT","ETHUSDT")}

        journal=Journal()
        runner=SwingRunner(Client(),journal,("ETHUSDT",))
        with patch("scalp_sentinel.swing_main.live_snapshot",return_value=snap),\
             patch("scalp_sentinel.swing_main.time.time",return_value=now/1000):
            self.assertEqual(runner.run(now),1)
            self.assertEqual(runner.run(now),0)
        self.assertEqual(len(journal.memory_trades),1)
        self.assertEqual(journal.memory_trades[0]["cohort"],"swing-shadow")
        self.assertEqual(journal.memory_trades[0]["strategy_version"],"swing-trend-v1")

    def test_unselected_candidate_is_independent_and_idempotent(self):
        t,snap=bullish_snapshot()
        now=t+5*60000
        class Client:
            def get(self,path,**_kw):
                return {"serverTime":now} if path.endswith("/time") else dict(snap["book"])

            def symbol_info(self):
                return {s:{"contractType":"PERPETUAL","filters":[{"filterType":"PRICE_FILTER","tickSize":"0.01"}]}
                        for s in ("BTCUSDT","ETHUSDT","SOLUSDT")}

        journal=Journal()
        runner=SwingRunner(Client(),journal,("ETHUSDT","SOLUSDT"))
        with patch("scalp_sentinel.swing_main.live_snapshot",return_value=snap),\
             patch("scalp_sentinel.swing_main.time.time",return_value=now/1000):
            self.assertEqual(runner.run(now),1)
            self.assertEqual(runner.run(now),0)
        self.assertEqual([(p["cohort"],p["symbol"]) for p in journal.memory_trades],
                         [("swing-shadow","ETHUSDT"),("swing-candidate-shadow","SOLUSDT")])
        self.assertEqual(journal.memory_trades[1]["journal_json"]["selection_reason"],"daily_cap")
        self.assertEqual(len(journal.active("swing-shadow")),1)
        self.assertEqual(journal.swing_recent_count(now),1)

    def test_fetch_history_rejects_gap_across_pages(self):
        class Client:
            def get(self,_path,**kwargs):
                start=kwargs["startTime"]
                if start==0:
                    return [bar(i*HOUR,100,101,99,100,1,HOUR) for i in range(1000)]
                return [bar(1001*HOUR,100,101,99,100,1,HOUR)]
        with self.assertRaisesRegex(ValueError,"page gap"):
            fetch_bars(Client(),"BTCUSDT","1h",HOUR,0,1002*HOUR)

    def test_console_report_preserves_holdout_aggregate(self):
        base={key:None for key in ("strategy","start_ms","end_ms","fetched_symbols",
              "skipped_symbols","development_end_ms","holdout_start_ms",
              "statuses","data_gaps","rejected","coverage","ranking",
              "delay_minutes","spread_bps","fee_bps","slippage_bps",
              "counterfactual_count","assumptions")}
        base["development"]=[{"symbol":"BTCUSDT","side":"LONG","signals":1},
                             {"symbol":"ALL","side":"ALL","signals":2}]
        base["holdout"]=[{"symbol":"ALL","side":"ALL","signals":3},
                         {"symbol":"ETHUSDT","side":"SHORT","signals":2}]
        report=console_report(base)
        self.assertEqual(report["development"]["signals"],2)
        self.assertEqual(report["holdout"]["signals"],3)
        self.assertEqual(len(report),len(base))

    def test_replay_keeps_rejected_candidate_outside_selected_portfolio(self):
        t=500*DAY
        info={sym:{"contractType":"PERPETUAL","filters":[{"filterType":"PRICE_FILTER","tickSize":"0.01"}]}
              for sym in ("BTCUSDT","ETHUSDT","SOLUSDT")}
        history={}
        for sym in info:
            rows={"1d":[bar(t-DAY,100,101,99,100,100,DAY)],
                  "4h":[bar(t-FOUR_HOURS,100,101,99,100,100,FOUR_HOURS)],
                  "1h":[bar(t-HOUR,100,101,99,100,100,HOUR)]}
            rows["opens"]={key:[int(row[0]) for row in rows[key]] for key in ("1d","4h","1h")}
            history[sym]=rows

        class Client:
            def __init__(self):
                self.quote_starts=[]

            def get(self,_path,**kwargs):
                self.quote_starts.append(kwargs["startTime"])
                return [bar(kwargs["startTime"],100,101,99,100,100,60000)]

        def setups(sym,*_args):
            if sym=="BTCUSDT":
                return []
            return [SwingSetup(sym,"LONG",t-FOUR_HOURS,f"{sym}:{t}",100,98,103,106,
                               "0.01",2,5,50_000_000,1.2 if sym=="ETHUSDT" else 2.0,{},True,
                               2 if sym=="ETHUSDT" else 1)]

        def outcome(_client,setup,decision,_end):
            trade=paper_trade(setup,decision)
            trade.update(status="NO_ENTRY",finished_at_ms=decision+60000)
            return trade,False

        client=Client()
        with patch("scalp_sentinel.swing_backtest.evaluate_swing",side_effect=setups),\
             patch("scalp_sentinel.swing_backtest.prepare_swing",return_value=None),\
             patch("scalp_sentinel.swing_backtest.paper_outcome",side_effect=outcome):
            result=replay(client,("ETHUSDT","SOLUSDT"),t,t+FOUR_HOURS,
                          delay_minutes=10,counterfactual=True,history=(info,history,{}))
            rvol=replay(Client(),("ETHUSDT","SOLUSDT"),t,t+FOUR_HOURS,
                        ranking="rvol",history=(info,history,{}))
        self.assertEqual([r["symbol"] for r in result["trade_ledger"]],["ETHUSDT"])
        self.assertEqual([r["symbol"] for r in result["counterfactual_ledger"]],["SOLUSDT"])
        self.assertEqual([r["reason"] for r in result["candidate_decisions"]],
                         ["paper_selected","daily_cap"])
        self.assertEqual(client.quote_starts,[t+10*60000,t+10*60000])
        self.assertEqual([r["symbol"] for r in rvol["trade_ledger"]],["SOLUSDT"])
        sample=paper_trade(setups("ETHUSDT")[0],t)
        sample["status"]="NO_ENTRY"
        self.assertEqual({r["symbol"] for r in aggregate([sample])},{"ALL","ETHUSDT"})


if __name__=="__main__":
    unittest.main()
