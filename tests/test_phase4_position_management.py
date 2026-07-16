from __future__ import annotations

import inspect
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from cipherfx_platform.database import DatabaseLayer
from cipherfx_platform.management import TradeManagementEngine
from mt5_xm_gateway import MT5SymbolSpec


class ManagementGateway:
    def __init__(self, position):
        self.position = position
        self.modified = []
        self.closed = []

    def positions(self):
        return [self.position]

    def symbol_tick(self, symbol):
        return symbol, SimpleNamespace(bid=1.0499, ask=1.0501)

    def symbol_info(self, symbol):
        return MT5SymbolSpec(symbol=symbol, point=0.0001)

    def modify_position(self, ticket, symbol, sl, tp):
        self.modified.append((ticket, symbol, sl, tp))
        return SimpleNamespace(retcode=10009)

    def close_position(self, position):
        self.closed.append(position.ticket)
        return SimpleNamespace(retcode=10009)


class Phase4PositionManagementTests(unittest.TestCase):
    def position(self, *, sl=0.9, current=1.05):
        return SimpleNamespace(
            ticket=77,
            symbol="TEST",
            direction="BUY",
            volume=1.0,
            price_open=1.0,
            price_current=current,
            sl=sl,
            tp=1.3,
            profit=5.0,
            time=datetime.now(timezone.utc) - timedelta(seconds=90),
        )

    def test_monitor_persists_intelligence_and_trails_profitable_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = DatabaseLayer(Path(tmp) / "platform.db")
            gateway = ManagementGateway(self.position())
            manager = TradeManagementEngine(gateway, db)
            manager.monitor()
            self.assertEqual(len(gateway.modified), 1)
            self.assertAlmostEqual(gateway.modified[0][2], 1.025)
            with sqlite3.connect(db.path) as conn:
                state = conn.execute(
                    "SELECT state_json FROM position_management WHERE position_ticket=77"
                ).fetchone()[0]
                event = conn.execute(
                    "SELECT event_type FROM platform_events WHERE event_type='POSITION_MANAGEMENT_ACTION'"
                ).fetchone()[0]
            self.assertEqual(event, "POSITION_MANAGEMENT_ACTION")
            self.assertIn('"mfe_r":0.5', state)
            self.assertIn('"rank":"STRONG"', state)

    def test_management_is_not_a_market_or_entry_engine(self):
        source = inspect.getsource(TradeManagementEngine)
        for forbidden in (
            "TradeProposal",
            "MarketSnapshot",
            "ScoringEngine",
            "LearningEngine",
            "EMA",
            "ATR",
            "ADX",
            "RSI",
            "RMI",
            "BOS",
            "CHOCH",
            "place_market_order",
        ):
            self.assertNotIn(forbidden, source)

    def test_unmanaged_position_is_closed_and_logged(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = DatabaseLayer(Path(tmp) / "platform.db")
            gateway = ManagementGateway(self.position(sl=0.0, current=1.01))
            manager = TradeManagementEngine(gateway, db)
            manager.monitor()
            self.assertEqual(gateway.closed, [77])
            with sqlite3.connect(db.path) as conn:
                reason = conn.execute(
                    "SELECT payload_json FROM platform_events WHERE event_type='POSITION_MANAGEMENT_ACTION'"
                ).fetchone()[0]
            self.assertIn("UNMANAGED_POSITION_NO_STOP", reason)


if __name__ == "__main__":
    unittest.main()
