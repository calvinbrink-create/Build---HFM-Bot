from datetime import datetime, timedelta, timezone
from math import log, sqrt
from statistics import mean, pstdev

import pytest

from cipherfx_clean.contracts import Candle
from cipherfx_clean.intelligence.market_features import (
    realized_volatility_observation,
    volatility_observation,
)


START = datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc)


def candles(closes, *, opens=None, ranges=None, end_seconds=None):
    opens = opens or closes
    ranges = ranges or tuple(2.0 for _ in closes)
    seconds = end_seconds or tuple(60 * (index + 1) for index in range(len(closes)))
    return tuple(
        Candle(
            symbol="XAUUSD",
            timeframe="M1",
            start=START + timedelta(seconds=end - 60),
            end=START + timedelta(seconds=end),
            open=float(open_price),
            high=float(close) + float(candle_range) / 2,
            low=float(close) - float(candle_range) / 2,
            close=float(close),
            source="TEST_CLOSE_SERIES",
            source_id=f"bar:{index}",
        )
        for index, (close, open_price, candle_range, end) in enumerate(
            zip(closes, opens, ranges, seconds)
        )
    )


def test_realized_volatility_is_exact_close_to_close_log_return_math():
    rows = candles((100.0, 102.0, 101.0, 104.0))
    result = realized_volatility_observation(rows)
    returns = tuple(log(current / previous) for previous, current in zip((100.0, 102.0, 101.0), (102.0, 101.0, 104.0)))
    variance = sum(value * value for value in returns)
    assert result.realized_variance == pytest.approx(variance)
    assert result.realized_volatility == pytest.approx(sqrt(variance))
    assert result.return_standard_deviation == pytest.approx(pstdev(returns))
    assert result.root_mean_square_return == pytest.approx(
        sqrt(mean(tuple(value * value for value in returns)))
    )
    assert result.sample_size == 4
    assert result.return_count == 3
    assert result.source == "CLOSE_TO_CLOSE_LOG_RETURNS_NO_ATR"


def test_realized_volatility_is_independent_of_atr_and_candle_range():
    narrow = candles((100.0, 101.0, 102.0), ranges=(1.0, 1.0, 1.0))
    wide = candles((100.0, 101.0, 102.0), ranges=(20.0, 30.0, 40.0))
    narrow_realized = realized_volatility_observation(narrow)
    wide_realized = realized_volatility_observation(wide)
    assert narrow_realized == wide_realized
    assert volatility_observation(narrow).atr != volatility_observation(wide).atr


def test_realized_volatility_ignores_open_price_and_uses_only_closes():
    first = candles((100.0, 99.0, 101.0), opens=(100.0, 99.0, 101.0))
    second = candles((100.0, 99.0, 101.0), opens=(80.0, 120.0, 70.0))
    assert realized_volatility_observation(first) == realized_volatility_observation(second)


def test_upside_and_downside_semivolatility_preserve_directional_components():
    result = realized_volatility_observation(candles((100.0, 102.0, 99.0, 103.0)))
    assert result.upside_semivolatility > 0.0
    assert result.downside_semivolatility > 0.0
    assert result.realized_volatility == pytest.approx(
        sqrt(result.upside_semivolatility**2 + result.downside_semivolatility**2)
    )


def test_invalid_prices_and_timestamps_are_rejected():
    with pytest.raises(ValueError, match="positive close"):
        realized_volatility_observation(candles((100.0, 0.0)))
    with pytest.raises(ValueError, match="strictly increasing"):
        realized_volatility_observation(candles((100.0, 101.0), end_seconds=(60, 60)))


def test_single_candle_has_explicit_zero_measurement_contract():
    result = realized_volatility_observation(candles((100.0,)))
    assert result.realized_volatility == 0.0
    assert result.sample_size == 1
    assert result.return_count == 0
    assert result.elapsed_seconds == 0.0
