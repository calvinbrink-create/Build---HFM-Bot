from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from cipherfx_platform.database import DatabaseLayer
from cipherfx_platform.management import TradeManagementEngine


class DatabaseGrowthTests(unittest.TestCase):
    def position(self):
        return SimpleNamespace(
            ticket=501,
            symbol="TEST",
            direction="BUY",
            volume=1.0,
            price_open=1.0,
            price_current=1.05,
            sl=0.9,
            tp=1.3,
            profit=5.0,
            time=datetime.now(timezone.utc),
        )

    def test_management_state_continues_without_per_loop_monitor_event_growth(self):
        class Gateway:
            def __init__(self, position):
                self.position = position

            def positions(self):
                return [self.position]

            def account_info(self):
                return SimpleNamespace(equity=10000, balance=10000, margin=0, free_margin=10000, margin_level=0)

            def symbol_tick(self, symbol):
                return symbol, SimpleNamespace(bid=1.0499, ask=1.0501)

            def symbol_info(self, symbol):
                return SimpleNamespace(point=0.0001, digits=4, stops_level=0)

            def modify_position(self, *args):
                return SimpleNamespace(retcode=10009)

        with tempfile.TemporaryDirectory() as directory:
            database = DatabaseLayer(Path(directory) / "platform.db")
            gateway = Gateway(self.position())
            manager = TradeManagementEngine(gateway, database)
            manager.monitor_event_seconds = 60.0
            manager.monitor()
            manager.monitor()
            with sqlite3.connect(database.path) as conn:
                monitor_count = conn.execute(
                    "SELECT COUNT(1) FROM platform_events WHERE event_type='POSITION_MONITOR'"
                ).fetchone()[0]
                state_count = conn.execute(
                    "SELECT COUNT(1) FROM position_management WHERE position_ticket=501"
                ).fetchone()[0]
            self.assertEqual(monitor_count, 1)
            self.assertEqual(state_count, 1)

    def test_database_checkpoint_has_bounded_wal_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            database = DatabaseLayer(Path(directory) / "platform.db")
            with database._connect() as conn:
                journal_limit = conn.execute("PRAGMA journal_size_limit").fetchone()[0]
                autocheckpoint = conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0]
            self.assertEqual(journal_limit, 262144000)
            self.assertEqual(autocheckpoint, 1000)
            result = database.checkpoint()
            self.assertEqual(result["busy"], 0)


if __name__ == "__main__":
    unittest.main()
