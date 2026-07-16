from __future__ import annotations

from dataclasses import replace
import inspect
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from cipherfx_platform.contracts import Candle, ExecutionResult, Frame, MarketSnapshot, Tick, TradeProposal
from cipherfx_platform.database import DatabaseLayer
from cipherfx_platform.engines import LearningEngine
from cipherfx_platform.execution import ExecutionEngine
from cipherfx_platform.feedback import LearningFeedbackEngine
from cipherfx_platform.management import TradeManagementEngine
from mt5_xm_gateway import MT5SymbolSpec


def make_proposal(proposal_id="p3", asset_class="forex"):
    now = datetime.now(timezone.utc)
    return TradeProposal(
        proposal_id=proposal_id,
        symbol="EURUSD" if asset_class == "forex" else "US500",
        asset_class=asset_class,
        side="BUY",
        entry_price=1.1001,
        stop_loss=1.0990,
        take_profit=1.1018,
        volume_hint=0.0,
        strategy_name="TEST",
        score=82.0,
        score_components={"timeframe": 60.0, "features": 22.0},
        created_at=now,
        expires_at=now + timedelta(seconds=30),
        context={"engine": {"forex": "FOREX", "index": "INDICES", "metal": "METALS"}[asset_class]},
        confidence=82.0,
        probability=72.0,
        reasoning=("test",),
        risk_amount=10.0,
    )


class ClosedGateway:
    def __init__(self):
        self.deals = [
            SimpleNamespace(
                position_id=55,
                net_profit=25.0,
                profit=25.0,
                swap=0.0,
                commission=0.0,
                time_utc=datetime.now(timezone.utc).timestamp(),
            )
        ]

    def positions(self):
        return []

    def deals_for_position(self, ticket):
        return self.deals if int(ticket) == 55 else []


class OperationalGateway:
    def account_info(self):
        return SimpleNamespace(
            equity=10000.0,
            free_margin=9000.0,
            terminal_connected=True,
            trade_allowed=True,
            account_trade_allowed=True,
        )

    def positions(self):
        return []

    def symbol_tick(self, symbol):
        return symbol, SimpleNamespace(bid=1.1000, ask=1.1001)

    def symbol_info(self, symbol):
        return MT5SymbolSpec(
            symbol=symbol,
            visible=True,
            point=0.00001,
            volume_min=0.01,
            volume_max=10.0,
            volume_step=0.01,
        )

    def order_calc_profit(self, symbol, side, volume, entry, close):
        return (close - entry) * 100000.0 * volume * (1 if side == "BUY" else -1)

    def order_calc_margin(self, symbol, side, volume, entry):
        return 100.0 * volume

    def normalize_volume(self, spec, volume):
        return round(min(spec.volume_max, max(spec.volume_min, volume)), 2)


class Phase3LearningSystemTests(unittest.TestCase):
    def test_closed_trade_updates_only_originating_engine(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = DatabaseLayer(Path(tmp) / "platform.db")
            intelligence = LearningEngine(db)
            feedback = LearningFeedbackEngine(db, intelligence.record_outcome)
            metrics = {
                "spread": 1.2,
                "volatility": 0.8,
                "session": "London",
                "entry_timing_seconds": 2.0,
                "exit_timing_seconds": 18.0,
                "holding_time_seconds": 120.0,
                "drawdown": -4.0,
                "profit": 20.0,
                "false_positive": False,
                "false_negative": False,
                "missed_trade": False,
            }
            feedback.record_closed_trade(
                "closed-fx",
                "EURUSD",
                "BUY",
                20.0,
                10.0,
                "TP",
                asset_class="forex",
                metrics=metrics,
            )
            self.assertEqual(len(db.engine_history("FOREX")), 1)
            self.assertEqual(db.engine_history("INDICES"), [])
            self.assertEqual(db.engine_history("METALS"), [])
            with sqlite3.connect(db.path) as conn:
                row = conn.execute("SELECT metrics_json FROM learning_metrics").fetchone()
            saved = json.loads(row[0])
            self.assertEqual(saved["session"], "London")
            self.assertEqual(saved["holding_time_seconds"], 120.0)

    def test_expired_proposal_is_recorded_without_mutating_proposal_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = DatabaseLayer(Path(tmp) / "platform.db")
            intelligence = LearningEngine(db)
            feedback = LearningFeedbackEngine(db, intelligence.record_outcome)
            proposal = make_proposal("expired-fx")
            db.save_proposal(proposal)
            original = (proposal.proposal_id, proposal.entry_price, proposal.stop_loss, proposal.take_profit)
            feedback.record_expired_proposal(
                proposal.proposal_id,
                proposal.symbol,
                proposal.side,
                proposal.asset_class,
            )
            self.assertEqual(
                original,
                (proposal.proposal_id, proposal.entry_price, proposal.stop_loss, proposal.take_profit),
            )
            self.assertEqual(len(db.engine_history("FOREX")), 1)
            with sqlite3.connect(db.path) as conn:
                state = conn.execute(
                    "SELECT state FROM trade_proposals WHERE proposal_id=?",
                    (proposal.proposal_id,),
                ).fetchone()[0]
            self.assertEqual(state, "EXPIRED")

    def test_snapshots_proposals_orders_executions_and_metrics_are_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = DatabaseLayer(Path(tmp) / "platform.db")
            candle = Candle(datetime.now(timezone.utc), 1.0, 1.2, 0.9, 1.1, 4.0)
            snapshot = MarketSnapshot(
                "EURUSD",
                "forex",
                {name: Frame(name, (candle,)) for name in ("H4", "H1", "M15", "M5", "M1")},
                Tick("EURUSD", 1.0, 1.1, candle.timestamp, candle.timestamp),
                candle.timestamp,
            )
            proposal = make_proposal()
            db.save_snapshot("snapshot-1", snapshot)
            db.save_proposal(proposal)
            result = ExecutionResult(
                proposal.proposal_id,
                "EXECUTION_BLOCKED",
                proposal.symbol,
                proposal.side,
                0.0,
                reason="SPREAD_LIMIT",
            )
            db.execution(proposal.proposal_id, proposal.symbol, proposal.side, result.status, 0.0)
            db.save_execution_result(proposal, result)
            with sqlite3.connect(db.path) as conn:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                counts = {
                    table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in ("market_snapshots", "trade_proposals", "orders", "executions")
                }
            self.assertTrue({"market_snapshots", "trade_proposals", "orders", "executions", "learning_metrics"} <= tables)
            self.assertEqual(counts, {"market_snapshots": 1, "trade_proposals": 1, "orders": 1, "executions": 1})

    def test_execution_operational_failure_is_blocked_without_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = DatabaseLayer(Path(tmp) / "platform.db")
            config = SimpleNamespace(max_daily_trades=0, max_open_trades=30, dry_run=False, trade_mode="live")
            execution = ExecutionEngine(OperationalGateway(), config, db)
            result = execution.submit(make_proposal())
            self.assertEqual(result.status, "EXECUTION_BLOCKED")
            source = inspect.getsource(ExecutionEngine)
            for forbidden in ("MarketSnapshot", "ScoringEngine", "EMA", "ATR", "ADX", "RSI", "RMI", "BOS", "CHOCH"):
                self.assertNotIn(forbidden, source)

    def test_closed_broker_position_reaches_feedback_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = DatabaseLayer(Path(tmp) / "platform.db")
            intelligence = LearningEngine(db)
            feedback = LearningFeedbackEngine(db, intelligence.record_outcome)
            proposal = make_proposal("filled-fx")
            db.save_proposal(proposal)
            result = ExecutionResult(
                proposal.proposal_id,
                "FILLED",
                proposal.symbol,
                proposal.side,
                0.1,
                order_ticket=10,
                deal_ticket=11,
                position_ticket=55,
                fill_price=proposal.entry_price,
                filled_at=datetime.now(timezone.utc) - timedelta(seconds=120),
                initial_risk=10.0,
            )
            db.execution(proposal.proposal_id, proposal.symbol, proposal.side, "FILLED", 0.1, initial_risk=10.0)
            db.save_execution_result(proposal, result)
            gateway = ClosedGateway()
            self.assertEqual(feedback.reconcile_closed_trades(gateway), 1)
            self.assertEqual(feedback.reconcile_closed_trades(gateway), 0)
            self.assertEqual(len(db.engine_history("FOREX")), 1)
            with sqlite3.connect(db.path) as conn:
                status = conn.execute(
                    "SELECT status FROM executions WHERE proposal_id=?",
                    (proposal.proposal_id,),
                ).fetchone()[0]
            self.assertEqual(status, "CLOSED")

    def test_feedback_is_idempotent_and_engine_scoped(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = DatabaseLayer(Path(tmp) / "platform.db")
            learning = LearningEngine(db)
            feedback = LearningFeedbackEngine(db, learning.record_outcome)
            metrics = {
                "spread": 1.0,
                "volatility": 1.4,
                "session": "New York",
                "entry_timing_seconds": 1.2,
                "exit_timing_seconds": 20.0,
                "holding_time_seconds": 90.0,
                "drawdown": -3.0,
                "profit": 15.0,
                "false_positive": False,
                "false_negative": False,
                "missed_trade": False,
            }
            feedback.record_closed_trade("same-trade", "EURUSD", "BUY", 15.0, 10.0, "TP", asset_class="forex", metrics=metrics)
            feedback.record_closed_trade("same-trade", "EURUSD", "BUY", 15.0, 10.0, "TP", asset_class="forex", metrics=metrics)
            with sqlite3.connect(db.path) as conn:
                history = conn.execute("SELECT COUNT(*) FROM trade_history").fetchone()[0]
                learning_rows = conn.execute("SELECT COUNT(*) FROM learning_metrics").fetchone()[0]
                performance = conn.execute("SELECT COUNT(*) FROM engine_performance WHERE engine='FOREX'").fetchone()[0]
                other_engines = conn.execute("SELECT COUNT(*) FROM engine_performance WHERE engine<>'FOREX'").fetchone()[0]
            self.assertEqual((history, learning_rows, performance, other_engines), (1, 1, 1, 0))

    def test_database_does_not_replace_an_immutable_proposal(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = DatabaseLayer(Path(tmp) / "platform.db")
            original = make_proposal("immutable")
            altered = replace(original, entry_price=9.99, score=1.0, reasoning=("altered",))
            db.save_proposal(original)
            db.save_proposal(altered)
            with sqlite3.connect(db.path) as conn:
                row = conn.execute(
                    "SELECT entry_price,score,payload_json FROM trade_proposals WHERE proposal_id='immutable'"
                ).fetchone()
            self.assertEqual(row[0], original.entry_price)
            self.assertEqual(row[1], original.score)
            self.assertNotIn("altered", row[2])

    def test_management_does_not_contain_analysis_authority(self):
        source = inspect.getsource(TradeManagementEngine)
        for forbidden in ("EMA", "ATR", "ADX", "RSI", "BOS", "CHOCH", "MarketSnapshot", "Scoring"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
