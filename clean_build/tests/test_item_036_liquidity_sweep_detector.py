from datetime import datetime, timedelta, timezone

import pytest

from cipherfx_clean.contracts import Candle
from cipherfx_clean.intelligence.market_features import detect_liquidity_sweeps


BASE = datetime(2026, 8, 21, tzinfo=timezone.utc)


def _candles(rows):
    return tuple(
        Candle(
            "XAUUSD",
            "M5",
            BASE + timedelta(minutes=index * 5),
            BASE + timedelta(minutes=(index + 1) * 5),
            *row,
            source="TEST",
            source_id=f"row:{index}",
        )
        for index, row in enumerate(rows)
    )


def test_upper_stop_run_records_immediate_reclaim_outcome():
    rows = _candles(
        (
            (9.5, 10.0, 9.0, 9.8),
            (9.8, 10.2, 9.4, 10.0),
            (10.0, 10.4, 9.7, 10.1),
            (10.1, 10.8, 9.9, 10.2),
            (10.2, 10.3, 9.5, 9.8),
        )
    )
    sweep = next(item for item in detect_liquidity_sweeps(rows, lookback=3) if item.direction == "UP")
    assert sweep.level == 10.4
    assert sweep.sweep_price == 10.8
    assert sweep.penetration == pytest.approx(0.4)
    assert sweep.reclaimed is True
    assert sweep.continuation is False
    assert sweep.reclaim_index == 3
    assert sweep.outcome == "RECLAIM"
    assert sweep.source == "ROLLING_PRIOR_EXTREME"


def test_lower_stop_run_records_confirmed_continuation_outcome():
    rows = _candles(
        (
            (10.0, 10.5, 9.5, 10.2),
            (10.2, 10.4, 9.2, 9.8),
            (9.8, 10.1, 9.0, 9.5),
            (9.5, 9.7, 8.7, 8.9),
            (8.9, 9.0, 8.4, 8.6),
        )
    )
    sweep = next(item for item in detect_liquidity_sweeps(rows, lookback=3) if item.direction == "DOWN")
    assert sweep.level == 9.0
    assert sweep.sweep_price == 8.7
    assert sweep.reclaimed is False
    assert sweep.continuation is True
    assert sweep.continuation_index == 4
    assert sweep.outcome == "CONTINUATION"


def test_unresolved_latest_stop_run_is_pending_not_fabricated():
    rows = _candles(
        (
            (9.0, 10.0, 8.5, 9.5),
            (9.5, 10.2, 9.0, 9.8),
            (9.8, 10.4, 9.4, 10.0),
            (10.0, 10.8, 9.8, 10.6),
        )
    )
    sweep = next(item for item in detect_liquidity_sweeps(rows, lookback=3) if item.direction == "UP")
    assert sweep.reclaimed is False
    assert sweep.continuation is False
    assert sweep.outcome == "PENDING"
    assert sweep.observation_end_index == 3


def test_detector_rejects_invalid_windows():
    with pytest.raises(ValueError):
        detect_liquidity_sweeps((), lookback=0)
    with pytest.raises(ValueError):
        detect_liquidity_sweeps((), outcome_window=0)
