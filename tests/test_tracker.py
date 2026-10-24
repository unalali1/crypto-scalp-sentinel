"""Deterministic outcome checks; no network, DB or Telegram required."""
import unittest

from scalp_sentinel.tracker import MINUTE, advance_trade


def trade(side="SHORT"):
    if side == "SHORT":
        entry, stop, tp1, tp2 = 726.28, 730.62, 719.77, 715.42
    else:
        entry, stop, tp1, tp2 = 726.28, 721.94, 732.79, 737.13
    return dict(
        id=1, symbol="BNBUSDT", side=side, sent_at_ms=1000,
        entry_price=entry, stop_price=stop, tp1_price=tp1, tp2_price=tp2,
        status="AWAITING_ENTRY", filled_at_ms=None, tp1_at_ms=None,
        tp2_at_ms=None, stop_at_ms=None, last_checked_ms=0,
        finished_at_ms=None,
    )


def bar(minute, high, low):
    return [minute * MINUTE, "0", str(high), str(low), "0"]


class TradeOutcomeTests(unittest.TestCase):
    def test_short_entry_then_tp1_then_tp2(self):
        updated = advance_trade(trade(), [
            bar(1, 727.00, 725.00),
            bar(2, 726.20, 719.00),
            bar(3, 719.00, 715.00),
        ])
        self.assertEqual(updated["status"], "TP2_HIT")
        self.assertEqual(updated["filled_at_ms"], MINUTE)
        self.assertEqual(updated["tp1_at_ms"], 2 * MINUTE)
        self.assertEqual(updated["tp2_at_ms"], 3 * MINUTE)

    def test_stop_before_tp_is_failure(self):
        updated = advance_trade(trade(), [
            bar(1, 727, 725),
            bar(2, 731, 727),
        ])
        self.assertEqual(updated["status"], "STOPPED")
        self.assertIsNone(updated["tp1_at_ms"])

    def test_target_without_fill_not_success(self):
        updated = advance_trade(trade(), [
            bar(1, 719, 716),
            bar(2, 718, 715),
        ])
        self.assertEqual(updated["status"], "AWAITING_ENTRY")
        self.assertIsNone(updated["filled_at_ms"])
        self.assertIsNone(updated["tp1_at_ms"])

    def test_tp_and_stop_same_bar_not_a_win(self):
        updated = advance_trade(trade(), [
            bar(1, 727, 725),
            bar(2, 731, 719),
        ])
        self.assertEqual(updated["status"], "AMBIGUOUS_EXIT")
        self.assertIsNone(updated["tp1_at_ms"])

    def test_entry_and_target_same_bar_ambiguous(self):
        updated = advance_trade(trade(), [bar(1, 727, 719)])
        self.assertEqual(updated["status"], "AMBIGUOUS_ENTRY")
        self.assertIsNone(updated["tp1_at_ms"])

    def test_partial_success_if_stop_after_tp1(self):
        updated = advance_trade(trade(), [
            bar(1, 727, 725),
            bar(2, 726, 719),
            bar(3, 731, 720),
        ])
        self.assertEqual(updated["status"], "TP1_THEN_STOP")
        self.assertIsNotNone(updated["tp1_at_ms"])
        self.assertIsNone(updated["tp2_at_ms"])

    def test_long_trade(self):
        updated = advance_trade(trade("LONG"), [
            bar(1, 727, 725),
            bar(2, 738, 732),
        ])
        self.assertEqual(updated["status"], "TP2_HIT")
        self.assertIsNotNone(updated["tp1_at_ms"])

    def test_no_entry_expires(self):
        bars = [bar(i, 725, 723) for i in range(1, 62)]
        updated = advance_trade(trade(), bars)
        self.assertEqual(updated["status"], "NO_ENTRY")
        self.assertIsNone(updated["filled_at_ms"])

    def test_gapped_history_cannot_be_skipped(self):
        with self.assertRaises(ValueError):
            advance_trade(trade(), [bar(2, 727, 725)])

    def test_open_trade_times_out(self):
        t = trade()
        t.update(status="OPEN", filled_at_ms=MINUTE, last_checked_ms=24 * 60 * MINUTE)
        out = advance_trade(t, [bar(24 * 60 + 1, 727, 726)])
        self.assertEqual(out["status"], "TIMEOUT")


if __name__ == "__main__":
    unittest.main()
