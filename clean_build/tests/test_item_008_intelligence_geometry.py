from datetime import datetime, timedelta, timezone

import pytest

from cipherfx_clean.contracts import Candle
from cipherfx_clean.intelligence.advanced import (
    breakout_observation,
    continuation_observation,
    trend_observation,
)
from cipherfx_clean.intelligence.liquidity import identify_liquidity
from cipherfx_clean.intelligence.market_features import candle_vwap
from cipherfx_clean.intelligence.patterns import detect_geometric_patterns
from cipherfx_clean.intelligence.structure import analyse_structure


BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _rows(values):
    return tuple(
        Candle("XAUUSD", "M5", BASE + timedelta(minutes=5 * index), BASE + timedelta(minutes=5 * (index + 1)), *value, tick_volume=10 + index)
        for index, value in enumerate(values)
    )


def test_structure_emits_swing_labels_bos_and_real_choch():
    rows = _rows(
        (
            (9.5, 10, 9, 9.8),
            (10, 12, 10, 11),
            (10.5, 11, 9.5, 10),
            (11, 14, 11, 13),
            (11, 13, 10.5, 12),
            (12, 13.5, 11, 13),
            (13, 15, 8, 9),
        )
    )
    result = analyse_structure(rows, lookback=1)
    assert {"HH", "HL"}.issubset(result.swing_sequence)
    assert result.break_of_structure
    assert result.bos_direction == "DOWN"
    assert result.change_of_character
    assert result.choch_direction == "DOWN"


def test_structure_labels_are_chronological_and_cover_all_directional_relations():
    rows = _rows(
        (
            (9.5, 10, 9, 9.8),
            (10, 12, 10, 11),
            (10, 11, 8, 9),
            (11, 14, 10, 13),
            (10, 12, 9, 10),
            (11, 13, 10, 12),
            (8, 12, 7, 9),
            (9, 11, 8, 10),
        )
    )
    result = analyse_structure(rows, lookback=1)
    assert result.swing_sequence == ("HH", "HL", "LH", "LL")
    assert tuple(point.label for point in result.swings if point.label) == result.swing_sequence
    assert {point.hierarchy for point in result.swings} == {"MAJOR", "MINOR"}
    assert all(0.0 <= point.significance <= 1.0 for point in result.swings)
    assert all(point.significance > 0 for point in result.swings)


def test_structure_break_is_a_crossing_event_not_a_permanent_state():
    base = (
        (9.5, 10, 9, 9.8),
        (10, 12, 10, 11),
        (10, 11, 8, 9),
        (11, 14, 10, 13),
        (10, 12, 9, 10),
        (11, 13, 10, 12),
        (11, 14.5, 10.5, 14.2),
    )
    crossed = analyse_structure(_rows(base), lookback=1)
    assert crossed.break_of_structure and crossed.bos_direction == "UP"
    after_cross = analyse_structure(_rows((*base, (14, 15, 13, 14.5))), lookback=1)
    assert not after_cross.break_of_structure
    assert after_cross.bos_direction is None


def test_breakout_requires_crossing_and_continuation_invalidation_is_reachable():
    no_cross = _rows(((9, 11, 8, 10.5), (10.5, 12, 10, 11)))
    crossing = _rows(((9, 10, 8, 9.5), (9.5, 12, 9, 11)))
    assert breakout_observation(no_cross, 10) is None
    assert breakout_observation(crossing, 10).direction == "UP"

    invalidated = _rows(((10, 12, 9, 11), (9, 10, 8, 8.5)))
    assert continuation_observation(invalidated, "UP", .5).invalidated


def test_vwap_uses_broker_activity_weight_and_labels_its_source():
    rows = _rows(((9, 11, 8, 10), (10, 14, 9, 13)))
    result = candle_vwap(rows)
    expected = (((11 + 8 + 10) / 3) * 10 + ((14 + 9 + 13) / 3) * 11) / 21
    assert result.value == pytest.approx(expected)
    assert result.source == "HFM_TICK_VOLUME_WEIGHTED_TYPICAL_PRICE"


def test_liquidity_output_is_swing_based_not_quadratic_pair_duplication():
    rows = _rows(
        tuple((100, 101 + index % 3, 99 - index % 2, 100 + (index % 2) * .1) for index in range(100))
    )
    pools = identify_liquidity(rows)
    assert len(pools) < len(rows) * 3
    assert all(item.indices for item in pools)


def test_trend_and_extended_chart_geometry_are_descriptive():
    rows = _rows(tuple((100 + index, 101 + index, 99.5 + index, 100.8 + index) for index in range(30)))
    trend = trend_observation(rows)
    assert trend.direction == "UP"
    assert trend.fit > .9
    patterns = detect_geometric_patterns(rows)
    assert all(0 <= item.confidence <= 1 for item in patterns)
    assert all(isinstance(item.confirmed, bool) and isinstance(item.failed, bool) for item in patterns)
