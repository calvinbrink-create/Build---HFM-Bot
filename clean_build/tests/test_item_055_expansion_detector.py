from datetime import datetime, timedelta, timezone

from cipherfx_clean.contracts import Candle
from cipherfx_clean.intelligence.volatility_dynamics import (
    expansion_after_compression_outcomes,
    expansion_observation,
    expansion_statistics,
)


START = datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc)


def market_with_compression_and_breakout(*, breakout=True):
    rows = []
    close = 100.0
    for index in range(135):
        if index < 90:
            move = 0.8 if index % 2 else -0.7
            candle_range = 2.5
        elif index < 120:
            fraction = (120 - index) / 30
            move = (0.12 * fraction) * (1 if index % 2 else -1)
            candle_range = 0.4 + fraction
        elif index == 120 and breakout:
            move = 8.0
            candle_range = 9.0
        else:
            move = 0.02 if index % 2 else -0.02
            candle_range = 0.3
        next_close = close + move
        start = START + timedelta(minutes=5 * index)
        rows.append(
            Candle(
                symbol="UK100",
                timeframe="M5",
                start=start,
                end=start + timedelta(minutes=5),
                open=close,
                high=max(close, next_close) + candle_range / 2,
                low=min(close, next_close) - candle_range / 2,
                close=next_close,
                source="TEST_EXPANSION",
                source_id=f"bar:{index}",
            )
        )
        close = next_close
    return tuple(rows)


def test_current_expansion_requires_break_range_and_volatility_growth():
    result = expansion_observation(
        market_with_compression_and_breakout()[:121],
        symbol="UK100",
        timeframe="M5",
    )
    assert result.status == "OBSERVED"
    assert result.expansion is True
    assert result.direction == "UP"
    assert result.range_expansion_ratio > 1.0
    assert result.volatility_expansion_ratio > 1.0
    assert result.bars_since_compression == 1


def test_historical_outcomes_preserve_expansion_and_no_expansion_denominator():
    expanded = expansion_after_compression_outcomes(
        market_with_compression_and_breakout(),
        symbol="UK100",
        timeframe="M5",
    )
    quiet = expansion_after_compression_outcomes(
        market_with_compression_and_breakout(breakout=False),
        symbol="UK100",
        timeframe="M5",
    )
    rows = (*expanded, *quiet)
    stats = expansion_statistics(rows)
    assert stats.compression_event_count == len(rows)
    assert stats.expansion_count > 0
    assert stats.no_expansion_count > 0
    assert stats.expansion_count + stats.no_expansion_count == len(rows)
    assert stats.expansion_rate == stats.expansion_count / len(rows)


def test_expansion_outcome_records_timing_direction_and_ratios():
    outcomes = expansion_after_compression_outcomes(
        market_with_compression_and_breakout(),
        symbol="UK100",
        timeframe="M5",
    )
    expansions = tuple(item for item in outcomes if item.expansion)
    assert expansions
    assert all(item.direction in {"UP", "DOWN"} for item in expansions)
    assert all(item.bars_to_expansion is not None for item in expansions)
    assert all(item.range_expansion_ratio > 1.0 for item in expansions)
    assert all(item.volatility_expansion_ratio > 1.0 for item in expansions)


def test_no_recent_compression_is_explicit_not_an_expansion():
    rows = market_with_compression_and_breakout()[:25]
    result = expansion_observation(rows, symbol="UK100", timeframe="M5")
    assert result.status == "NO_RECENT_COMPRESSION"
    assert result.expansion is False
    assert result.direction == "NONE"
