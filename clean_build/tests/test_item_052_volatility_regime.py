from datetime import datetime, timedelta, timezone

import pytest

from cipherfx_clean.contracts import Candle
from cipherfx_clean.intelligence.volatility_regime import (
    rolling_realized_volatility,
    volatility_regime_probability,
)


START = datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc)


def candles(returns, *, ranges=None):
    closes = [100.0]
    for value in returns:
        closes.append(closes[-1] * (1.0 + value))
    ranges = ranges or tuple(1.0 for _ in closes)
    return tuple(
        Candle(
            symbol="USA500",
            timeframe="M5",
            start=START + timedelta(minutes=5 * index),
            end=START + timedelta(minutes=5 * (index + 1)),
            open=close,
            high=close + candle_range / 2,
            low=close - candle_range / 2,
            close=close,
            source="TEST_VOLATILITY_REGIME",
            source_id=f"bar:{index}",
        )
        for index, (close, candle_range) in enumerate(zip(closes, ranges))
    )


def probability_map(result):
    return dict(result.probabilities)


def test_probabilities_are_bounded_and_sum_to_one():
    returns = tuple((0.001 if index % 2 else -0.0015) for index in range(120))
    result = volatility_regime_probability(candles(returns), symbol="USA500", timeframe="M5")
    probabilities = probability_map(result)
    assert result.status == "OBSERVED"
    assert set(probabilities) == {"LOW", "NORMAL", "HIGH", "EXTREME"}
    assert sum(probabilities.values()) == pytest.approx(1.0)
    assert all(0.0 <= value <= 1.0 for value in probabilities.values())
    assert result.dominant_regime == max(probabilities, key=probabilities.get)


def test_current_volatility_burst_has_extreme_probability_dominance():
    baseline = tuple((0.0005 if index % 2 else -0.0005) for index in range(100))
    burst = (0.01, -0.012, 0.014, -0.011, 0.013, -0.012, 0.015, -0.01, 0.012, -0.014,
             0.011, -0.013, 0.014, -0.012, 0.013, -0.011, 0.015, -0.012, 0.014, -0.013)
    result = volatility_regime_probability(
        candles((*baseline, *burst)), symbol="XAUUSD", timeframe="M5"
    )
    assert result.dominant_regime == "EXTREME"


def test_current_quiet_window_has_low_probability_dominance():
    baseline = tuple((0.003 if index % 2 else -0.003) for index in range(100))
    quiet = tuple((0.00005 if index % 2 else -0.00005) for index in range(20))
    result = volatility_regime_probability(
        candles((*baseline, *quiet)), symbol="UK100", timeframe="M5"
    )
    assert result.dominant_regime == "LOW"


def test_volatility_regime_is_independent_of_atr_and_candle_ranges():
    returns = tuple((0.001 if index % 2 else -0.001) for index in range(80))
    narrow = candles(returns, ranges=tuple(1.0 for _ in range(81)))
    wide = candles(returns, ranges=tuple(50.0 for _ in range(81)))
    first = volatility_regime_probability(narrow, symbol="USA100", timeframe="M5")
    second = volatility_regime_probability(wide, symbol="USA100", timeframe="M5")
    assert first == second


def test_rolling_realized_volatility_uses_only_close_to_close_returns():
    returns = (0.01, -0.02, 0.03, -0.01)
    values = rolling_realized_volatility(candles(returns), lookback=3)
    assert len(values) == 3
    assert all(value >= 0.0 for value in values)


def test_short_history_is_explicitly_unavailable_not_a_hard_label():
    result = volatility_regime_probability(
        candles((0.001, -0.001, 0.001)),
        symbol="USA30",
        timeframe="M5",
    )
    assert result.status == "INSUFFICIENT_HISTORY"
    assert probability_map(result) == {
        "LOW": 0.25,
        "NORMAL": 0.25,
        "HIGH": 0.25,
        "EXTREME": 0.25,
    }
