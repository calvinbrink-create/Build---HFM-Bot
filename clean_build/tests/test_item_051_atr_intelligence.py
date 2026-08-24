from datetime import datetime, timedelta, timezone

import pytest

from cipherfx_clean.contracts import Candle
from cipherfx_clean.intelligence.atr import (
    atr_intelligence_observation,
    true_range_series,
    wilder_atr_series,
)
from cipherfx_clean.intelligence.regime import classify_regime


START = datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc)


def candles(count=80, *, symbol="UK100", timeframe="M1", gap_at=None):
    output = []
    close = 100.0
    for index in range(count):
        if index == gap_at:
            close += 10.0
        start = START + timedelta(minutes=index)
        next_close = close + 0.2 + (index % 3) * 0.1
        output.append(
            Candle(
                symbol=symbol,
                timeframe=timeframe,
                start=start,
                end=start + timedelta(minutes=1),
                open=close,
                high=next_close + 0.5,
                low=close - 0.5,
                close=next_close,
                source="TEST_ATR_SERIES",
                source_id=f"bar:{index}",
            )
        )
        close = next_close
    return tuple(output)


def test_true_range_includes_gap_from_previous_close():
    rows = candles(4, gap_at=2)
    ranges = true_range_series(rows)
    candle_range = rows[2].high - rows[2].low
    assert ranges[2] > candle_range
    assert ranges[2] == pytest.approx(rows[2].high - rows[1].close)


def test_wilder_atr_uses_recursive_smoothing_after_initial_period_mean():
    rows = candles(20, gap_at=15)
    true_ranges = true_range_series(rows)
    series = wilder_atr_series(rows, period=14)
    expected = sum(true_ranges[:14]) / 14
    assert series[0] == pytest.approx((13, expected))
    for value in true_ranges[14:]:
        expected = ((expected * 13) + value) / 14
    assert series[-1][1] == pytest.approx(expected)


def test_atr_is_scoped_by_symbol_timeframe_session_and_regime():
    rows = candles()
    regime = classify_regime(rows).label
    result = atr_intelligence_observation(
        rows,
        symbol="UK100",
        timeframe="M1",
        regime=regime,
    )
    assert result.baseline_scope == f"UK100|M1|{result.session}|{regime}"
    assert result.status == "OBSERVED"
    assert result.context_sample_size > 0
    assert result.context_baseline_atr is not None
    assert result.atr_to_context_baseline is not None
    assert result.context_percentile is not None
    assert 0.0 <= result.context_percentile <= 1.0


def test_higher_timeframe_uses_explicit_multi_session_scope():
    rows = candles(timeframe="H4")
    regime = classify_regime(rows).label
    result = atr_intelligence_observation(
        rows,
        symbol="USA500",
        timeframe="H4",
        regime=regime,
    )
    assert result.session == "MULTI_SESSION"
    assert result.baseline_scope.startswith("USA500|H4|MULTI_SESSION|")


def test_context_identity_is_never_shared_between_symbols():
    rows = candles()
    regime = classify_regime(rows).label
    uk = atr_intelligence_observation(rows, symbol="UK100", timeframe="M1", regime=regime)
    us = atr_intelligence_observation(rows, symbol="USA100", timeframe="M1", regime=regime)
    assert uk.atr == us.atr
    assert uk.baseline_scope != us.baseline_scope


def test_regime_mismatch_and_bad_time_order_are_rejected():
    rows = candles()
    with pytest.raises(ValueError, match="canonical timeframe regime"):
        atr_intelligence_observation(rows, symbol="UK100", timeframe="M1", regime="UNKNOWN")
    with pytest.raises(ValueError, match="strictly increasing"):
        true_range_series((rows[1], rows[0]))
