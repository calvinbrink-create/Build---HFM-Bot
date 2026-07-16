from __future__ import annotations

import inspect
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from cipherfx_platform.contracts import Candle, Frame, MarketSnapshot, TradeProposal
from cipherfx_platform.database import DatabaseLayer
from cipherfx_platform.engines import (
    ForexLearningEngine,
    IndicesLearningEngine,
    MetalsLearningEngine,
    MarketIntelligenceEngine,
)
from cipherfx_platform.market_data import MarketDataEngine


class SnapshotGateway:
    def rates(self, symbol, timeframe, count):
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        period = {"MN1": 43200, "W1": 10080, "D1": 1440, "H4": 240, "H1": 60,
                  "M30": 30, "M15": 15, "M5": 5, "M3": 3, "M1": 1}[timeframe]
        rows = []
        for i in range(220):
            close = 100.0 + i * 0.02
            rows.append({
                "time": now - timedelta(minutes=period * (219 - i)),
                "Open": close - 0.01,
                "High": close + 0.03,
                "Low": close - 0.02,
                "Close": close,
                "Volume": 1000.0,
            })
        return pd.DataFrame(rows).set_index("time")
    def symbol_tick(self, symbol):
        return symbol, SimpleNamespace(bid=104.39, ask=104.41, time_utc=datetime.now(timezone.utc).timestamp())


class Phase2MarketIntelligenceTests(unittest.TestCase):
    def test_snapshot_contains_all_ten_timeframes(self):
        snapshot = MarketDataEngine(SnapshotGateway(), history_bars=220).snapshot("EURUSD", "forex")
        expected = {"MN1", "W1", "D1", "H4", "H1", "M30", "M15", "M5", "M3", "M1"}
        self.assertEqual(set(snapshot.frames), expected)
        self.assertEqual(snapshot.freshness["timeframes"], sorted(expected, key=lambda x: [
            "MN1", "W1", "D1", "H4", "H1", "M30", "M15", "M5", "M3", "M1"
        ].index(x)))

    def test_engines_have_independent_weights_and_thresholds(self):
        self.assertNotEqual(ForexLearningEngine.TIME_WEIGHTS, IndicesLearningEngine.TIME_WEIGHTS)
        self.assertNotEqual(IndicesLearningEngine.TIME_WEIGHTS, MetalsLearningEngine.TIME_WEIGHTS)
        self.assertNotEqual(ForexLearningEngine.THRESHOLD, IndicesLearningEngine.THRESHOLD)
        self.assertNotEqual(IndicesLearningEngine.THRESHOLD, MetalsLearningEngine.THRESHOLD)
        for engine in (ForexLearningEngine, IndicesLearningEngine, MetalsLearningEngine):
            self.assertEqual(set(engine.TIME_WEIGHTS), {"MN1", "W1", "D1", "H4", "H1", "M30", "M15", "M5", "M3", "M1"})

    def test_no_shared_scoring_class_is_active(self):
        from cipherfx_platform import runtime
        source = inspect.getsource(runtime)
        self.assertNotIn("ScoringEngine", source)
        self.assertNotIn("LearningEngine", source)
        for engine in (ForexLearningEngine, IndicesLearningEngine, MetalsLearningEngine):
            source = inspect.getsource(engine)
            self.assertNotIn("ScoringEngine", source)
            self.assertIn("TIME_WEIGHTS", source)

    def test_each_engine_produces_its_own_report(self):
        snapshot = MarketDataEngine(SnapshotGateway(), history_bars=220).snapshot("EURUSD", "forex")
        with tempfile.TemporaryDirectory() as tmp:
            db = DatabaseLayer(Path(tmp) / "platform.db")
            router = MarketIntelligenceEngine(db)
            proposal = router.propose(snapshot)
            self.assertEqual(router.last_report["engine"], "FOREX")
            self.assertEqual(set(router.last_report["scores"]), {"BUY", "SELL"})
            self.assertEqual(set(router.last_report["scores"]["BUY"]), set(ForexLearningEngine.TIME_WEIGHTS))
            self.assertTrue(proposal is None or isinstance(proposal, TradeProposal))
            if proposal:
                self.assertTrue(proposal.confidence > 0)
                self.assertTrue(proposal.probability > 0)
                self.assertTrue(proposal.proposal_id)
                self.assertIsInstance(proposal.reasoning, tuple)

    def test_proposal_contract_is_immutable_and_has_required_fields(self):
        now = datetime.now(timezone.utc)
        proposal = TradeProposal(
            "id", "XAUUSD", "metal", "BUY", 100, 98, 104, 0,
            "METALS", 75, {"M5": 80}, now, now + timedelta(seconds=20),
            {"engine": "METALS"}, 75, 68, ("IMPULSE",), 2,
        )
        with self.assertRaises(Exception):
            proposal.side = "SELL"
        self.assertEqual(proposal.confidence, 75)
        self.assertEqual(proposal.probability, 68)
        self.assertEqual(proposal.risk_amount, 2)


if __name__ == "__main__":
    unittest.main()
