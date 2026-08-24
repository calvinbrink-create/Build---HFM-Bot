from datetime import datetime, timedelta, timezone

import pytest

from cipherfx_clean.contracts import Candle
from cipherfx_clean.intelligence.volatility_dynamics import (
    volatility_of_volatility_observation,
)


START = datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc)


def candles(returns, *, ranges=None):
    closes = [100.0]
    for value in returns:
        closes.append(closes[-1] * (1.0 + value))
    ranges = ranges or tuple(1.0 for _ in closes)
    return tuple(
        Candle(
            symbol="XAUUSD",
            timeframe="M5",
            start=START + timedelta(minutes=5 * index),
            end=START + timedelta(minutes=5 * (index + 1)),
            open=close,
            high=close + candle_range / 2,
            low=close - candle_range / 2,
            close=close,
            source="TEST_VOLATILITY_CHANGE",
            source_id=f"bar:{index}",
        )
        for index, (close, candle_range) in enumerate(zip(closes, ranges))
    )


def test_stable_realized_volatility_is_not_a_rapid_change():
    returns = tuple((0.001 if index % 2 else -0.001) for index in range(120))
    result = volatility_of_volatility_observation(
        candles(returns), symbol="XAUUSD", timeframe="M5"
    )
    assert result.status == "OBSERVED"
    assert result.rapid_change is False
    assert result.robust_change_zscore < 3.0
    assert result.absolute_log_volatility_change == pytest.approx(
        result.baseline_median_absolute_change,
        rel=1e-6,
    )


def test_latest_volatility_jump_is_detected_against_its_history():
    baseline = tuple((0.0005 if index % 2 else -0.0005) for index in range(120))
    result = volatility_of_volatility_observation(
        candles((*baseline, 0.02)), symbol="XAUUSD", timeframe="M5"
    )
    assert result.direction == "INCREASING"
    assert result.rapid_change is True
    assert result.rapid_change_percentile >= 0.95
    assert result.robust_change_zscore > 0.0


def test_latest_volatility_collapse_reports_decreasing_direction():
    variable = tuple((0.005 if index % 2 else -0.005) for index in range(120))
    result = volatility_of_volatility_observation(
        candles((*variable, 0.0)), symbol="USA100", timeframe="M5"
    )
    assert result.direction in {"DECREASING", "STABLE"}
    assert result.absolute_log_volatility_change >= 0.0


def test_measurement_is_independent_of_atr_and_candle_ranges():
    returns = tuple((0.001 if index % 2 else -0.0015) for index in range(80))
    narrow = candles(returns, ranges=tuple(1.0 for _ in range(81)))
    wide = candles(returns, ranges=tuple(100.0 for _ in range(81)))
    first = volatility_of_volatility_observation(narrow, symbol="USA30", timeframe="M5")
    second = volatility_of_volatility_observation(wide, symbol="USA30", timeframe="M5")
    assert first == second


def test_short_history_is_explicitly_unavailable():
    result = volatility_of_volatility_observation(
        candles((0.001, -0.001, 0.001)), symbol="UK100", timeframe="M5"
    )
    assert result.status == "INSUFFICIENT_HISTORY"
    assert result.rapid_change is False


def test_bad_time_order_is_rejected():
    rows = candles(tuple(0.001 for _ in range(30)))
    with pytest.raises(ValueError, match="strictly increasing"):
        volatility_of_volatility_observation(
            (*rows[:-2], rows[-1], rows[-2]), symbol="USA500", timeframe="M5"
        )
