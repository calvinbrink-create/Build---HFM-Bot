from __future__ import annotations

import inspect
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from cipherfx_platform.contracts import TradeProposal
from cipherfx_platform.database import DatabaseLayer
from cipherfx_platform.execution import ExecutionEngine
from cipherfx_platform.feedback import LearningFeedbackEngine
from cipherfx_platform.runtime import ModularTradingRuntime
from mt5_xm_gateway import MT5SymbolSpec


class FakeGateway:
    def __init__(self):
        self.calls = []
        self._spec = MT5SymbolSpec(
            symbol="EURUSD", visible=True, digits=5, point=0.00001,
            volume_min=0.01, volume_max=10.0, volume_step=0.01,
        )
    def account_info(self):
        return SimpleNamespace(
            equity=10000.0, free_margin=9000.0, terminal_connected=True,
            trade_allowed=True, account_trade_allowed=True,
        )
    def positions(self):
        return []
    def symbol_tick(self, symbol):
        return symbol, SimpleNamespace(bid=1.1000, ask=1.1001)
    def symbol_info(self, symbol):
        return self._spec
    def order_calc_profit(self, symbol, side, volume, entry, close):
        return (close - entry) * 100000.0 * volume * (1 if side == "BUY" else -1)
    def order_calc_margin(self, symbol, side, volume, entry):
        return 100.0 * volume
    def normalize_volume(self, spec, volume):
        return round(min(spec.volume_max, max(spec.volume_min, volume)), 2)
    def place_market_order(self, *args):
        self.calls.append(args)
        return args[0], 1.1001, SimpleNamespace(retcode=10009, order=10, deal=11, position=12, comment="done")
    def close_position(self, position):
        return True
    def request_shutdown(self):
        pass


def proposal(expires=None):
    now = datetime.now(timezone.utc)
    return TradeProposal(
        proposal_id="proposal-1", symbol="EURUSD", asset_class="forex",
        side="BUY", entry_price=1.1001, stop_loss=1.0990,
        take_profit=1.1018, volume_hint=0.0, strategy_name="test",
        score=85.0, score_components={"m5_trigger": 25.0},
        created_at=now, expires_at=expires or now + timedelta(seconds=30),
    )


class Phase1ModularPlatformTests(unittest.TestCase):
    def test_execution_has_no_market_analysis_dependency(self):
        from cipherfx_platform import execution
        source = inspect.getsource(execution)
        for forbidden in ("LearningEngine", "ScoringEngine", "MarketSnapshot", "EMA", "ATR", "ADX", "RSI", "BOS", "CHOCH"):
            self.assertNotIn(forbidden, source)

    def test_runtime_does_not_import_legacy_route(self):
        from cipherfx_platform import runtime
        source = inspect.getsource(runtime)
        for forbidden in ("mt5_bot", "strategies.architecture", "scalping_bot_v4"):
            self.assertNotIn(forbidden, source)

    def test_database_and_execution_idempotency(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = DatabaseLayer(Path(tmp) / "platform.db")
            config = SimpleNamespace(
                max_daily_trades=30, max_open_trades=30,
                dry_run=True, trade_mode="paper",
            )
            gateway = FakeGateway()
            engine = ExecutionEngine(gateway, config, db)
            first = engine.submit(proposal())
            second = engine.submit(proposal())
            self.assertEqual(first.status, "DRY_RUN")
            self.assertEqual(second.status, "DUPLICATE_SUPPRESSED")
            self.assertEqual(len(gateway.calls), 0)

    def test_live_path_submits_once_after_preflight(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = DatabaseLayer(Path(tmp) / "platform.db")
            config = SimpleNamespace(
                max_daily_trades=30, max_open_trades=30,
                dry_run=False, trade_mode="live",
            )
            gateway = FakeGateway()
            engine = ExecutionEngine(gateway, config, db)
            result = engine.submit(proposal())
            self.assertEqual(result.status, "FILLED")
            self.assertEqual(len(gateway.calls), 1)
            duplicate = engine.submit(proposal())
            self.assertEqual(duplicate.status, "DUPLICATE_SUPPRESSED")

    def test_feedback_only_consumes_trade_outcome(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = DatabaseLayer(Path(tmp) / "platform.db")
            feedback = LearningFeedbackEngine(db)
            record = feedback.record_closed_trade("t1", "EURUSD", "BUY", 20.0, 10.0, "TP")
            self.assertEqual(record.result_r, 2.0)

    def test_contracts_are_importable_without_runtime_connection(self):
        self.assertTrue(TradeProposal)
        self.assertTrue(ModularTradingRuntime)


if __name__ == "__main__":
    unittest.main()
