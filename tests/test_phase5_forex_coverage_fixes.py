from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest

from cipherfx_platform.contracts import Candle, Frame, TradeProposal
from cipherfx_platform.database import DatabaseLayer
from cipherfx_platform.execution import ExecutionEngine
from cipherfx_platform.market_data import MarketDataEngine, MarketDataError
from mt5_xm_gateway import MT5SymbolSpec


class Gateway:
    def __init__(self):
        self.spec = MT5SymbolSpec(
            symbol="EURUSD", visible=True, digits=5, point=0.00001,
            volume_min=0.01, volume_max=10.0, volume_step=0.01,
        )

    def account_info(self):
        return SimpleNamespace(
            equity=10000.0, free_margin=9000.0,
            terminal_connected=True, trade_allowed=True,
            account_trade_allowed=True,
        )

    def positions(self):
        return []

    def symbol_tick(self, symbol):
        return symbol, SimpleNamespace(bid=1.1000, ask=1.1001)

    def symbol_info(self, symbol):
        return self.spec

    def order_calc_profit(self, symbol, side, volume, entry, close):
        return (close - entry) * 100000.0 * volume * (1 if side == "BUY" else -1)

    def order_calc_margin(self, symbol, side, volume, entry):
        return 100.0 * volume

    def normalize_volume(self, spec, volume):
        return round(min(spec.volume_max, max(spec.volume_min, volume)), 2)


def make_proposal(proposal_id="retry-me"):
    now = datetime.now(timezone.utc)
    return TradeProposal(
        proposal_id=proposal_id,
        symbol="EURUSD",
        asset_class="forex",
        side="BUY",
        entry_price=1.1001,
        stop_loss=1.0990,
        take_profit=1.1018,
        volume_hint=0.0,
        strategy_name="test",
        score=85.0,
        score_components={"m5_trigger": 25.0},
        created_at=now,
        expires_at=now + timedelta(seconds=30),
    )


class Phase5ForexCoverageFixTests(unittest.TestCase):
    def test_daily_count_ignores_non_trade_outcomes(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = DatabaseLayer(Path(tmp) / "platform.db")
            for status in ("EXECUTION_BLOCKED", "REJECTED", "DUPLICATE_SUPPRESSED"):
                db.execution(status, "EURUSD", "BUY", status, 0.0)
            for status in ("FILLED", "DRY_RUN", "CLOSED"):
                db.execution(status, "EURUSD", "BUY", status, 0.01)
            self.assertEqual(db.count_today(), 3)

    def test_blocked_attempt_does_not_poison_same_proposal(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = DatabaseLayer(Path(tmp) / "platform.db")
            db.execution("retry-me", "EURUSD", "BUY", "EXECUTION_BLOCKED", 0.0)
            config = SimpleNamespace(
                max_daily_trades=1,
                max_open_trades=30,
                dry_run=True,
                trade_mode="paper",
            )
            result = ExecutionEngine(Gateway(), config, db).submit(make_proposal())
            self.assertEqual(result.status, "DRY_RUN")

    def test_market_snapshot_uses_websocket_only(self):
        class SnapshotGateway:
            def __init__(self):
                self.config = SimpleNamespace(resolved_symbol=lambda value: value)

            def ensure_symbol(self, symbol):
                return symbol

            def symbol_tick(self, symbol):
                raise AssertionError("snapshot must not use bridge tick files")

        gateway = SnapshotGateway()
        market_data = MarketDataEngine(gateway)
        candle_time = datetime.now(timezone.utc) - timedelta(minutes=5)
        market_data._frame = lambda symbol, timeframe: Frame(
            timeframe,
            (Candle(candle_time, 1.0, 1.1, 0.9, 1.05, 10.0),),
        )
        market_data.ingest_tick({
            "type": "tick",
            "symbol": "EURUSD",
            "time_utc": time.time() - 1.0,
            "bid": 1.1000,
            "ask": 1.1001,
        })
        snapshot = market_data.snapshot("EURUSD", "forex")
        self.assertEqual(snapshot.freshness["source"], "websocket")
        self.assertLess(snapshot.freshness["tick_age_seconds"], 10.0)

    def test_market_snapshot_rejects_missing_or_stale_websocket_tick(self):
        class SnapshotGateway:
            def __init__(self):
                self.config = SimpleNamespace(resolved_symbol=lambda value: value)

            def ensure_symbol(self, symbol):
                return symbol

        gateway = SnapshotGateway()
        market_data = MarketDataEngine(gateway)
        with self.assertRaisesRegex(MarketDataError, "websocket tick unavailable"):
            market_data.snapshot("EURUSD", "forex")
        market_data.ingest_tick({
            "type": "tick",
            "symbol": "EURUSD",
            "time_utc": time.time() - 11.0,
            "bid": 1.1000,
            "ask": 1.1001,
        })
        with self.assertRaisesRegex(MarketDataError, "websocket tick stale"):
            market_data.snapshot("EURUSD", "forex")


if __name__ == "__main__":
    unittest.main()
