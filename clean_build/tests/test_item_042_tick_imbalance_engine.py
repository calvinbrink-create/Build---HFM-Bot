from datetime import datetime, timedelta, timezone

import pytest

from cipherfx_clean.contracts import RawTick
from cipherfx_clean.intelligence.market_features import tick_imbalance_observation


BASE = datetime(2026, 8, 21, tzinfo=timezone.utc)


def test_tick_imbalance_reports_count_and_distance_without_hiding_neutral_ticks():
    ticks = (
        RawTick("USA100", BASE, 10.0, 10.2),
        RawTick("USA100", BASE + timedelta(seconds=1), 10.1, 10.3),
        RawTick("USA100", BASE + timedelta(seconds=2), 10.3, 10.5),
        RawTick("USA100", BASE + timedelta(seconds=3), 10.2, 10.4),
        RawTick("USA100", BASE + timedelta(seconds=4), 10.1, 10.5),
    )
    result = tick_imbalance_observation(ticks)
    assert (result.up_count, result.down_count, result.unchanged_count) == (2, 1, 1)
    assert result.sample_size == 4
    assert result.active_move_ratio == 0.75
    assert result.count_imbalance == pytest.approx(1 / 3)
    assert result.up_distance == pytest.approx(0.3)
    assert result.down_distance == pytest.approx(0.1)
    assert result.gross_distance == pytest.approx(0.4)
    assert result.net_change == pytest.approx(0.2)
    assert result.distance_imbalance == pytest.approx(0.5)
    assert result.source == "EXACT_MIDPOINT_DIRECTION_EVENTS"


def test_tick_imbalance_handles_all_unchanged_and_empty_windows():
    flat = (
        RawTick("UK100", BASE, 100.0, 100.2),
        RawTick("UK100", BASE + timedelta(seconds=1), 99.9, 100.3),
    )
    result = tick_imbalance_observation(flat)
    assert result.unchanged_count == 1
    assert result.count_imbalance == 0.0
    assert result.distance_imbalance == 0.0
    assert result.active_move_ratio == 0.0
    empty = tick_imbalance_observation(())
    assert empty.sample_size == 0 and empty.window_start is None and empty.window_end is None
