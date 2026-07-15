import tempfile
import time
import unittest
from pathlib import Path

from market_data.feed_health import FeedHealth, LIVE_DATA, STALE_MARKET_DATA
from tick_websocket import BridgeTickFileRecovery


class Phase5MarketDataTests(unittest.TestCase):
    def test_no_tick_is_stale(self):
        health = FeedHealth(max_age_seconds=5)
        allowed, reason = health.entry_allowed("XAUUSD", now=1000)
        self.assertFalse(allowed)
        self.assertIn(STALE_MARKET_DATA, reason)

    def test_fresh_tick_allows_and_expiry_blocks(self):
        health = FeedHealth(max_age_seconds=5)
        transition = health.update_tick(
            "XAUUSD",
            998,
            received_epoch=1000,
            source="bridge_tick_file_recovery",
        )
        self.assertEqual(transition["state"], LIVE_DATA)
        self.assertEqual(health.status("XAUUSD", now=1000)["source"], "bridge_tick_file_recovery")
        self.assertTrue(health.entry_allowed("XAUUSD", now=1003)[0])
        allowed, reason = health.entry_allowed("XAUUSD", now=1004)
        self.assertFalse(allowed)
        self.assertIn(STALE_MARKET_DATA, reason)

    def test_real_tick_recovers_after_stale_timeout(self):
        health = FeedHealth(max_age_seconds=5)
        health.update_tick("XAUUSD", 998, received_epoch=1000)
        self.assertFalse(health.entry_allowed("XAUUSD", now=1006)[0])
        transition = health.update_tick("XAUUSD", 1006, received_epoch=1006)
        self.assertEqual(transition["previous_state"], STALE_MARKET_DATA)
        self.assertEqual(transition["state"], LIVE_DATA)
        self.assertTrue(health.entry_allowed("XAUUSD", now=1006)[0])

    def test_out_of_order_tick_does_not_move_clock_backwards(self):
        health = FeedHealth(max_age_seconds=5)
        health.update_tick("XAUUSD", 1006, received_epoch=1006)
        transition = health.update_tick("XAUUSD", 1000, received_epoch=1007)
        self.assertFalse(transition["accepted"])
        self.assertEqual(transition["reason"], "out_of_order_tick_ignored")
        self.assertEqual(health.status("XAUUSD", now=1007)["last_tick_epoch"], 1006)
        self.assertTrue(health.entry_allowed("XAUUSD", now=1007)[0])

    def test_symbol_freshness_is_independent(self):
        health = FeedHealth(max_age_seconds=5)
        health.update_tick("XAUUSD", 998, received_epoch=1000)
        health.update_tick("EURUSD", 990, received_epoch=1000)
        self.assertTrue(health.entry_allowed("XAUUSD", now=1003)[0])
        self.assertFalse(health.entry_allowed("EURUSD", now=1003)[0])
        snap = health.snapshot(symbols=["XAUUSD", "EURUSD"], now=1003)
        self.assertEqual(snap["fresh_symbols"], ["XAUUSD"])
        self.assertEqual(snap["last_tick_source"], "websocket")
        self.assertEqual(snap["stale_symbols"], ["EURUSD"])

    def test_transport_connected_without_tick_remains_stale(self):
        health = FeedHealth(max_age_seconds=5)
        health.set_transport(True, 1, now=1000)
        snap = health.snapshot(symbols=["GER40"], now=1000)
        self.assertEqual(snap["state"], STALE_MARKET_DATA)
        self.assertEqual(snap["reason"], "websocket_connected_without_fresh_symbol_tick")

    def test_bridge_tick_file_recovery_emits_only_real_new_ticks(self):
        with tempfile.TemporaryDirectory() as directory:
            events = []
            recovery = BridgeTickFileRecovery(
                Path(directory),
                events.append,
                symbol_normalizer=lambda symbol: {"GOLD": "XAUUSD"}.get(symbol, symbol),
                interval_seconds=0.05,
            )
            self.assertTrue(recovery.start())
            tick_path = Path(directory) / "tick_GOLD.txt"
            tick_path.write_text(
                "time_utc=2000\n"
                "bid=4028.5\n"
                "ask=4029.0\n"
                "last=0\n",
                encoding="utf-8",
            )
            deadline = time.time() + 2
            while time.time() < deadline and not events:
                time.sleep(0.02)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["symbol"], "XAUUSD")
            self.assertEqual(events[0]["time_utc"], 2000.0)
            self.assertEqual(events[0]["_feed_source"], "bridge_tick_file_recovery")
            tick_path.write_text(
                "time_utc=2000\n"
                "bid=4028.6\n"
                "ask=4029.1\n"
                "last=0\n",
                encoding="utf-8",
            )
            time.sleep(0.15)
            self.assertEqual(len(events), 1)
            tick_path.write_text(
                "time_utc=2001\n"
                "bid=4028.7\n"
                "ask=4029.2\n"
                "last=0\n",
                encoding="utf-8",
            )
            deadline = time.time() + 2
            while time.time() < deadline and len(events) < 2:
                time.sleep(0.02)
            recovery.stop()
            self.assertEqual(len(events), 2)
            self.assertEqual(events[-1]["time_utc"], 2001.0)


if __name__ == "__main__":
    unittest.main()
