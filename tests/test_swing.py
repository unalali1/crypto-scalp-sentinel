import copy
import unittest
from unittest.mock import patch

from scalp_sentinel.binance_client import BinanceError
from scalp_sentinel.journal import Journal, paper_trade
from scalp_sentinel.swing_backtest import asof, fetch_bars
from scalp_sentinel.swing_engine import DAY, FOUR_HOURS, HOUR, daily_direction, evaluate_swing
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

    def test_fetch_history_rejects_gap_across_pages(self):
        class Client:
            def get(self,_path,**kwargs):
                start=kwargs["startTime"]
                if start==0:
                    return [bar(i*HOUR,100,101,99,100,1,HOUR) for i in range(1000)]
                return [bar(1001*HOUR,100,101,99,100,1,HOUR)]
        with self.assertRaisesRegex(ValueError,"page gap"):
            fetch_bars(Client(),"BTCUSDT","1h",HOUR,0,1002*HOUR)


if __name__=="__main__":
    unittest.main()
