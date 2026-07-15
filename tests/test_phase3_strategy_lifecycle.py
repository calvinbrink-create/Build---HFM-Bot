from __future__ import annotations

import inspect
import unittest
from datetime import datetime, timedelta, timezone

from mt5_bot import XM_MT5_Bot
from strategies.architecture import Candle, Frame, Profile, SetupState, evaluate


START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _frame(timeframe: str, closes: list[float], wick: float = 0.2) -> Frame:
    candles = []
    for index, close in enumerate(closes):
        previous = closes[index - 1] if index else close
        open_price = previous + (close - previous) * 0.35
        high = max(open_price, close) + wick
        low = min(open_price, close) - wick
        minutes = {"H4": 240, "H1": 60, "M15": 15, "M5": 5, "M1": 1}[timeframe]
        timestamp = (START + timedelta(minutes=index * minutes)).isoformat()
        candles.append(Candle(timestamp, open_price, high, low, close, 100.0))
    return Frame(timeframe, tuple(candles))


def _range_frame(timeframe: str, count: int) -> Frame:
    return _frame(timeframe, [100.0 for _ in range(count)], 0.02)


def _scenario_frames(name: str) -> dict[str, Frame]:
    if name in {"range", "low_volatility"}:
        return {
            "H4": _range_frame("H4", 50),
            "H1": _range_frame("H1", 50),
            "M15": _range_frame("M15", 40),
            "M5": _range_frame("M5", 12),
            "M1": _range_frame("M1", 20),
        }

    step = 1.5 if name == "high_volatility" else 0.5
    htf = _frame("H4", [100.0 + index * step for index in range(50)], wick=step * 0.4)
    h1 = _frame("H1", [100.0 + index * step for index in range(50)], wick=step * 0.4)

    m15_closes = (
        [100.0 + index * step for index in range(28)]
        + [140.0, 141.0, 142.0, 143.0, 144.0, 145.0]
        + [136.0, 135.0, 134.0, 135.0, 136.0, 148.0]
    )
    m5_closes = [135.0 + index * (step / 2.0) for index in range(11)] + [145.0]
    return {
        "H4": htf,
        "H1": h1,
        "M15": _frame("M15", m15_closes, wick=max(0.2, step * 0.4)),
        "M5": _frame("M5", m5_closes, wick=max(0.2, step * 0.4)),
        "M1": _frame("M1", [100.0 + index * step for index in range(20)], wick=max(0.2, step * 0.4)),
    }


def _profile() -> Profile:
    return Profile(
        symbol="EURUSD",
        asset_class="forex",
        stop_atr=1.25,
        target_r=1.6,
        spread_limit_atr=0.35,
        risk_fraction=0.01,
        h4_min_score=70.0,
        h1_min_score=70.0,
        m15_min_score=70.0,
        m5_min_score=70.0,
        minimum_strategy_score=70.0,
    )


class Phase3StrategyLifecycleTests(unittest.TestCase):
    def test_lifecycle_identity_is_durable(self):
        bot = object.__new__(XM_MT5_Bot)
        signal = {
            "symbol": "EURUSD",
            "decision_trace_id": "dt_EURUSD_test",
            "generated_at": "2026-01-01T00:00:00+00:00",
        }
        result = bot._ensure_signal_lifecycle(signal)
        self.assertIs(result, signal)
        self.assertEqual(signal["signal_id"], "dt_EURUSD_test")
        self.assertEqual(signal["signal_created_at"], "2026-01-01T00:00:00+00:00")
        self.assertEqual(signal["signal_timeframe_source"], "H4,H1,M15,M5")
        self.assertEqual(signal["signal_lifecycle_state"], "CANDIDATE")

    def test_not_qualified_keeps_dashboard_event_and_adds_lifecycle_event(self):
        source = inspect.getsource(XM_MT5_Bot._mark_not_qualified)
        self.assertIn('"event": "NOT_QUALIFIED"', source)
        self.assertIn('"event": "SIGNAL_LIFECYCLE_TERMINAL"', source)

    def test_replay_scenarios_are_deterministic(self):
        profile = _profile()
        for scenario in ("trending", "range", "high_volatility", "low_volatility"):
            with self.subTest(scenario=scenario):
                frames = _scenario_frames(scenario)
                first = evaluate("EURUSD", profile, frames)
                second = evaluate("EURUSD", profile, frames)
                self.assertEqual(first.state, second.state)
                self.assertEqual(first.reason, second.reason)
                self.assertEqual(first.score, second.score)
                self.assertEqual(first.evidence.get("higher_timeframes"), second.evidence.get("higher_timeframes"))
                if first.state == "PASS":
                    self.assertEqual(first.evidence["setup_id"], second.evidence["setup_id"])

    def test_flat_h4_cannot_create_context(self):
        profile = _profile()
        frames = _scenario_frames("range")
        decision = evaluate("EURUSD", profile, frames)
        self.assertNotEqual(decision.state, "PASS")
        self.assertIn(decision.reason, {"H4_NO_DIRECTION", "H4_SCORE_BELOW_MINIMUM"})

    def test_pass_decision_contains_structure_and_trigger_evidence(self):
        profile = _profile()
        decision = evaluate("EURUSD", profile, _scenario_frames("trending"))
        self.assertIn("higher_timeframes", decision.evidence)
        self.assertIn("m5_trigger", decision.evidence)
        self.assertEqual(decision.evidence["m1"]["status"], "NOT_REQUIRED")
        self.assertIn("score_metric", decision.evidence)
        self.assertTrue(decision.evidence["score_metric"]["enforced"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
