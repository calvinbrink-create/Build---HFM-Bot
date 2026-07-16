import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from cipherfx_platform.contracts import TradeProposal
from cipherfx_platform.database import DatabaseLayer
from cipherfx_platform.execution import ExecutionEngine
from mt5_xm_gateway import MT5SymbolSpec


class FailingGateway:
    def account_info(self):
        return SimpleNamespace(equity=10000, free_margin=9000, terminal_connected=True, trade_allowed=True, account_trade_allowed=True)
    def positions(self): return []
    def symbol_tick(self, symbol): return symbol, SimpleNamespace(bid=1.1, ask=1.1001)
    def symbol_info(self, symbol): return MT5SymbolSpec(symbol=symbol, point=0.00001, volume_min=0.01, volume_max=10, volume_step=0.01)
    def order_calc_profit(self, *args): raise RuntimeError("broker 4756")
    def order_calc_margin(self, *args): return 1.0
    def normalize_volume(self, spec, volume): return 0.01


class Phase2ExecutionBoundaryTests(unittest.TestCase):
    def test_broker_exception_is_persisted_and_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = DatabaseLayer(Path(tmp) / "platform.db")
            config = SimpleNamespace(max_daily_trades=30, max_open_trades=30, dry_run=False, trade_mode="demo")
            engine = ExecutionEngine(FailingGateway(), config, db)
            now = datetime.now(timezone.utc)
            p = TradeProposal("broker-error", "FRA40", "index", "BUY", 1.1, 1.0, 1.3, 0,
                               "INDICES", 70, {}, now, now + timedelta(seconds=30))
            first = engine.submit(p)
            second = engine.submit(p)
            self.assertEqual(first.status, "EXECUTION_BLOCKED")
            self.assertIn("BROKER_ERROR", first.reason)
            self.assertEqual(second.status, "DUPLICATE_SUPPRESSED")


if __name__ == "__main__":
    unittest.main()
