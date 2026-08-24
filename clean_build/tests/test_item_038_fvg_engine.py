from datetime import datetime, timedelta, timezone

from cipherfx_clean.contracts import Candle
from cipherfx_clean.intelligence.market_features import detect_fair_value_gaps


BASE = datetime(2026, 8, 21, tzinfo=timezone.utc)


def _candles(rows):
    return tuple(
        Candle(
            "USA100", "M5",
            BASE + timedelta(minutes=index * 5),
            BASE + timedelta(minutes=(index + 1) * 5),
            *row,
            source="TEST",
            source_id=f"m5:{index}",
        )
        for index, row in enumerate(rows)
    )


def test_fvg_engine_detects_exact_bullish_three_candle_wick_imbalance():
    rows = _candles(
        (
            (10.0, 10.5, 9.5, 10.2),
            (10.2, 12.0, 10.1, 11.8),
            (11.8, 12.4, 11.0, 12.1),
        )
    )
    gap = detect_fair_value_gaps(rows)[0]
    assert gap.direction == "BULLISH"
    assert (gap.low, gap.high) == (10.5, 11.0)
    assert (gap.left_index, gap.impulse_index, gap.origin_index) == (0, 1, 2)
    assert gap.width == 0.5
    assert gap.midpoint == 10.75
    assert gap.source == "THREE_CANDLE_WICK_IMBALANCE"


def test_fvg_engine_detects_exact_bearish_three_candle_wick_imbalance():
    rows = _candles(
        (
            (12.0, 12.5, 11.5, 12.2),
            (12.2, 12.3, 9.8, 10.0),
            (10.0, 11.0, 9.5, 9.8),
        )
    )
    gap = detect_fair_value_gaps(rows)[0]
    assert gap.direction == "BEARISH"
    assert (gap.low, gap.high) == (11.0, 11.5)
    assert (gap.left_index, gap.impulse_index, gap.origin_index) == (0, 1, 2)
    assert gap.width == 0.5
    assert gap.midpoint == 11.25


def test_fvg_engine_does_not_label_overlapping_wicks_as_imbalance():
    rows = _candles(
        (
            (10.0, 11.0, 9.0, 10.5),
            (10.5, 11.5, 9.5, 10.8),
            (10.8, 12.0, 10.5, 11.5),
        )
    )
    assert detect_fair_value_gaps(rows) == ()
