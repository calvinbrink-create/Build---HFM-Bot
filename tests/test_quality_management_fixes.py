from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cipherfx_platform.database import DatabaseLayer
from cipherfx_platform.execution import ExecutionEngine
from cipherfx_platform.contracts import TradeProposal
from cipherfx_platform.engines import (
    ForexLearningEngine,
    IndicesLearningEngine,
    MetalsLearningEngine,
)
from cipherfx_platform.management import TradeManagementEngine
from mt5_xm_gateway import MT5SymbolSpec


class HistoryDatabase:
    def __init__(self, rows):
        self.rows = rows

    def engine_history(self, *args, **kwargs):
        return list(self.rows)


class ManagementGateway:
    def __init__(self, position):
        self.position = position
        self.closed = []
        self.modified = []

    def positions(self):
        return [self.position]

    def symbol_tick(self, symbol):
        return symbol, SimpleNamespace(bid=self.position.price_current, ask=self.position.price_current)

    def symbol_info(self, symbol):
        return MT5SymbolSpec(symbol=symbol, point=0.0001)

    def close_position(self, position):
        self.closed.append(int(position.ticket))
        return SimpleNamespace(retcode=10009)

    def modify_position(self, ticket, symbol, sl, tp):
        self.modified.append((ticket, symbol, sl, tp))
        return SimpleNamespace(retcode=10009)


class QualityManagementFixTests(unittest.TestCase):
    def test_forex_uses_active_vps_score_gate(self):
        with patch.dict(os.environ, {"MT5_MIN_SCORE_FOREX": "65"}, clear=False):
            engine = ForexLearningEngine(None)
        self.assertEqual(engine.base_threshold, 65.0)
        self.assertEqual(engine.THRESHOLD, 65.0)

    def test_learning_adaptation_uses_eligible_history_before_old_thirty_sample_gate(self):
        rows = [{"result_r": -1.0}] * 5
        engine = ForexLearningEngine(HistoryDatabase(rows))
        adjustment = engine._history_adjustment()
        self.assertEqual(engine.last_learning_sample_size, 5)
        self.assertEqual(engine.last_learning_average_r, -1.0)
        self.assertEqual(adjustment, -2.0)

    def test_each_engine_consumes_eligible_history(self):
        rows = [{"result_r": 0.5}] * 5
        for engine in (
            IndicesLearningEngine(HistoryDatabase(rows)),
            MetalsLearningEngine(HistoryDatabase(rows)),
        ):
            adjustment = engine._adjustment()
            self.assertEqual(engine.last_learning_sample_size, 5)
            self.assertEqual(engine.last_learning_average_r, 0.5)
            self.assertEqual(adjustment, 1.0)

    def test_danger_threshold_closes_without_profile_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = DatabaseLayer(Path(tmp) / "platform.db")
            position = SimpleNamespace(
                ticket=9001,
                symbol="EURJPY",
                direction="BUY",
                volume=1.0,
                price_open=1.0,
                price_current=0.85,
                sl=0.8,
                tp=1.3,
                profit=-75.0,
                time=datetime.now(timezone.utc) - timedelta(seconds=30),
            )
            gateway = ManagementGateway(position)
            manager = TradeManagementEngine(gateway, db)
            manager.profiles = {"EURJPY": {}}
            manager.monitor()
            self.assertEqual(gateway.closed, [9001])
            with sqlite3.connect(db.path) as conn:
                payload = conn.execute(
                    "SELECT payload_json FROM platform_events "
                    "WHERE event_type='POSITION_MANAGEMENT_ACTION'"
                ).fetchone()[0]
            self.assertIn("RECOVERY_DANGER_THRESHOLD", payload)

    def test_pyramid_add_on_is_blocked_when_existing_leg_is_not_profitable(self):
        class PyramidGateway:
            def __init__(self, profit):
                self.position = SimpleNamespace(
                    symbol="USDJPYCASH",
                    direction="BUY",
                    profit=profit,
                )

            def account_info(self):
                return SimpleNamespace(
                    equity=10000.0,
                    free_margin=9000.0,
                    terminal_connected=True,
                    trade_allowed=True,
                    account_trade_allowed=True,
                )

            def positions(self):
                return [self.position]

            def symbol_tick(self, symbol):
                return symbol, SimpleNamespace(bid=1.1, ask=1.1001)

            def symbol_info(self, symbol):
                return MT5SymbolSpec(
                    symbol=symbol,
                    point=0.00001,
                    volume_min=0.01,
                    volume_max=10.0,
                    volume_step=0.01,
                )

            def order_calc_profit(self, *args):
                return 100.0

            def order_calc_margin(self, *args):
                return 10.0

            def normalize_volume(self, spec, volume):
                return 0.01

        with tempfile.TemporaryDirectory() as tmp:
            db = DatabaseLayer(Path(tmp) / "platform.db")
            config = SimpleNamespace(
                max_daily_trades=1000,
                max_open_trades=30,
                dry_run=True,
                trade_mode="demo",
                canonical_symbol=lambda value: str(value).upper().replace("CASH", ""),
            )
            now = datetime.now(timezone.utc)
            proposal = TradeProposal(
                "pyramid-check",
                "USDJPY",
                "forex",
                "BUY",
                1.1001,
                1.0,
                1.2,
                0.0,
                "FOREX",
                70.0,
                {},
                now,
                now + timedelta(seconds=30),
            )
            losing = ExecutionEngine(PyramidGateway(-0.01), config, db)
            allowed, reason, _, _ = losing._preflight(proposal)
            self.assertFalse(allowed)
            self.assertEqual(reason, "PYRAMID_LOSS_BLOCKED")

            winning = ExecutionEngine(PyramidGateway(0.01), config, db)
            allowed, reason, _, _ = winning._preflight(proposal)
            self.assertTrue(allowed)
            self.assertEqual(reason, "PASS")

    def test_mfe_and_mae_are_persisted_as_r_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = DatabaseLayer(Path(tmp) / "platform.db")
            position = SimpleNamespace(
                ticket=9002,
                symbol="EURJPY",
                direction="BUY",
                volume=1.0,
                price_open=1.0,
                price_current=1.1,
                sl=0.8,
                tp=1.3,
                profit=10.0,
                time=datetime.now(timezone.utc) - timedelta(seconds=30),
            )
            gateway = ManagementGateway(position)
            manager = TradeManagementEngine(gateway, db)
            manager.profiles = {"EURJPY": {}}
            manager.monitor()
            position.price_current = 0.9
            position.profit = -10.0
            manager.monitor()
            with sqlite3.connect(db.path) as conn:
                raw = conn.execute(
                    "SELECT state_json FROM position_management WHERE position_ticket=9002"
                ).fetchone()[0]
            state = json.loads(raw)
            self.assertAlmostEqual(state["mfe_r"], 0.5)
            self.assertAlmostEqual(state["mae_r"], 0.5)
            self.assertAlmostEqual(state["last_metrics"]["mfe_r"], 0.5)
            self.assertAlmostEqual(state["last_metrics"]["mae_r"], 0.5)


if __name__ == "__main__":
    unittest.main()
