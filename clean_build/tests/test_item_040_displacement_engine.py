from datetime import datetime, timedelta, timezone

import pytest

from cipherfx_clean.contracts import Candle
from cipherfx_clean.intelligence.market_features import measure_displacement


BASE = datetime(2026, 8, 21, tzinfo=timezone.utc)


def _candles(rows):
    return tuple(
        Candle(
            "UK100", "M5",
            BASE + timedelta(minutes=index * 5),
            BASE + timedelta(minutes=(index + 1) * 5),
            *row,
            source="TEST",
            source_id=f"m5:{index}",
        )
        for index, row in enumerate(rows)
    )


def test_displacement_normalizes_impulse_by_prior_true_range():
    rows = _candles(
        (
            (10.0, 11.0, 9.0, 10.5),
            (10.5, 11.5, 9.5, 10.8),
            (10.8, 11.8, 9.8, 11.0),
            (11.0, 16.0, 10.5, 15.5),
        )
    )
    result = measure_displacement(rows, lookback=3)
    assert result.direction == "UP"
    assert result.reference_range == 2.0
    assert result.range_value == 5.5
    assert result.expansion_ratio == 2.75
    assert result.body_value == 4.5
    assert result.body_participation == pytest.approx(4.5 / 5.5)
    assert result.close_location == pytest.approx(5.0 / 5.5)
    assert result.normalized_signed_move == 2.25
    assert result.abnormal_expansion is True
    assert result.normalization_source == "PRIOR_TRUE_RANGE"


def test_displacement_preserves_signed_downward_impulse():
    rows = _candles(
        (
            (10.0, 11.0, 9.0, 10.5),
            (10.5, 11.5, 9.5, 10.8),
            (10.8, 11.8, 9.8, 11.0),
            (11.0, 11.2, 6.0, 6.5),
        )
    )
    result = measure_displacement(rows, lookback=3)
    assert result.direction == "DOWN"
    assert result.normalized_signed_move < 0
    assert result.close_location > 0.8
    assert result.body_ratio > 1


def test_displacement_rejects_invalid_lookback_and_handles_no_data():
    with pytest.raises(ValueError):
        measure_displacement((), lookback=0)
    empty = measure_displacement(())
    assert empty.direction == "NONE"
    assert empty.index == -1
