from __future__ import annotations

import inspect
import re
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from cipherfx_platform.contracts import Candle, Frame, MarketSnapshot, Tick
from cipherfx_platform.engines.forex import ForexLearningEngine
from cipherfx_platform.engines.indices import IndicesLearningEngine
from cipherfx_platform.engines.metals import MetalsLearningEngine
from cipherfx_platform.setup_intelligence import FRAME_ORDER, build_market_setup, rsi, structure_events

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def rising_frame(name: str, step_minutes: int, n: int = 80) -> Frame:
    candles = []
    for index in range(n):
        cycle = index % 6
        anchor = 100.0 + index * 0.05
        if cycle == 0:
            open_, close, high, low = anchor, anchor + 0.10, anchor + 0.20, anchor - 0.05
        elif cycle == 1:
            open_, close, high, low = anchor + 0.10, anchor + 0.02, anchor + 0.12, anchor - 0.02
        elif cycle == 2:
            open_, close, high, low = anchor + 0.02, anchor - 0.05, anchor + 0.06, anchor - 0.15
        elif cycle == 3:
            open_, close, high, low = anchor - 0.05, anchor + 0.02, anchor + 0.08, anchor - 0.12
        elif cycle == 4:
            open_, close, high, low = anchor + 0.02, anchor + 0.15, anchor + 0.30, anchor - 0.02
        else:
            open_, close, high, low = anchor + 0.15, anchor + 0.30, anchor + 0.40, anchor + 0.10
        candles.append(Candle(BASE + timedelta(minutes=step_minutes * index), open_, high, low, close, 1000.0))
    return Frame(name, tuple(candles))


def snapshot() -> MarketSnapshot:
    steps = {"MN1": 43200, "W1": 10080, "D1": 1440, "H4": 240, "H1": 60,
             "M30": 30, "M15": 15, "M5": 5, "M3": 3, "M1": 1}
    frames = {name: rising_frame(name, step) for name, step in steps.items()}
    prior = frames["M5"].candles[:-1]
    last = prior[-1]
    trigger = Candle(BASE + timedelta(minutes=5 * 79), last.close, last.close + 1.0,
                     last.low, last.close + 0.8, 1000.0)
    frames["M5"] = Frame("M5", prior + (trigger,))
    tick_time = BASE + timedelta(days=1)
    return MarketSnapshot("EURUSD", "forex", frames,
                          Tick("EURUSD", 105.75, 105.85, tick_time, tick_time), tick_time)


class SetupIntelligenceTests(unittest.TestCase):
    def test_rsi_is_deterministic_and_directional(self):
        rising = Frame("M1", tuple(Candle(BASE + timedelta(minutes=i), 1 + i, 2 + i,
                                           1 + i, 2 + i, 1) for i in range(20)))
        falling = Frame("M1", tuple(Candle(BASE + timedelta(minutes=i), 20 - i, 20 - i,
                                           19 - i, 19 - i, 1) for i in range(20)))
        self.assertEqual(rsi(rising), 100.0)
        self.assertEqual(rsi(falling), 0.0)

    def test_confirmed_structure_is_causal_and_repeatable(self):
        frame = rising_frame("H4", 240)
        first = structure_events(frame)
        self.assertEqual(first, structure_events(frame))
        self.assertEqual(first["structure"], "BUY")
        self.assertGreaterEqual(len(first["swings"]["highs"]), 2)
        self.assertGreaterEqual(len(first["swings"]["lows"]), 2)

    def test_setup_contains_exactly_one_ten_frame_snapshot(self):
        setup = build_market_setup(snapshot(), engine="FOREX")
        self.assertEqual(tuple(setup["frames"]), FRAME_ORDER)
        self.assertEqual(set(setup["frames"]), set(FRAME_ORDER))
        self.assertEqual(setup["h4_direction"], "BUY")
        self.assertIn("M15_SETUP_PASS", setup["reasons"])
        self.assertEqual(setup["indicator_role"], "SETUP_EVIDENCE_NOT_SCORE_VETO")

    def test_memory_is_evidence_and_cannot_reverse_h4_direction(self):
        with patch("cipherfx_platform.setup_intelligence._memory_evidence",
                   return_value={"available": True, "support": "SELL",
                                 "matches_direction": False, "role": "EVIDENCE_ONLY"}):
            setup = build_market_setup(snapshot(), engine="FOREX")
        self.assertEqual(setup["side"], "BUY")
        self.assertEqual(setup["memory"]["support"], "SELL")
        self.assertEqual(setup["memory"]["role"], "EVIDENCE_ONLY")

    def test_primary_engines_do_not_use_diagnostic_score_as_setup_veto(self):
        for engine_class in (ForexLearningEngine, IndicesLearningEngine, MetalsLearningEngine):
            source = inspect.getsource(engine_class.propose)
            self.assertIn('if not setup["valid"]', source)
            self.assertNotRegex(source, re.compile(r"if\s+raw\s*<\s*threshold"))

    def test_each_primary_engine_has_distinct_diagnostic_weights(self):
        self.assertNotEqual(ForexLearningEngine.TIME_WEIGHTS, IndicesLearningEngine.TIME_WEIGHTS)
        self.assertNotEqual(IndicesLearningEngine.TIME_WEIGHTS, MetalsLearningEngine.TIME_WEIGHTS)
        self.assertEqual(set(ForexLearningEngine.TIME_WEIGHTS), {"H4", "H1", "M15", "M5", "M1"})
        self.assertEqual(set(IndicesLearningEngine.TIME_WEIGHTS), {"H4", "H1", "M15", "M5", "M1"})
        self.assertEqual(set(MetalsLearningEngine.TIME_WEIGHTS), {"H4", "H1", "M15", "M5", "M1"})


if __name__ == "__main__":
    unittest.main()
