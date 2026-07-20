from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cipherfx_platform.contracts import TradeProposal
from cipherfx_platform.database import DatabaseLayer
from cipherfx_platform.engines import ForexLearningEngine, IndicesLearningEngine, MetalsLearningEngine
from cipherfx_platform.execution import ExecutionEngine
from mt5_xm_gateway import MT5SymbolSpec


class _Gateway:
    def __init__(self, positions=None):
        self._positions = list(positions or [])
        self.submitted = []

    def account_info(self):
        return SimpleNamespace(
            equity=10000.0,
            free_margin=9000.0,
            terminal_connected=True,
            trade_allowed=True,
            account_trade_allowed=True,
        )

    def positions(self):
        return list(self._positions)

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
        return 0.01


class QualityHardeningTests(unittest.TestCase):
    def test_each_engine_normalizes_total_score_to_zero_100(self):
        for engine_class in (ForexLearningEngine, IndicesLearningEngine, MetalsLearningEngine):
            engine = engine_class(None)
            self.assertEqual(engine._normalize_score(engine._score_scale, engine.FEATURE_MAX), 100.0)
            self.assertEqual(engine._normalize_score(0.0, 0.0), 0.0)
            self.assertLessEqual(engine._normalize_score(engine._score_scale * 2, engine.FEATURE_MAX * 2), 100.0)

    def test_each_engine_uses_configured_floor_of_at_least_65(self):
        with patch.dict(os.environ, {
            "MT5_MIN_SCORE_FOREX": "20",
            "MT5_MIN_SCORE_INDICES": "20",
            "MT5_MIN_SCORE_METALS": "20",
            "MT5_SCORE_FLOOR": "65",
        }, clear=False):
            self.assertEqual(ForexLearningEngine(None).base_threshold, 65.0)
            self.assertEqual(IndicesLearningEngine(None).base_threshold, 65.0)
            self.assertEqual(MetalsLearningEngine(None).base_threshold, 65.0)

    def test_engine_specific_participation_components_are_declared(self):
        # Forex dropped its tick-volume-proxy feature (unreliable for FX -
        # tick count, not real traded size), reducing its achievable max.
        self.assertEqual(ForexLearningEngine.FEATURE_MAX, 90.0)
        self.assertEqual(IndicesLearningEngine.FEATURE_MAX, 108.0)
        self.assertEqual(MetalsLearningEngine.FEATURE_MAX, 108.0)

    def test_loss_cooldown_is_persistent_and_symbol_engine_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            db = DatabaseLayer(Path(directory) / "platform.db")
            closed_at = datetime.now(timezone.utc) - timedelta(seconds=30)
            db.record_trade_history(
                "loss-1", None, "EURUSD", "forex", "BUY", -25.0, 25.0, -1.0,
                "STOP_LOSS", closed_at, {"outcome_type": "closed_trade"},
            )
            config = SimpleNamespace(
                max_daily_trades=1000,
                max_open_trades=30,
                loss_cooldown_seconds=600,
                dry_run=True,
                trade_mode="demo",
                canonical_symbol=lambda value: str(value).upper(),
            )
            now = datetime.now(timezone.utc)
            proposal = TradeProposal(
                "cooldown-check", "EURUSD", "forex", "BUY", 1.1001, 1.0, 1.3, 0.0,
                "FOREX", 70.0, {}, now, now + timedelta(seconds=30),
                {"engine": "FOREX"},
            )
            engine = ExecutionEngine(_Gateway(), config, db)
            ok, reason, _, _ = engine._preflight(proposal)
            self.assertFalse(ok)
            self.assertTrue(reason.startswith("LOSS_COOLDOWN_ACTIVE:"))

            other_engine = TradeProposal(
                "cooldown-other", "EURUSD", "index", "BUY", 1.1001, 1.0, 1.3, 0.0,
                "INDICES", 70.0, {}, now, now + timedelta(seconds=30),
                {"engine": "INDICES"},
            )
            self.assertEqual(engine._loss_cooldown_reason(other_engine), "")


if __name__ == "__main__":
    unittest.main()
