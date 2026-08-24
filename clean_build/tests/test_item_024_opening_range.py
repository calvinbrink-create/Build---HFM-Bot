from datetime import datetime, timedelta, timezone

from cipherfx_clean.contracts import Candle
from cipherfx_clean.intelligence.advanced import opening_range


def _bar(index, high, low, close):
    start = datetime(2026, 8, 21, 7, tzinfo=timezone.utc) + timedelta(minutes=index)
    return Candle("UK100", "M1", start, start + timedelta(minutes=1), close, high, low, close)


def test_opening_range_uses_only_first_declared_interval_not_full_session():
    rows = tuple(_bar(index, 100 + index, 90 - index, 95) for index in range(60))
    result = opening_range(
        rows, "LONDON", session_start=rows[0].start,
        duration=timedelta(minutes=15), observed_at=rows[-1].end,
    )

    assert result.completed
    assert result.observation_count == 15
    assert result.high == 114
    assert result.low == 76
    assert result.high != max(item.high for item in rows)
    assert result.low != min(item.low for item in rows)


def test_opening_range_is_explicitly_incomplete_before_window_ends():
    rows = tuple(_bar(index, 100, 90, 95) for index in range(5))
    result = opening_range(
        rows, "LONDON", session_start=rows[0].start,
        duration=timedelta(minutes=15), observed_at=rows[-1].end,
    )
    assert not result.completed
    assert result.breakout is None
