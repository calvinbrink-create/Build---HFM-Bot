import tempfile
import unittest
from pathlib import Path

from visual_market_intelligence import VisualMarketIntelligence, causal_frame_features, fetch_visual_summary
from visual_market_intelligence.labels import triple_barrier_label
from visual_market_intelligence.leakage import assert_causal_rows, assert_split_gap

def bars(count=40):
    return [
        {
            "timestamp": f"2026-07-11T12:{index:02d}:00+00:00",
            "open": 100 + index * .1,
            "high": 100 + index * .1 + .3,
            "low": 100 + index * .1 - .1,
            "close": 100 + index * .1 + .2,
            "volume": 100 + index,
        }
        for index in range(count)
    ]

class VisualIntelligenceTests(unittest.TestCase):
    def test_features_are_causal_and_available(self):
        rows = bars()
        features = causal_frame_features(rows, "M5")
        self.assertTrue(features["available"])
        self.assertEqual(features["last_completed_at"], rows[-1]["timestamp"])

    def test_shadow_prediction_persists_without_execution(self):
        with tempfile.TemporaryDirectory() as folder:
            observer = VisualMarketIntelligence(str(Path(folder) / "state.db"))
            result = observer.evaluate(
                symbol="TEST", timestamp=bars()[-1]["timestamp"],
                h1_context=bars(), m15_context=bars(),
                m5_context=bars(), m1_context=bars(),
            )
            self.assertEqual(result["mode"], "SHADOW_ONLY")
            summary = fetch_visual_summary(str(Path(folder) / "state.db"))
            self.assertEqual(summary["prediction_count"], 1)
            self.assertFalse(summary["verified_profitability"])

    def test_missing_context_abstains(self):
        with tempfile.TemporaryDirectory() as folder:
            observer = VisualMarketIntelligence(str(Path(folder) / "state.db"))
            result = observer.evaluate(
                symbol="TEST", timestamp="2026-07-11T12:00:00+00:00",
                h1_context=[], m15_context=bars(), m5_context=bars(), m1_context=bars(),
            )
            self.assertTrue(result["abstain"])
            self.assertTrue(any("DATA_MISSING_H1" in reason for reason in result["reasons"]))

    def test_triple_barrier_and_split_hygiene(self):
        label = triple_barrier_label(
            bars(6), direction="BUY", entry_price=100, initial_risk=1,
            take_profit_r=1, spread=.01,
        )
        self.assertGreater(label.future_bars_used, 0)
        assert_causal_rows([{
            "feature_time": "2026-07-11T12:00:00+00:00",
            "label_time": "2026-07-11T12:05:00+00:00",
        }])
        assert_split_gap(
            "2026-07-11T12:00:00+00:00",
            "2026-07-11T12:05:00+00:00",
            60,
        )

if __name__ == "__main__":
    unittest.main()
