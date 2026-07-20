from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from cipherfx_platform.contracts import TradeProposal
from cipherfx_platform.database import DatabaseLayer
from cipherfx_platform.execution import ExecutionEngine, ExecutionLimits
from mt5_xm_gateway import MT5SymbolSpec


class MarginFitGateway:
    def __init__(self, free_margin):
        self.free_margin = free_margin
        self.submitted = []

    def account_info(self):
        return SimpleNamespace(
            equity=10000,
            free_margin=self.free_margin,
            terminal_connected=True,
            trade_allowed=True,
            account_trade_allowed=True,
        )

    def positions(self):
        return []

    def symbol_tick(self, symbol):
        return symbol, SimpleNamespace(bid=1.0999, ask=1.1001)

    def symbol_info(self, symbol):
        return MT5SymbolSpec(symbol=symbol, point=0.0001, volume_min=0.01, volume_max=10.0, volume_step=0.01)

    def normalize_volume(self, spec, volume):
        import math
        if volume < spec.volume_min:
            return 0.0
        steps = math.floor((min(spec.volume_max, volume) + spec.volume_step * 1e-9) / spec.volume_step)
        return round(steps * spec.volume_step, 2)

    def order_calc_profit(self, symbol, side, volume, entry, stop):
        return -1000.0 * float(volume)

    def order_calc_margin(self, symbol, side, volume, entry):
        return 2000.0 * float(volume)

    def place_market_order(self, *args):
        self.submitted.append(args)
        return "EURUSD", 1.1, SimpleNamespace(retcode=10009, order=1, deal=2, position=3, comment="done")


def proposal():
    now = datetime.now(timezone.utc)
    return TradeProposal(
        "margin-fit", "EURUSD", "forex", "BUY", 1.1, 1.0, 1.3, 0,
        "FOREX", 70, {}, now, now + timedelta(minutes=1),
    )


class Phase6MarginTests(unittest.TestCase):
    def test_risk_sized_volume_is_fitted_to_free_margin(self):
        with tempfile.TemporaryDirectory() as directory:
            gateway = MarginFitGateway(100.0)
            config = SimpleNamespace(max_daily_trades=30, max_open_trades=30, dry_run=True, trade_mode="demo")
            engine = ExecutionEngine(gateway, config, DatabaseLayer(Path(directory) / "platform.db"))
            engine.limits = ExecutionLimits(1.0, 1.0, 0.0)
            ok, reason, volume, risk = engine._preflight(proposal())
            self.assertTrue(ok)
            self.assertEqual(reason, "PASS")
            self.assertEqual(volume, 0.05)
            self.assertEqual(risk, 50.0)

    def test_minimum_volume_still_blocks_when_margin_cannot_fit(self):
        with tempfile.TemporaryDirectory() as directory:
            gateway = MarginFitGateway(10.0)
            config = SimpleNamespace(max_daily_trades=30, max_open_trades=30, dry_run=True, trade_mode="demo")
            engine = ExecutionEngine(gateway, config, DatabaseLayer(Path(directory) / "platform.db"))
            result = engine.submit(proposal())
            self.assertEqual(result.status, "EXECUTION_BLOCKED")
            self.assertEqual(result.reason, "INSUFFICIENT_MARGIN")
            self.assertEqual(gateway.submitted, [])

    def test_fitted_volume_never_exceeds_free_margin(self):
        with tempfile.TemporaryDirectory() as directory:
            gateway = MarginFitGateway(100.0)
            config = SimpleNamespace(max_daily_trades=30, max_open_trades=30, dry_run=True, trade_mode="demo")
            engine = ExecutionEngine(gateway, config, DatabaseLayer(Path(directory) / "platform.db"))
            ok, _, volume, _ = engine._preflight(proposal())
            self.assertTrue(ok)
            self.assertLessEqual(gateway.order_calc_margin("EURUSD", "BUY", volume, 1.1), gateway.free_margin)


if __name__ == "__main__":
    unittest.main()
