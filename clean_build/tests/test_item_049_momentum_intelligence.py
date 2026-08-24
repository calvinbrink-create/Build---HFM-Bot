from datetime import datetime, timedelta, timezone

import pytest

from cipherfx_clean.contracts import Candle
from cipherfx_clean.intelligence.advanced import momentum_observation


START = datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc)


def candles(closes, end_seconds=None):
    seconds = end_seconds or tuple(60 * (index + 1) for index in range(len(closes)))
    return tuple(
        Candle(
            symbol="USA100",
            timeframe="M1",
            start=START + timedelta(seconds=end - 60),
            end=START + timedelta(seconds=end),
            open=float(close),
            high=float(close) + 0.5,
            low=float(close) - 0.5,
            close=float(close),
            source="TEST_PRICE_SERIES",
            source_id=f"bar:{index}",
        )
        for index, (close, end) in enumerate(zip(closes, seconds))
    )


def test_constant_price_velocity_has_zero_acceleration_and_full_persistence():
    result = momentum_observation(candles((100, 101, 102, 103)))
    assert result.direction == "UP"
    assert result.velocity_per_second == pytest.approx(3 / 180)
    assert result.acceleration_per_second_squared == pytest.approx(0.0)
    assert result.persistence_strength == pytest.approx(1.0)
    assert result.directional_persistence == pytest.approx(1.0)
    assert result.source == "CLOSE_TO_CLOSE_PRICE_TIME"


def test_acceleration_uses_close_to_close_velocity_and_actual_elapsed_time():
    result = momentum_observation(candles((100, 101, 103, 106)))
    first_velocity = 1 / 60
    last_velocity = 3 / 60
    assert result.velocity_per_second == pytest.approx(6 / 180)
    assert result.acceleration_per_second_squared == pytest.approx(
        (last_velocity - first_velocity) / 120
    )
    assert result.impulse_ratio == pytest.approx(2.0)


def test_irregular_bar_spacing_is_time_normalized_not_count_normalized():
    result = momentum_observation(
        candles((100, 101, 103), end_seconds=(60, 90, 150))
    )
    assert result.elapsed_seconds == pytest.approx(90.0)
    assert result.velocity_per_second == pytest.approx(3 / 90)
    assert result.slope == pytest.approx(1.5)


def test_pullback_is_descriptive_and_does_not_erase_the_net_direction():
    result = momentum_observation(candles((100, 102, 104, 103)))
    assert result.direction == "UP"
    assert result.pullback is True
    assert result.continuation is False
    assert result.persistence_strength == pytest.approx(2 / 3)


def test_non_increasing_candle_times_are_rejected():
    with pytest.raises(ValueError, match="strictly increasing"):
        momentum_observation(candles((100, 101), end_seconds=(60, 60)))
