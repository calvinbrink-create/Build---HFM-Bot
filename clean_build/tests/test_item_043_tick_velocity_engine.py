from datetime import datetime, timedelta, timezone

import pytest

from cipherfx_clean.contracts import RawTick
from cipherfx_clean.intelligence.market_features import tick_velocity_observation


BASE = datetime(2026, 8, 21, tzinfo=timezone.utc)


def test_tick_velocity_separates_price_path_speed_from_quote_arrival_rate():
    ticks = (
        RawTick("USA30", BASE, 10.0, 10.2),
        RawTick("USA30", BASE + timedelta(seconds=1), 10.1, 10.3),
        RawTick("USA30", BASE + timedelta(seconds=3), 10.3, 10.5),
        RawTick("USA30", BASE + timedelta(seconds=4), 10.2, 10.4),
    )
    result = tick_velocity_observation(ticks)
    assert result.sample_size == 3
    assert result.duration_seconds == 4.0
    assert result.quote_arrival_rate == 0.75
    assert result.mean_interarrival_seconds == pytest.approx(4 / 3)
    assert result.median_interarrival_seconds == 1.0
    assert result.net_velocity == pytest.approx(0.05)
    assert result.path_velocity == pytest.approx(0.1)
    assert result.upward_path_velocity == pytest.approx(0.075)
    assert result.downward_path_velocity == pytest.approx(0.025)
    assert result.mean_absolute_event_velocity == pytest.approx(0.1)
    assert result.maximum_absolute_event_velocity == pytest.approx(0.1)
    assert result.latest_event_velocity == pytest.approx(-0.1)
    assert result.source == "EXACT_MIDPOINT_CHANGE_PER_ELAPSED_SECOND"


def test_tick_velocity_exposes_zero_duration_events_without_division_errors():
    ticks = (
        RawTick("XAUUSD", BASE, 10.0, 10.2),
        RawTick("XAUUSD", BASE, 10.1, 10.3),
    )
    result = tick_velocity_observation(ticks)
    assert result.zero_interval_count == 1
    assert result.duration_seconds == 0.0
    assert result.net_velocity == 0.0
    assert result.quote_arrival_rate == 0.0
