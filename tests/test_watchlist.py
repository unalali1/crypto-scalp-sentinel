"""Default V2 watchlist regression tests (no credentials required)."""
import os
import unittest
from unittest.mock import patch

from scalp_sentinel.config import Config, DEFAULT_SYMBOLS


EXPECTED = (
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "AVAXUSDT", "LINKUSDT", "NEARUSDT", "AAVEUSDT", "ZECUSDT",
    "ENAUSDT", "HYPEUSDT", "SUIUSDT", "DOGEUSDT", "LTCUSDT",
    "WLDUSDT", "BIOUSDT",
)


class DefaultWatchlist(unittest.TestCase):
    def test_new_watchlist_has_17_unique_contracts(self):
        self.assertEqual(tuple(DEFAULT_SYMBOLS.split(",")), EXPECTED)
        self.assertEqual(len(set(EXPECTED)), 17)

    def test_environment_default_uses_all_17(self):
        with patch.dict(os.environ, {"SYMBOLS": ""}, clear=False):
            # An explicitly empty override means no configured symbols.
            self.assertEqual(Config.from_env().symbols, ())
        with patch.dict(os.environ, {}, clear=True):
            cfg = Config.from_env()
        self.assertEqual(cfg.symbols, EXPECTED)
        self.assertTrue(cfg.v2_enabled)

    def test_explicit_override_remains_supported_and_deduplicates(self):
        with patch.dict(os.environ, {"SYMBOLS":"HYPEUSDT,SUIUSDT,HYPEUSDT"}, clear=True):
            self.assertEqual(Config.from_env().symbols, ("HYPEUSDT", "SUIUSDT"))


if __name__ == "__main__":
    unittest.main()
