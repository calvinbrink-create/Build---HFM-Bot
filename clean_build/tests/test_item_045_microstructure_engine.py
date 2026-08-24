from dataclasses import FrozenInstanceError, asdict
from datetime import datetime, timedelta, timezone
from statistics import pstdev

import pytest

from cipherfx_clean.contracts import RawTick
from cipherfx_clean.intelligence.microstructure import measure_microstructure


BASE = datetime(2026, 8, 24, 8, tzinfo=timezone.utc)


def _ticks():
    return (
        RawTick("UK100", BASE, 100.0, 100.2),
        RawTick("UK100", BASE + timedelta(seconds=1), 100.1, 100.4),
        RawTick("UK100", BASE + timedelta(seconds=2), 100.4, 100.6),
        RawTick("UK100", BASE + timedelta(seconds=3), 100.3, 100.7),
        RawTick("UK100", BASE + timedelta(seconds=4), 100.8, 101.0),
    )


def test_microstructure_combines_transparent_quote_event_components():
    result = measure_microstructure(_ticks())

    assert result.symbol == "UK100"
    assert result.status == "OBSERVED"
    assert (result.tick_count, result.event_count) == (5, 4)
    assert result.window_start == BASE
    assert result.window_end == BASE + timedelta(seconds=4)
    assert result.duration_seconds == 4.0

    assert result.latest_spread == pytest.approx(0.2)
    assert result.mean_spread == pytest.approx(0.26)
    assert result.median_spread == pytest.approx(0.2)
    assert result.minimum_spread == pytest.approx(0.2)
    assert result.max_spread == pytest.approx(0.4)
    assert result.spread_stddev > 0
    assert result.mean_relative_spread_bps > 0

    assert result.quote_arrival_rate == pytest.approx(1.0)
    assert result.mean_interarrival_seconds == pytest.approx(1.0)
    assert result.net_quote_movement == pytest.approx(0.8)
    assert result.gross_quote_movement == pytest.approx(0.8)
    assert result.directional_imbalance == pytest.approx(1.0)
    assert result.distance_imbalance == pytest.approx(1.0)
    assert result.velocity == pytest.approx(0.2)
    assert result.path_velocity == pytest.approx(0.2)

    changes = (0.15, 0.25, 0.0, 0.4)
    assert result.micro_volatility == pytest.approx(pstdev(changes))
    assert result.relative_micro_volatility_bps > 0
    assert result.acceleration == pytest.approx(0.4)
    assert result.mean_absolute_acceleration == pytest.approx(0.25)
    assert result.maximum_absolute_acceleration == pytest.approx(0.4)

    fields = set(asdict(result))
    assert not fields.intersection({"score", "approved", "qualified", "trade", "decision"})
    with pytest.raises(FrozenInstanceError):
        result.status = "APPROVED"


def test_microstructure_requires_one_symbol_and_reports_small_windows_honestly():
    empty = measure_microstructure(())
    assert empty.status == "INSUFFICIENT_TICKS"
    assert empty.tick_count == 0

    one = measure_microstructure((_ticks()[0],))
    assert one.status == "INSUFFICIENT_TICKS"
    assert one.tick_count == 1
    assert one.quote_arrival_rate == 0.0

    with pytest.raises(ValueError, match="exactly one symbol"):
        measure_microstructure(
            (
                _ticks()[0],
                RawTick("USA500", BASE + timedelta(seconds=1), 50.0, 50.2),
            )
        )
