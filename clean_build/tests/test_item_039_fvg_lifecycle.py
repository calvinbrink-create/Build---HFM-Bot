from datetime import datetime, timedelta, timezone

import pytest

from cipherfx_clean.contracts import Candle
from cipherfx_clean.intelligence.market_features import detect_fair_value_gaps


BASE = datetime(2026, 8, 21, tzinfo=timezone.utc)


def _candles(rows):
    return tuple(
        Candle(
            "USA500", "M5",
            BASE + timedelta(minutes=index * 5),
            BASE + timedelta(minutes=(index + 1) * 5),
            *row,
            source="TEST",
            source_id=f"m5:{index}",
        )
        for index, row in enumerate(rows)
    )


def _first_bullish(rows):
    return next(item for item in detect_fair_value_gaps(_candles(rows)) if item.direction == "BULLISH")


def test_fvg_lifecycle_tracks_untouched_and_partial_fill_depth():
    formation = (
        (10.0, 10.5, 9.5, 10.2),
        (10.2, 12.0, 10.1, 11.8),
        (11.8, 12.4, 11.0, 12.1),
    )
    untouched = _first_bullish(formation)
    assert untouched.state == "UNTOUCHED"
    assert untouched.first_touched_index is None
    assert untouched.fill_ratio == 0.0

    partial = _first_bullish(formation + ((12.1, 12.2, 10.75, 11.2),))
    assert partial.state == "PARTIAL"
    assert partial.first_touched_index == 3
    assert partial.fill_ratio == pytest.approx(0.5)
    assert partial.filled_at is None and partial.failed_at is None


def test_exact_near_edge_touch_is_not_reported_as_partial_fill():
    formation = (
        (10.0, 10.5, 9.5, 10.2),
        (10.2, 12.0, 10.1, 11.8),
        (11.8, 12.4, 11.0, 12.1),
    )
    touched_edge = _first_bullish(formation + ((12.1, 12.2, 11.0, 11.4),))
    assert touched_edge.state == "UNTOUCHED"
    assert touched_edge.first_touched_index is None
    assert touched_edge.fill_ratio == 0.0


def test_fvg_lifecycle_distinguishes_full_fill_from_close_through_failure():
    formation = (
        (10.0, 10.5, 9.5, 10.2),
        (10.2, 12.0, 10.1, 11.8),
        (11.8, 12.4, 11.0, 12.1),
    )
    filled = _first_bullish(formation + ((12.1, 12.2, 10.4, 10.7),))
    assert filled.state == "FILLED"
    assert filled.filled_at == 3 and filled.failed_at is None
    assert filled.fill_ratio == 1.0

    failed = _first_bullish(formation + ((12.1, 12.2, 10.2, 10.4),))
    assert failed.state == "FAILED"
    assert failed.failed_at == 3 and failed.filled_at is None
    assert failed.fill_ratio == 1.0


def test_bearish_fvg_failure_requires_close_above_far_boundary():
    rows = (
        (12.0, 12.5, 11.5, 12.2),
        (12.2, 12.3, 9.8, 10.0),
        (10.0, 11.0, 9.5, 9.8),
        (9.8, 11.8, 9.7, 11.7),
    )
    gap = next(item for item in detect_fair_value_gaps(_candles(rows)) if item.direction == "BEARISH")
    assert gap.state == "FAILED"
    assert gap.failed_at == 3
    assert gap.filled_at is None
