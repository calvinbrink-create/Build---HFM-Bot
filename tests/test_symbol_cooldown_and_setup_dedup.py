from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from cipherfx_platform.contracts import ExecutionResult, TradeProposal
from cipherfx_platform.database import DatabaseLayer
from cipherfx_platform.execution import ExecutionEngine
from mt5_xm_gateway import MT5SymbolSpec


class _Gateway:
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
        return symbol, SimpleNamespace(bid=1.0999, ask=1.1001)

    def symbol_info(self, symbol):
        return MT5SymbolSpec(
            symbol=symbol,
            point=0.0001,
            volume_min=0.01,
            volume_max=10.0,
            volume_step=0.01,
        )

    def order_calc_profit(self, *args):
        return -100.0

    def order_calc_margin(self, *args):
        return 10.0

    def normalize_volume(self, spec, volume):
        return max(spec.volume_min, min(spec.volume_max, round(float(volume), 2)))


def _proposal(proposal_id: str, side: str, fingerprint: str) -> TradeProposal:
    now = datetime.now(timezone.utc)
    return TradeProposal(
        proposal_id=proposal_id,
        symbol="EURUSD",
        asset_class="forex",
        side=side,
        entry_price=1.1001,
        stop_loss=1.0991,
        take_profit=1.1021,
        volume_hint=0.0,
        strategy_name="FOREX_ENGINE",
        score=0.0,
        score_components={},
        created_at=now,
        expires_at=now + timedelta(seconds=30),
        context={
            "engine": "FOREX_ENGINE",
            "setup_fingerprint": fingerprint,
        },
    )


class SymbolBurstProtectionTests(unittest.TestCase):
    def _config(self, cooldown: int = 900):
        return SimpleNamespace(
            max_open_trades=30,
            max_pyramid_trades=1,
            dry_run=True,
            trade_mode="demo",
            capital_cap_usd=0.0,
            max_position_pct=1.0,
            max_xauusd_losses_per_day=0,
            symbol_reentry_cooldown_seconds=cooldown,
        )

    def test_opposite_direction_burst_is_blocked_for_fifteen_minutes(self):
        with tempfile.TemporaryDirectory() as directory:
            db = DatabaseLayer(Path(directory) / "platform.db")
            engine = ExecutionEngine(_Gateway(), self._config(), db)

            first = _proposal("first", "BUY", "setup-a")
            db.save_proposal(first)
            result = engine.submit(first)
            self.assertEqual(result.status, "DRY_RUN")

            second = _proposal("second", "SELL", "setup-b")
            db.save_proposal(second)
            result = engine.submit(second)
            self.assertEqual(result.status, "EXECUTION_BLOCKED")
            self.assertTrue(result.reason.startswith("SYMBOL_COOLDOWN_ACTIVE:"))

    def test_same_setup_is_suppressed_without_a_filled_burst(self):
        with tempfile.TemporaryDirectory() as directory:
            db = DatabaseLayer(Path(directory) / "platform.db")
            engine = ExecutionEngine(_Gateway(), self._config(cooldown=900), db)

            first = _proposal("first", "BUY", "same-setup")
            second = _proposal("second", "BUY", "same-setup")
            db.save_proposal(first)
            db.save_proposal(second)

            result = engine.submit(second)
            self.assertEqual(result.status, "EXECUTION_BLOCKED")
            self.assertEqual(result.reason, "SETUP_DUPLICATE_SUPPRESSED")

    def test_open_symbol_blocks_opposite_direction_entry(self):
        class OpenPositionGateway(_Gateway):
            def positions(self):
                return [SimpleNamespace(symbol="EURUSD", ticket=12345, direction="BUY")]

        with tempfile.TemporaryDirectory() as directory:
            db = DatabaseLayer(Path(directory) / "platform.db")
            engine = ExecutionEngine(OpenPositionGateway(), self._config(), db)
            proposal = _proposal("opposite-open", "SELL", "new-setup")
            db.save_proposal(proposal)
            result = engine.submit(proposal)
            self.assertEqual(result.status, "EXECUTION_BLOCKED")
            self.assertTrue(result.reason.startswith("SYMBOL_ALREADY_HAS_OPEN_POSITION:"))

    def test_different_symbol_is_not_blocked_by_eurusd_cooldown(self):
        with tempfile.TemporaryDirectory() as directory:
            db = DatabaseLayer(Path(directory) / "platform.db")
            first = _proposal("first", "BUY", "setup-a")
            db.save_proposal(first)
            db.save_execution_result(
                first,
                ExecutionResult(
                    proposal_id="first",
                    status="DRY_RUN",
                    symbol="EURUSD",
                    side="BUY",
                    volume=0.1,
                ),
            )
            self.assertIsNotNone(db.latest_filled_execution_at_for_symbol("EURUSD"))
            self.assertIsNone(db.latest_filled_execution_at_for_symbol("GBPUSD"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
