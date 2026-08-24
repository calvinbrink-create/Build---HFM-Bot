from datetime import datetime, timedelta, timezone

import pytest

from cipherfx_clean.contracts import RawTick
from cipherfx_clean.intelligence.market_features import tick_acceleration_observation


BASE = datetime(2026, 8, 21, tzinfo=timezone.utc)


def test_tick_acceleration_uses_velocity_change_per_elapsed_second():
    ticks = (
        RawTick("USA500", BASE, 10.0, 10.2),
        RawTick("USA500", BASE + timedelta(seconds=1), 10.1, 10.3),
        RawTick("USA500", BASE + timedelta(seconds=2), 10.4, 10.6),
        RawTick("USA500", BASE + timedelta(seconds=3), 10.6, 10.8),
    )
    result = tick_acceleration_observation(ticks)
    assert result.velocity_sample_size == 3
    assert result.acceleration_sample_size == 2
    assert result.previous_velocity == pytest.approx(0.3)
    assert result.latest_velocity == pytest.approx(0.2)
    assert result.latest_acceleration == pytest.approx(-0.1)
    assert result.mean_absolute_acceleration == pytest.approx(0.15)
    assert result.maximum_absolute_acceleration == pytest.approx(0.2)
    assert (result.positive_acceleration_count, result.negative_acceleration_count) == (1, 1)
    assert result.source == "CHANGE_IN_EVENT_VELOCITY_PER_ELAPSED_SECOND"


def test_tick_acceleration_reports_rapid_relative_change_and_zero_intervals():
    ticks = (
        RawTick("XAUUSD", BASE, 10.0, 10.2),
        RawTick("XAUUSD", BASE + timedelta(seconds=1), 10.1, 10.3),
        RawTick("XAUUSD", BASE + timedelta(seconds=2), 10.2, 10.4),
        RawTick("XAUUSD", BASE + timedelta(seconds=3), 11.2, 11.4),
    )
    result = tick_acceleration_observation(ticks)
    assert result.rapid_change is True
    assert result.latest_acceleration_zscore > 2

    duplicate_time = tick_acceleration_observation(
        (
            RawTick("XAUUSD", BASE, 10.0, 10.2),
            RawTick("XAUUSD", BASE, 10.1, 10.3),
        )
    )
    assert duplicate_time.zero_interval_count == 1
    assert duplicate_time.acceleration_sample_size == 0
