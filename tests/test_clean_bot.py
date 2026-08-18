from __future__ import annotations

import ast
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from cipherfx_platform import setup_engine
from cipherfx_platform.contracts import Candle, Frame, MarketSnapshot, Tick
from cipherfx_platform.engines.router import LearningEngine
from cipherfx_platform.market_data import MarketDataEngine


def _frame(name: str, side: str = "BUY") -> Frame:
    now = datetime.now(timezone.utc)
    candles = []
    price = 100.0
    for index in range(40):
        if side == "BUY":
            price += 0.2
        else:
            price -= 0.2
        candles.append(
            Candle(
                now - timedelta(minutes=index + 10),
                price - 0.1,
                price + 0.2,
                price - 0.2,
                price,
                1.0,
            )
        )
    candles.reverse()
    return Frame(name, tuple(candles))


def _snapshot() -> MarketSnapshot:
    now = datetime.now(timezone.utc)
    return MarketSnapshot(
        symbol="EURUSD",
        asset_class="forex",
        frames={name: _frame(name) for name in ("H4", "M5")},
        tick=Tick("EURUSD", 100.0, 100.1, now, now),
        captured_at=now,
        freshness={"source": "websocket"},
    )


class CleanRouteTests(unittest.TestCase):
    def test_active_route_uses_h4_and_m5_only(self):
        self.assertEqual(set(setup_engine.FRAME_ORDER), {"H4", "M5"})

    def test_h4_owns_direction_even_when_memory_disagrees(self):
        def fake_action(_frame, side):
            return {
                "structure": side,
                "swings": {"highs": [{"price": 101.0}], "lows": [{"price": 99.0}]},
                "prior_high": 101.0,
                "prior_low": 99.0,
                "bos": side,
                "liquidity_sweep": side,
                "support_resistance_flip": True,
                "fvg": {"side": side, "low": 99.5, "high": 100.0, "in_zone": True},
                "order_block": {"side": side, "low": 99.5, "high": 100.0, "in_zone": True},
                "range_position": 0.5,
                "retracement": True,
                "candle_side": side,
                "ema20": 100.0,
                "rsi": 50.0,
                "bar_time": datetime.now(timezone.utc).isoformat(),
            }

        with patch.object(setup_engine, "structure", return_value="SELL"),              patch.object(setup_engine, "_price_action", side_effect=fake_action),              patch.object(setup_engine, "_zone_tapped", return_value=True),              patch.object(
                 setup_engine,
                 "_memory",
                 return_value={
                     "available": True,
                     "role": "EVIDENCE_ONLY",
                     "support": "BUY",
                     "matches_direction": False,
                 },
             ):
            result = setup_engine.build_setup(_snapshot())

        self.assertEqual(result["side"], "SELL")
        self.assertEqual(result["h4_direction"], "SELL")
        self.assertEqual(result["memory"]["support"], "BUY")
        self.assertTrue(result["valid"])

    def test_m15_cannot_veto_h4_poi_and_m5_trigger(self):
        base = _snapshot()
        frames = dict(base.frames)
        frames["M15"] = _frame("M15")
        snapshot = MarketSnapshot(
            base.symbol,
            base.asset_class,
            frames,
            base.tick,
            base.captured_at,
            base.freshness,
        )

        def fake_action(_frame, side):
            return {
                "structure": side,
                "swings": {"highs": [{"price": 101.0}], "lows": [{"price": 99.0}]},
                "prior_high": 101.0,
                "prior_low": 99.0,
                "bos": side,
                "liquidity_sweep": side,
                "support_resistance_flip": True,
                "fvg": {"side": side, "low": 99.5, "high": 100.0, "in_zone": True},
                "order_block": {"side": side, "low": 99.5, "high": 100.0, "in_zone": True},
                "candle_side": side,
                "bar_time": datetime.now(timezone.utc).isoformat(),
            }

        with patch.object(setup_engine, "structure", return_value="BUY"),              patch.object(setup_engine, "_price_action", side_effect=fake_action),              patch.object(setup_engine, "_zone_tapped", return_value=True):
            result = setup_engine.build_setup(snapshot)

        self.assertTrue(result["valid"])
        self.assertEqual(set(result["frames"]), {"H4", "M5"})
        self.assertNotIn("M15", result["frames"])

    def test_learning_proposal_has_no_score_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            from cipherfx_platform.database import DatabaseLayer
            engine = LearningEngine(DatabaseLayer(Path(directory) / "test.db"))
            setup = {
                "valid": True,
                "side": "BUY",
                "setup_type": "H4_POI_5M_SWEEP_MSS_FVG_OB",
                "entry": 100.0,
                "stop_loss": 99.0,
                "take_profit": 101.5,
                "reasons": ("H4_DIRECTION_BUY",),
                "frames": {"M5": {"bar_time": "2026-01-01T00:00:00+00:00"}},
            }
            with patch("cipherfx_platform.engines.router.build_setup", return_value=setup):
                proposal = engine.propose(_snapshot())
        self.assertIsNotNone(proposal)
        self.assertEqual(proposal.score, 0.0)
        self.assertEqual(proposal.score_components, {})
        self.assertEqual(proposal.context["score_role"], "NOT_USED")
        self.assertEqual(proposal.context["memory_role"], "EVIDENCE_ONLY")

    def test_closed_outcome_updates_memory_without_changing_direction(self):
        import numpy as np
        import cipherfx_platform.pattern_memory as memory_module
        from cipherfx_platform.pattern_memory import PatternMemory
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memory.npz"
            np.savez_compressed(
                path,
                shapes=np.zeros((1, 12), dtype=np.float32),
                buy_r=np.zeros((1,), dtype=np.float32),
                sell_r=np.zeros((1,), dtype=np.float32),
            )
            with patch.object(memory_module, "DEFAULT_PATH", str(path)):
                shape = [float(index) for index in range(12)]
                observed = PatternMemory.load().observe(shape, "BUY", 1.25)
                current = PatternMemory.load()
                self.assertTrue(observed)
                self.assertEqual(current.size, 2)
                self.assertAlmostEqual(float(current.buy_r[-1]), 1.25, places=5)
                self.assertTrue(np.isnan(current.sell_r[-1]))

    def test_active_route_has_one_engine_and_one_order_call(self):
        init_source = Path("/opt/cipherfx_mt5/cipherfx_platform/engines/__init__.py").read_text()
        router_source = Path("/opt/cipherfx_mt5/cipherfx_platform/engines/router.py").read_text()
        execution_source = Path("/opt/cipherfx_mt5/cipherfx_platform/execution.py").read_text()
        runtime_source = Path("/opt/cipherfx_mt5/cipherfx_platform/runtime.py").read_text()
        self.assertIn("from .router import LearningEngine", init_source)
        self.assertIn("from ..strategy_dispatch import build_setup", router_source)
        self.assertIn("FOREX_ENGINE", router_source)
        self.assertIn("INDICES_ENGINE", router_source)
        self.assertIn("METALS_ENGINE", router_source)
        dispatch_source = Path("/opt/cipherfx_mt5/cipherfx_platform/strategy_dispatch.py").read_text()
        self.assertIn("build_forex_setup", dispatch_source)
        self.assertIn("build_indices_setup", dispatch_source)
        self.assertIn("build_metals_setup", dispatch_source)
        self.assertEqual(execution_source.count("self.gateway.place_market_order("), 1)
        self.assertNotIn("_fire_pyramid_burst", execution_source)
        self.assertNotIn("PremarketPlanningEngine", runtime_source)
        ast.parse(runtime_source)

    def test_filled_result_has_fill_timestamp_for_learning_reconciliation(self):
        from types import SimpleNamespace
        from cipherfx_platform.execution import ExecutionEngine
        from cipherfx_platform.database import DatabaseLayer
        from cipherfx_platform.contracts import TradeProposal
        with tempfile.TemporaryDirectory() as directory:
            class Gateway:
                def account_info(self):
                    return SimpleNamespace(terminal_connected=True, trade_allowed=True, account_trade_allowed=True, free_margin=10000, leverage=100)
                def positions(self):
                    return []
                def symbol_tick(self, symbol):
                    return symbol, SimpleNamespace(ask=100.0, bid=99.9)
                def symbol_info(self, symbol):
                    return SimpleNamespace(contract_size=1.0, volume_min=0.01, volume_max=100.0, volume_step=0.01)
                def normalize_volume(self, spec, volume):
                    return max(spec.volume_min, min(spec.volume_max, round(volume / spec.volume_step) * spec.volume_step))
                def place_market_order(self, *args):
                    return args[0], 100.0, SimpleNamespace(retcode=10009, order=12, deal=13, position=14, comment="done")
            db = DatabaseLayer(Path(directory) / "test.db")
            config = SimpleNamespace(max_open_trades=30, dry_run=False, trade_mode="live")
            proposal = TradeProposal("filled", "EURUSD", "forex", "BUY", 100.0, 99.0, 101.5, 0.0, "H4_M15_M5_SETUP", 0.0, {}, datetime.now(timezone.utc), datetime.now(timezone.utc) + timedelta(seconds=10), context={})
            result = ExecutionEngine(Gateway(), config, db).submit(proposal)
            self.assertEqual(result.status, "FILLED")
            self.assertIsNotNone(result.filled_at)
            with db._connect() as conn:
                self.assertIsNotNone(conn.execute("SELECT filled_at FROM executions WHERE proposal_id='filled'").fetchone()[0])

    def test_market_data_requests_only_active_frames(self):
        class FakeGateway:
            def __init__(self):
                self.calls = []

            def ensure_symbol(self, symbol):
                return symbol

            def rates(self, symbol, timeframe, count):
                self.calls.append(timeframe)
                now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
                minutes = {"H4": 240, "M5": 5}[timeframe]
                index = [
                    now - timedelta(minutes=minutes * (40 - n + 2))
                    for n in range(40)
                ]
                return pd.DataFrame(
                    {
                        "Open": [100.0 + n * 0.01 for n in range(40)],
                        "High": [100.2 + n * 0.01 for n in range(40)],
                        "Low": [99.8 + n * 0.01 for n in range(40)],
                        "Close": [100.1 + n * 0.01 for n in range(40)],
                        "Volume": [1.0] * 40,
                    },
                    index=pd.DatetimeIndex(index),
                )

        gateway = FakeGateway()
        market = MarketDataEngine(gateway, history_bars=40)
        now = datetime.now(timezone.utc)
        market.ingest_tick(
            {
                "type": "tick",
                "symbol": "EURUSD",
                "time_utc": now.isoformat(),
                "bid": 100.0,
                "ask": 100.1,
            }
        )
        snapshot = market.snapshot("EURUSD", "forex")
        self.assertEqual(set(snapshot.frames), {"H4", "M5"})
        self.assertEqual(gateway.calls, ["H4", "M5"])
        self.assertEqual(snapshot.freshness["source"], "websocket")

    def test_near_entry_swing_uses_valid_m15_atr_stop(self):
        def fake_action(_frame, side):
            return {
                "structure": side,
                "swings": {"highs": [{"price": 101.0}], "lows": [{"price": 100.0}]},
                "prior_high": 101.0,
                "prior_low": 99.0,
                "bos": side,
                "liquidity_sweep": side,
                "support_resistance_flip": True,
                "fvg": {"side": side, "low": 99.5, "high": 100.0, "in_zone": True},
                "order_block": {"side": side, "low": 99.5, "high": 100.0, "in_zone": True},
                "range_position": 0.5,
                "retracement": True,
                "candle_side": side,
                "ema20": 100.0,
                "rsi": 50.0,
                "bar_time": datetime.now(timezone.utc).isoformat(),
            }

        with patch.object(setup_engine, "structure", return_value="BUY"),              patch.object(setup_engine, "_price_action", side_effect=fake_action),              patch.object(setup_engine, "_zone_tapped", return_value=True),              patch.object(setup_engine, "atr", return_value=0.4):
            result = setup_engine.build_setup(_snapshot())

        self.assertTrue(result["valid"])
        self.assertGreater(result["stop_distance"], 0.0)

    def test_asset_specific_strategy_dispatch_is_explicit(self):
        dispatch = Path("/opt/cipherfx_mt5/cipherfx_platform/strategy_dispatch.py").read_text()
        metals = Path("/opt/cipherfx_mt5/cipherfx_platform/engines/metals.py").read_text()
        indices = Path("/opt/cipherfx_mt5/cipherfx_platform/engines/indices.py").read_text()
        self.assertIn('snapshot.asset_class == "metal"', dispatch)
        self.assertIn('snapshot.asset_class == "index"', dispatch)
        self.assertIn("METALS_H4_POI_5M_SWEEP_MSS_FVG_OB", metals)
        self.assertIn("INDICES_H4_POI_5M_SWEEP_MSS_FVG_OB", indices)
        self.assertIn("M5_FVG_OR_OB_RETEST_IMMEDIATE", metals)
        self.assertNotIn("liquidity_sweep", indices)

    def test_websocket_tick_freshness_matches_bridge_contract(self):
        class Gateway:
            def ensure_symbol(self, symbol):
                return symbol
        market = MarketDataEngine(Gateway())
        self.assertEqual(market.websocket_max_tick_age_seconds, 60.0)


if __name__ == "__main__":
    unittest.main()
