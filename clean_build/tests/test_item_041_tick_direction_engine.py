from datetime import datetime, timedelta, timezone

import pytest

from cipherfx_clean.contracts import RawTick
from cipherfx_clean.intelligence.market_features import classify_tick_directions


BASE = datetime(2026, 8, 21, tzinfo=timezone.utc)


def test_tick_direction_engine_classifies_exact_midpoint_changes():
    ticks = (
        RawTick("XAUUSD", BASE, 10.0, 10.2),
        RawTick("XAUUSD", BASE + timedelta(seconds=1), 10.1, 10.3),
        RawTick("XAUUSD", BASE + timedelta(seconds=2), 10.0, 10.2),
        RawTick("XAUUSD", BASE + timedelta(seconds=3), 9.9, 10.3),
    )
    events = classify_tick_directions(ticks)
    assert tuple(item.direction for item in events) == ("UP", "DOWN", "UNCHANGED")
    assert tuple(item.price_change for item in events) == pytest.approx((0.1, -0.1, 0.0))
    assert all(item.source == "BID_ASK_MID_CHANGE" for item in events)
    assert all(item.interarrival_seconds == 1.0 for item in events)


def test_tick_direction_identity_is_stable_for_reordered_input():
    ticks = (
        RawTick("UK100", BASE, 100.0, 100.2),
        RawTick("UK100", BASE + timedelta(seconds=1), 100.1, 100.3),
    )
    assert classify_tick_directions(ticks) == classify_tick_directions(tuple(reversed(ticks)))
    assert classify_tick_directions(ticks)[0].event_id


def test_tick_direction_rejects_cross_symbol_comparisons():
    with pytest.raises(ValueError):
        classify_tick_directions(
            (
                RawTick("UK100", BASE, 100.0, 100.2),
                RawTick("USA100", BASE + timedelta(seconds=1), 100.1, 100.3),
            )
        )
