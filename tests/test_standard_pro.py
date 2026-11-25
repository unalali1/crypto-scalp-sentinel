"""Tests for improved shadow setups, correlation clustering, and primary pause."""
import copy
import unittest
from unittest.mock import patch

from scalp_sentinel.binance_client import BinanceClient
from scalp_sentinel.execution import prepare
from scalp_sentinel.forward_report import format_digest
from scalp_sentinel.main import Runner
from scalp_sentinel.standard_shadow_pro import PRO_COHORT, assess_pro, select_pro_candidates
from test_execution_research import config

NOW = 1800000003000


def _bar(ts, o, h, l, c, v=100.0, buy_vol=None, duration=300000):
    if buy_vol is None:
        buy_vol = v * 0.5
    return [ts, str(o), str(h), str(l), str(c), str(v), ts + duration - 1, "0", 10, str(buy_vol), "0", "0"]


def _aligned_ts(count, duration):
    end = NOW // duration * duration
    return [end - (count - i) * duration for i in range(count)]


def _base_snapshot(side="LONG", setup="sfp"):
    sign = 1 if side == "LONG" else -1
    ts5 = _aligned_ts(100, 300000)
    ts15 = _aligned_ts(100, 900000)
    ts1h = _aligned_ts(100, 3600000)

    # Use realistic ~0.6% 5m ATR on price ~100 so 13bp costs remain <= 0.20R.
    five = []
    for i, t in enumerate(ts5):
        if setup == "vcp":
            c = 100.0 + sign * (i - 86) * 0.04 if i <= 86 else 100.0
            five.append(_bar(t, c - 0.10 * sign, c + 0.35, c - 0.35, c, v=100.0,
                             buy_vol=54.0 if sign == 1 else 46.0))
        else:
            c = 100.0 + (0.12 if i % 2 == 0 else -0.12)
            five.append(_bar(t, c, c + 0.30, c - 0.30, c, v=100.0, buy_vol=50.0))

    fifteen = []
    for i, t in enumerate(ts15):
        if setup == "vcp":
            c = 100.0 + sign * (i - 99) * 0.18
            fifteen.append(_bar(t, c - 0.08 * sign, c + 0.25, c - 0.25, c, v=300.0, duration=900000))
        else:
            c = 100.0 + sign * (i - 99) * 0.03
            fifteen.append(_bar(t, c - 0.01 * sign, c + 0.15, c - 0.15, c, v=300.0, duration=900000))

    hourly = []
    for i, t in enumerate(ts1h):
        c = 100.0 + sign * (i - 99) * 0.25
        hourly.append(_bar(t, c - 0.10 * sign, c + 0.25, c - 0.25, c, v=1000.0, duration=3600000))

    if setup == "sfp":
        for idx in range(78, 98):
            five[idx] = _bar(ts5[idx], 100.0, 100.35, 99.65, 100.0, v=100.0, buy_vol=50.0)
        if sign == 1:
            five[-2] = _bar(ts5[-2], 99.90, 100.00, 99.20, 99.60, v=180.0, buy_vol=95.0)
            five[-1] = _bar(ts5[-1], 99.62, 100.12, 99.45, 100.05, v=160.0, buy_vol=110.0)
        else:
            five[-2] = _bar(ts5[-2], 100.10, 100.80, 100.00, 100.40, v=180.0, buy_vol=85.0)
            five[-1] = _bar(ts5[-1], 100.38, 100.55, 99.88, 99.95, v=160.0, buy_vol=50.0)
    else:
        # Tight consolidation in -13:-1 (median range <= 0.90*atr5), then expansion breakout.
        ref = 100.0
        for idx in range(87, 99):
            five[idx] = _bar(ts5[idx], ref, ref + 0.18, ref - 0.18, ref + 0.02 * sign, v=90.0,
                             buy_vol=50.0 if sign == 1 else 40.0)
        if sign == 1:
            five[-3] = _bar(ts5[-3], ref, ref + 0.15, ref - 0.25, ref, v=90.0, buy_vol=48.0)
            five[-1] = _bar(ts5[-1], ref + 0.02, ref + 0.72, ref, ref + 0.65, v=220.0, buy_vol=165.0)
        else:
            five[-3] = _bar(ts5[-3], ref, ref + 0.25, ref - 0.15, ref, v=90.0, buy_vol=42.0)
            five[-1] = _bar(ts5[-1], ref - 0.02, ref, ref - 0.72, ref - 0.65, v=220.0, buy_vol=55.0)

    close = float(five[-1][4])
    taker = [
        {"timestamp": int(r[0]), "buyVol": float(r[9]), "sellVol": float(r[5]) - float(r[9])}
        for r in five[-40:]
    ]
    return {
        "5m": five,
        "15m": fifteen,
        "1h": hourly,
        "taker": taker,
        "ticker": {"quoteVolume": "60000000"},
        "book": {"bidPrice": str(close - 0.01), "askPrice": str(close + 0.01), "bidQty": "1000", "askQty": "1000", "time": NOW},
        "asof_ms": NOW,
    }


def _pro_cfg(**kwargs):
    return config(
        primary_strategy="standard",
        standard_enhanced=True,
        standard_pro_shadow=True,
        primary_notifications_paused=True,
        max_entry_drift_atr=0.30,
        **kwargs,
    )


class StandardProShadowTests(unittest.TestCase):
    def test_sfp_reclaim_eligible_and_uses_pro_exit_geometry(self):
        cfg = _pro_cfg()
        for side in ("LONG", "SHORT"):
            with self.subTest(side=side):
                snap = _base_snapshot(side, setup="sfp")
                btc = _base_snapshot(side, setup="vcp")
                signals, prefilter = assess_pro("ETHUSDT", snap, "0.01", btc, cfg)
                self.assertEqual(prefilter, [])
                sig = next(s for s in signals if s.side == side and s.strategy_version == "pro-sfp-reclaim")
                self.assertTrue(sig.eligible, sig.checks)
                self.assertIsNone(prepare(sig, snap["book"], NOW, cfg))
                self.assertEqual(sig.journal["tp1_r"], 1.1)
                self.assertEqual(sig.journal["tp2_r"], 2.0)
                self.assertEqual(sig.journal["max_hold_ms"], 90 * 60000)
                self.assertLessEqual(sig.journal["estimated_cost_r"], 0.20)

    def test_vcp_trend_eligible_and_enters_at_top_of_book(self):
        cfg = _pro_cfg()
        for side in ("LONG", "SHORT"):
            with self.subTest(side=side):
                snap = _base_snapshot(side, setup="vcp")
                btc = _base_snapshot(side, setup="vcp")
                signals, _ = assess_pro("SOLUSDT", snap, "0.01", btc, cfg)
                sig = next(s for s in signals if s.side == side and s.strategy_version == "pro-vcp-trend")
                self.assertTrue(sig.eligible, sig.checks)
                self.assertIsNone(prepare(sig, snap["book"], NOW, cfg))
                expected_entry = float(snap["book"]["bidPrice"]) if side == "LONG" else float(snap["book"]["askPrice"])
                self.assertAlmostEqual(sig.entry, expected_entry)

    def test_btc_opposing_shock_and_counter_flow_block_signals(self):
        cfg = _pro_cfg()
        snap = _base_snapshot("LONG", setup="sfp")
        btc_bear = _base_snapshot("SHORT", setup="vcp")
        signals, _ = assess_pro("ETHUSDT", snap, "0.01", btc_bear, cfg)
        sig = next(s for s in signals if s.side == "LONG" and s.strategy_version == "pro-sfp-reclaim")
        self.assertFalse(sig.checks["btc alignment"])
        self.assertFalse(sig.eligible)

        # Opposing taker flow blocks even with aligned BTC.
        btc_bull = _base_snapshot("LONG", setup="vcp")
        snap["taker"][-1]["buyVol"] = 10.0
        snap["taker"][-1]["sellVol"] = 150.0
        signals, _ = assess_pro("ETHUSDT", snap, "0.01", btc_bull, cfg)
        sig = next(s for s in signals if s.side == "LONG" and s.strategy_version == "pro-sfp-reclaim")
        self.assertFalse(sig.checks["taker flow"])
        self.assertFalse(sig.eligible)

        # Shock regime blocks via prefilter.
        shock = copy.deepcopy(snap)
        shock["5m"][-1][2] = "110.0"
        shock["5m"][-1][3] = "90.0"
        signals, prefilter = assess_pro("ETHUSDT", shock, "0.01", btc_bull, cfg)
        self.assertEqual(signals, [])
        self.assertEqual(prefilter, ["pro_shock_regime"])

    def test_structural_obstacle_and_high_cost_reject_trade(self):
        cfg = _pro_cfg()
        snap = _base_snapshot("LONG", setup="sfp")
        # Inject a confirmed 15m swing high just ahead of entry (< 1.15R).
        for idx in range(70, 75):
            snap["15m"][idx][2] = "100.30"
        snap["15m"][72][2] = "100.55"
        btc = _base_snapshot("LONG", setup="vcp")
        signals, _ = assess_pro("ETHUSDT", snap, "0.01", btc, cfg)
        sig = next(s for s in signals if s.side == "LONG" and s.strategy_version == "pro-sfp-reclaim")
        self.assertFalse(sig.checks["structure room"])
        self.assertFalse(sig.eligible)

        # Excessive cost-to-risk is rejected at execution gate.
        clean = _base_snapshot("LONG", setup="sfp")
        sig = next(s for s in assess_pro("ETHUSDT", clean, "0.01", btc, cfg)[0]
                   if s.side == "LONG" and s.strategy_version == "pro-sfp-reclaim")
        expensive_cfg = _pro_cfg(fee_bps=50.0, slippage_bps=20.0)
        self.assertEqual(prepare(sig, clean["book"], NOW, expensive_cfg), "cost_to_risk")

    def test_correlation_cluster_blocks_correlated_second_symbol(self):
        cfg = _pro_cfg()
        eth = _base_snapshot("LONG", setup="sfp")
        sol = _base_snapshot("LONG", setup="sfp")
        btc = _base_snapshot("LONG", setup="vcp")
        eth_sig = next(s for s in assess_pro("ETHUSDT", eth, "0.01", btc, cfg)[0]
                       if s.side == "LONG" and s.strategy_version == "pro-sfp-reclaim")
        sol_sig = next(s for s in assess_pro("SOLUSDT", sol, "0.01", btc, cfg)[0]
                       if s.side == "LONG" and s.strategy_version == "pro-sfp-reclaim")
        self.assertIsNone(prepare(eth_sig, eth["book"], NOW, cfg))
        self.assertIsNone(prepare(sol_sig, sol["book"], NOW, cfg))
        eth_sig.journal["quality_score"] = 85.0
        sol_sig.journal["quality_score"] = 70.0
        decisions = list(select_pro_candidates([sol_sig, eth_sig], [], {"ETHUSDT": eth, "SOLUSDT": sol}, 0.75))
        selected = [sig.symbol for sig, reason in decisions if reason is None]
        blocked = [(sig.symbol, reason) for sig, reason in decisions if reason is not None]
        self.assertEqual(selected, ["ETHUSDT"])
        self.assertEqual(blocked, [("SOLUSDT", "pro_correlated_cluster")])

    def test_paused_primary_still_records_pro_shadow_without_telegram(self):
        eth = _base_snapshot("LONG", setup="sfp")
        btc = _base_snapshot("LONG", setup="vcp")

        class Client:
            def symbol_info(self):
                return {
                    sym: {"contractType": "PERPETUAL", "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.01"}]}
                    for sym in ("ETHUSDT", "SOLUSDT", "BTCUSDT")
                }

            def scalp_snapshot(self, symbol, asof, deadline=None):
                return copy.deepcopy(btc if symbol == "BTCUSDT" else eth)

            def get(self, path, **kwargs):
                if path.endswith("/time"):
                    return {"serverTime": NOW}
                if path.endswith("/ticker/bookTicker"):
                    return [
                        dict(eth["book"], symbol="ETHUSDT"),
                        dict(eth["book"], symbol="SOLUSDT"),
                        dict(btc["book"], symbol="BTCUSDT"),
                    ]
                return {}

            def context_candles(self, symbol, tf, asof):
                return [[0, "100", "101", "99", "100", "10", 1]] * 60

        cfg = _pro_cfg(symbols=("ETHUSDT", "SOLUSDT", "BTCUSDT"), dry_run=False,
                       telegram_token="tok", telegram_chat_id="chat", database_url="")
        with patch("scalp_sentinel.main.Cooldowns") as storage_cls:
            from scalp_sentinel.storage import Cooldowns
            storage_cls.side_effect = lambda *_a: Cooldowns("", True)
            r = Runner(cfg, Client())
        try:
            with patch("scalp_sentinel.main.time.time", return_value=NOW / 1000), \
                 patch("scalp_sentinel.main.send_telegram") as send, \
                 patch("scalp_sentinel.main.track_signals"), \
                 patch("scalp_sentinel.main.detect_confluences", return_value=[]):
                sent = r.tick(NOW)
            self.assertEqual(sent, 0)
            send.assert_not_called()
            pro_trades = [t for t in r.journal.memory_trades if t.get("cohort") == PRO_COHORT]
            # ETHUSDT and SOLUSDT share identical 5m returns, so cluster cap records only one of them plus BTCUSDT.
            self.assertEqual({t["symbol"] for t in pro_trades}, {"ETHUSDT", "BTCUSDT"})
            by_sym = {t["symbol"]: t for t in pro_trades}
            self.assertEqual(by_sym["ETHUSDT"]["journal_json"]["pro_setup"], "pro-sfp-reclaim")
            self.assertEqual(by_sym["BTCUSDT"]["journal_json"]["pro_setup"], "pro-vcp-trend")
        finally:
            r.close()

    def test_scalp_snapshot_extracts_taker_from_full_klines_and_digest_formats_pro(self):
        class Client(BinanceClient):
            def __init__(self):
                super().__init__("unused")

            def get(self, path, **kwargs):
                if path.endswith("klines"):
                    tf = kwargs["interval"]
                    dur = 300000 if tf == "5m" else 900000
                    end = NOW // dur * dur
                    return [_bar(end - (100 - i) * dur, 100, 101, 99, 100, v=50.0, buy_vol=30.0, duration=dur)
                            for i in range(100)]
                if path.endswith("bookTicker"):
                    return {"bidPrice": "100.0", "askPrice": "100.01", "bidQty": "10", "askQty": "10", "time": NOW}
                return {"quoteVolume": "50000000"}

        snap = Client().scalp_snapshot("BTCUSDT", NOW)
        self.assertEqual(len(snap["taker"]), 40)
        self.assertEqual(snap["taker"][-1]["buyVol"], 30.0)
        self.assertEqual(snap["taker"][-1]["sellVol"], 20.0)

        digest = format_digest("2026-11-24", {
            "scans": 288, "symbol_evaluations": 4896, "eligible": 0, "sent": 0, "filled": 0,
            "outcomes": {}, "active": 0, "ambiguous": 0, "blocked": {}, "by_strategy": {},
            "total": {"ALL": {}},
            "paired_shadows": [{
                "version": "standard-pro-v1", "signals": 8, "resolved": 6,
                "filled": 7, "mean_net_r_low": 0.215, "profit_factor_low": 1.64,
            }],
        })
        self.assertIn("Yeni shadow (standard-pro-v1): 8 fikir, 6 sonuç", digest)

    def test_daily_vwap_and_mitigated_liquidity_rules(self):
        cfg = _pro_cfg()
        snap = _base_snapshot("LONG", setup="vcp")
        btc = _base_snapshot("LONG", setup="vcp")
        # Huge volume spike on an earlier 15m bar above current price pulls Daily VWAP above entry.
        day_bucket = int(snap["15m"][-1][0]) // 86400000
        for row in snap["15m"]:
            if int(row[0]) // 86400000 == day_bucket and int(row[0]) < int(snap["15m"][-1][0]):
                row[2] = "108.0"
                row[3] = "107.0"
                row[4] = "107.5"
                row[5] = "500000"
                break
        signals, _ = assess_pro("SOLUSDT", snap, "0.01", btc, cfg)
        sig = next(s for s in signals if s.side == "LONG" and s.strategy_version == "pro-vcp-trend")
        self.assertFalse(sig.checks["daily vwap"])
        self.assertFalse(sig.eligible)

        # A 15m pivot high that was already pierced later is mitigated and does not block room.
        mit = _base_snapshot("LONG", setup="sfp")
        for idx in range(70, 75):
            mit["15m"][idx][2] = "100.30"
        mit["15m"][72][2] = "100.55"
        mit["15m"][76][2] = "101.50"  # mitigates the pivot at 72 while staying >1.15R away
        signals, _ = assess_pro("ETHUSDT", mit, "0.01", btc, cfg)
        sfp_sig = next(s for s in signals if s.side == "LONG" and s.strategy_version == "pro-sfp-reclaim")
        self.assertTrue(sfp_sig.checks["structure room"])

    def test_confluence_supports_extended_oscillators_on_4h_and_1d(self):
        from scalp_sentinel.confluence import detect_confluences
        # Deterministic 80-bar 4h series should evaluate cleanly with Stoch/CCI/CMF included.
        ts4 = _aligned_ts(80, 14400000)
        rows = [_bar(t, 100 + i * 0.1, 101 + i * 0.1, 99 + i * 0.1, 100 + i * 0.1, v=500.0, duration=14400000)
                for i, t in enumerate(ts4)]
        result = detect_confluences("BTCUSDT", {"4h": rows}, "0.01", timeframes=("4h",))
        self.assertIsInstance(result, list)


if __name__ == "__main__":
    unittest.main()
