from __future__ import annotations

import inspect
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from cipherfx_platform.contracts import ExecutionResult, TradeProposal
from cipherfx_platform.database import DatabaseLayer
from cipherfx_platform.feedback import LearningFeedbackEngine
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




class FailingManagementGateway(ManagementGateway):
    def modify_position(self, ticket, symbol, sl, tp):
        raise RuntimeError("broker 4756")


class MissingExportManagementGateway(ManagementGateway):
    def symbol_tick(self, symbol):
        raise RuntimeError(f"MT5 bridge symbol not exported: {symbol}")

    def symbol_info(self, symbol):
        raise RuntimeError(f"MT5 bridge symbol not exported: {symbol}")


class FailingCloseGateway(ManagementGateway):
    def close_position(self, position):
        raise RuntimeError("broker close failure")
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
        # r=1.3 clears the new trail_start_r=1.2 (raised from 0.5) but stays
        # below profit_take_r=1.5, so this exercises the trail-modify path.
        with tempfile.TemporaryDirectory() as tmp:
            db = DatabaseLayer(Path(tmp) / "platform.db")
            gateway = ManagementGateway(self.position(sl=0.9, current=1.13))
            manager = TradeManagementEngine(gateway, db)
            manager.monitor()
            self.assertEqual(len(gateway.modified), 1)
            self.assertAlmostEqual(gateway.modified[0][2], 1.105)
            with sqlite3.connect(db.path) as conn:
                state = conn.execute(
                    "SELECT state_json FROM position_management WHERE position_ticket=77"
                ).fetchone()[0]
                event = conn.execute(
                    "SELECT event_type FROM platform_events WHERE event_type='POSITION_MANAGEMENT_ACTION'"
                ).fetchone()[0]
            self.assertEqual(event, "POSITION_MANAGEMENT_ACTION")
            parsed_state = json.loads(state)
            self.assertAlmostEqual(parsed_state["mfe_r"], 1.3)
            self.assertEqual(parsed_state["rank"], "EXCELLENT")

    def test_profitable_position_is_closed_at_configured_profit_take(self):
        # r=1.6 clears the new profit_take_r=1.5 (raised from 0.6).
        with tempfile.TemporaryDirectory() as tmp:
            db = DatabaseLayer(Path(tmp) / "platform.db")
            gateway = ManagementGateway(self.position(sl=0.9, current=1.16))
            manager = TradeManagementEngine(gateway, db)
            manager.monitor()
            self.assertEqual(gateway.closed, [77])
            with sqlite3.connect(db.path) as conn:
                payload = conn.execute(
                    "SELECT payload_json FROM platform_events WHERE event_type='POSITION_MANAGEMENT_ACTION'"
                ).fetchone()[0]
            self.assertIn("PROFIT_TAKE_R_REACHED", payload)

    def test_missing_export_stop_modification_is_not_repeated_each_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = DatabaseLayer(Path(tmp) / "platform.db")
            gateway = MissingExportManagementGateway(self.position(sl=0.9, current=1.13))
            manager = TradeManagementEngine(gateway, db)
            manager.monitor()
            manager.monitor()
            with sqlite3.connect(db.path) as conn:
                errors = conn.execute(
                    "SELECT COUNT(*) FROM platform_events WHERE event_type='POSITION_MANAGEMENT_ERROR'"
                ).fetchone()[0]
            self.assertEqual(errors, 1)

    def test_failed_close_is_not_repeated_each_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = DatabaseLayer(Path(tmp) / "platform.db")
            gateway = FailingCloseGateway(self.position(sl=0.9, current=1.16))
            manager = TradeManagementEngine(gateway, db)
            manager.monitor()
            manager.monitor()
            with sqlite3.connect(db.path) as conn:
                errors = conn.execute(
                    "SELECT COUNT(*) FROM platform_events WHERE event_type='POSITION_MANAGEMENT_ERROR'"
                ).fetchone()[0]
            self.assertEqual(errors, 1)

    def test_failed_stop_modification_is_not_repeated_each_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = DatabaseLayer(Path(tmp) / "platform.db")
            gateway = FailingManagementGateway(self.position(sl=0.9, current=1.13))
            manager = TradeManagementEngine(gateway, db)
            manager.monitor()
            manager.monitor()
            with sqlite3.connect(db.path) as conn:
                errors = conn.execute(
                    "SELECT COUNT(*) FROM platform_events WHERE event_type='POSITION_MANAGEMENT_ERROR'"
                ).fetchone()[0]
            self.assertEqual(errors, 1)

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

    def test_configured_open_position_loss_cap_closes_before_unmanaged_loss_grows(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = DatabaseLayer(Path(tmp) / "platform.db")
            gateway = ManagementGateway(self.position(sl=0.9, current=0.93))
            gateway.position.profit = -160.0
            manager = TradeManagementEngine(gateway, db)
            manager.profiles = {"TEST": {"max_open_position_loss_usd": 150.0}}
            manager.monitor()
            self.assertEqual(gateway.closed, [77])
            with sqlite3.connect(db.path) as conn:
                payload = conn.execute(
                    "SELECT payload_json FROM platform_events WHERE event_type='POSITION_MANAGEMENT_ACTION'"
                ).fetchone()[0]
            self.assertIn("MAX_OPEN_POSITION_LOSS", payload)

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


    def test_management_accumulates_intelligence_and_actions(self):
        # r=1.3 then r=1.6 clears the new trail_start_r=1.2 and then
        # profit_take_r=1.5 (raised from 0.5/0.6) so an action is still
        # recorded on each pass.
        with tempfile.TemporaryDirectory() as tmp:
            db = DatabaseLayer(Path(tmp) / "platform.db")
            gateway = ManagementGateway(self.position(sl=0.9, current=1.13))
            manager = TradeManagementEngine(gateway, db)
            manager.monitor()
            gateway.position.price_current = 1.16
            gateway.position.profit = 8.0
            manager.monitor()
            with sqlite3.connect(db.path) as conn:
                state_json = conn.execute(
                    "SELECT state_json FROM position_management WHERE position_ticket=77"
                ).fetchone()[0]
            state = json.loads(state_json)
            metrics = state["last_metrics"]
            self.assertGreaterEqual(state["mfe_r"], 0.8)
            self.assertIsNotNone(metrics["volatility"])
            self.assertIn("momentum", metrics)
            self.assertIn("profit_acceleration_r", metrics)
            self.assertIn("trend_continuation", metrics)
            self.assertGreaterEqual(len(state["price_history"]), 3)
            self.assertGreaterEqual(len(state["action_log"]), 1)

    def test_restart_restores_original_proposal_geometry(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = DatabaseLayer(Path(tmp) / "platform.db")
            created = datetime.now(timezone.utc)
            proposal = TradeProposal(
                "proposal-77",
                "TEST",
                "forex",
                "BUY",
                1.0,
                0.9,
                1.3,
                0.1,
                "FOREX",
                80.0,
                {"context": 20.0},
                created,
                created + timedelta(minutes=5),
                context={"engine": "FOREX"},
                confidence=80.0,
                probability=80.0,
                risk_amount=10.0,
            )
            result = ExecutionResult(
                "proposal-77",
                "FILLED",
                "TEST",
                "BUY",
                1.0,
                order_ticket=100,
                deal_ticket=101,
                position_ticket=77,
                fill_price=1.0,
                filled_at=created,
                initial_risk=10.0,
            )
            db.save_proposal(proposal)
            db.save_execution_result(proposal, result)
            gateway = ManagementGateway(self.position(sl=1.04, current=1.05))
            manager = TradeManagementEngine(gateway, db)
            manager.monitor()
            with sqlite3.connect(db.path) as conn:
                state_json = conn.execute(
                    "SELECT state_json FROM position_management WHERE position_ticket=77"
                ).fetchone()[0]
            state = json.loads(state_json)
            self.assertEqual(state["initial_entry"], 1.0)
            self.assertEqual(state["initial_sl"], 0.9)
            self.assertEqual(state["initial_tp"], 1.3)
            self.assertEqual(state["initial_risk_amount"], 10.0)

    def test_closed_trade_feedback_contains_management_history(self):
        class ClosedGateway:
            def positions(self):
                return []

            def deals_for_position(self, ticket):
                return [
                    SimpleNamespace(
                        time_utc=(datetime.now(timezone.utc) - timedelta(seconds=30)).timestamp(),
                        profit=12.0,
                        swap=0.0,
                        commission=0.0,
                    )
                ]

        with tempfile.TemporaryDirectory() as tmp:
            db = DatabaseLayer(Path(tmp) / "platform.db")
            created = datetime.now(timezone.utc) - timedelta(minutes=2)
            proposal = TradeProposal(
                "proposal-closed",
                "TEST",
                "forex",
                "BUY",
                1.0,
                0.9,
                1.3,
                0.1,
                "FOREX",
                80.0,
                {"context": 20.0},
                created,
                created + timedelta(minutes=5),
                context={"engine": "FOREX"},
                confidence=80.0,
                probability=80.0,
                risk_amount=10.0,
            )
            result = ExecutionResult(
                "proposal-closed",
                "FILLED",
                "TEST",
                "BUY",
                1.0,
                order_ticket=110,
                deal_ticket=111,
                position_ticket=88,
                fill_price=1.0,
                filled_at=created,
                initial_risk=10.0,
            )
            db.save_proposal(proposal)
            db.save_execution_result(proposal, result)
            closed_position = self.position()
            closed_position.ticket = 88
            db.save_position(closed_position)
            db.save_management_state(
                88,
                {
                    "mfe_r": 1.2,
                    "mae_r": -0.3,
                    "rank": "STRONG",
                    "last_seen_at": datetime.now(timezone.utc).isoformat(),
                    "last_metrics": {"drawdown_r": 0.2},
                    "action_log": [{"action": "MODIFY_SL", "reason": "BREAK_EVEN"}],
                },
            )
            feedback = LearningFeedbackEngine(db)
            self.assertEqual(feedback.reconcile_closed_trades(ClosedGateway()), 1)
            with sqlite3.connect(db.path) as conn:
                row = conn.execute(
                    "SELECT metrics_json FROM trade_history WHERE trade_id='proposal-closed'"
                ).fetchone()[0]
                state = conn.execute(
                    "SELECT state FROM positions WHERE position_ticket=88"
                ).fetchone()[0]
            metrics = json.loads(row)
            self.assertEqual(metrics["mfe_r"], 1.2)
            self.assertEqual(metrics["management_rank"], "STRONG")
            self.assertEqual(metrics["management_actions"][0]["action"], "MODIFY_SL")
            self.assertEqual(state, "CLOSED")


if __name__ == "__main__":
    unittest.main()
