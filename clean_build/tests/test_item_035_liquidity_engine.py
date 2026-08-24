from datetime import datetime, timedelta, timezone

from cipherfx_clean.contracts import Candle
from cipherfx_clean.intelligence.liquidity import identify_liquidity, session_liquidity
from cipherfx_clean.intelligence.market_features import SessionRange


BASE = datetime(2026, 8, 24, tzinfo=timezone.utc)


def _rows(values):
    return tuple(
        Candle(
            "XAUUSD",
            "M5",
            BASE + timedelta(minutes=5 * index),
            BASE + timedelta(minutes=5 * (index + 1)),
            *value,
        )
        for index, value in enumerate(values)
    )


def test_liquidity_engine_emits_equal_swings_and_exact_previous_completed_bar():
    rows = _rows(
        (
            (9.5, 10, 9, 9.8),
            (9.5, 10.5, 9, 10),
            (10, 12, 9.5, 11),
            (10, 11, 9, 10),
            (9, 10.5, 8, 9),
            (9, 10.8, 9, 10),
            (10, 11, 9.5, 10.5),
            (11, 12.004, 9, 11.5),
            (9, 11, 8.003, 9),
            (9, 10.8, 9, 10),
            (10, 10.8, 9.2, 10.2),
        )
    )
    pools = identify_liquidity(rows, tolerance=0.001)
    kinds = {item.kind for item in pools}
    assert {"EQUAL_HIGH", "EQUAL_LOW", "SWING_HIGH", "SWING_LOW", "PREVIOUS_HIGH", "PREVIOUS_LOW"}.issubset(kinds)
    previous_high = next(item for item in pools if item.kind == "PREVIOUS_HIGH")
    previous_low = next(item for item in pools if item.kind == "PREVIOUS_LOW")
    assert previous_high.price == rows[-2].high
    assert previous_low.price == rows[-2].low
    assert previous_high.source == previous_low.source == "PREVIOUS_COMPLETED_CANDLE"


def test_session_liquidity_retains_session_identity_and_price():
    ranges = {
        "ASIA": SessionRange("ASIA", BASE, BASE + timedelta(hours=7), 12.5, 8.5, 10.5, 4.0),
        "LONDON": SessionRange("LONDON", BASE + timedelta(hours=7), BASE + timedelta(hours=16), None, None, None, None),
    }
    pools = session_liquidity(ranges, 10.0)
    assert {(item.kind, item.session_id, item.price) for item in pools} == {
        ("SESSION_HIGH", "ASIA", 12.5),
        ("SESSION_LOW", "ASIA", 8.5),
    }
    assert all(item.source == "SESSION_RANGE" for item in pools)
