import json
import unittest
from datetime import datetime
from pathlib import Path

from cipherfx_platform.market_sessions import SAST, market_window
from mt5_xm_config import MT5RuntimeConfig


def sast(day, hour, minute=0):
    return datetime(2026, 7, day, hour, minute, tzinfo=SAST)


class MarketSessionTests(unittest.TestCase):
    def test_active_symbol_universe_is_ten(self):
        payload = json.loads(Path("/opt/cipherfx_mt5/mt5_symbols.json").read_text())
        symbols = (
            payload["forex"]
            + payload["metals"]
            + payload["indices"]
        )
        self.assertEqual(len(symbols), 10)
        self.assertNotIn("SILVER", symbols)
        self.assertNotIn("XAGUSD", symbols)
        for disabled in ("GBPUSD", "EURUSD", "NZDUSD", "AUDUSD", "USDCAD", "EURJPY"):
            self.assertNotIn(disabled, symbols)
        self.assertEqual(len(MT5RuntimeConfig().all_symbols()), 10)

    def test_forex_week_boundary_is_sast(self):
        self.assertFalse(market_window("USDJPY", "forex", sast(19, 22, 59))["open"])
        self.assertTrue(market_window("USDJPY", "forex", sast(19, 23, 0))["open"])
        self.assertTrue(market_window("USDJPY", "forex", sast(25, 2, 0))["open"])
        self.assertFalse(market_window("USDJPY", "forex", sast(25, 2, 1))["open"])

    def test_us_index_window(self):
        self.assertFalse(market_window("US30", "index", sast(20, 15, 29))["open"])
        self.assertTrue(market_window("US30", "index", sast(20, 15, 30))["open"])
        self.assertTrue(market_window("US30", "index", sast(20, 21, 59))["open"])
        self.assertFalse(market_window("US30", "index", sast(20, 22, 0))["open"])

    def test_uk_europe_window(self):
        self.assertFalse(market_window("UK100", "index", sast(20, 9, 59))["open"])
        self.assertTrue(market_window("UK100", "index", sast(20, 10, 0))["open"])
        self.assertFalse(market_window("GER40", "index", sast(20, 19, 30))["open"])

    def test_asia_window(self):
        self.assertTrue(market_window("JP225", "index", sast(20, 2, 0))["open"])
        self.assertTrue(market_window("JP225", "index", sast(20, 9, 59))["open"])
        self.assertFalse(market_window("JP225", "index", sast(20, 10, 0))["open"])

    def test_metal_uses_weekday_market_window(self):
        self.assertTrue(market_window("XAUUSD", "metal", sast(20, 12, 0))["open"])
        self.assertFalse(market_window("XAUUSD", "metal", sast(25, 3, 0))["open"])


if __name__ == "__main__":
    unittest.main()
