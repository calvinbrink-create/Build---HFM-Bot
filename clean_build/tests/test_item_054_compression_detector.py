from datetime import datetime, timedelta, timezone

from cipherfx_clean.contracts import Candle
from cipherfx_clean.intelligence.volatility_dynamics import (
    compression_observation,
    compression_outcomes,
)


START = datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc)


def market_with_compression_and_breakout():
    rows = []
    close = 100.0
    for index in range(130):
        if index < 90:
            move = 0.8 if index % 2 else -0.7
            candle_range = 2.5
        elif index < 120:
            fraction = (120 - index) / 30
            move = (0.12 * fraction) * (1 if index % 2 else -1)
            candle_range = 0.4 + fraction
        elif index == 120:
            move = 8.0
            candle_range = 9.0
        else:
            move = 0.5
            candle_range = 1.0
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
                source="TEST_COMPRESSION",
                source_id=f"bar:{index}",
            )
        )
        close = next_close
    return tuple(rows)


def test_compression_uses_both_declining_range_and_realized_volatility():
    rows = market_with_compression_and_breakout()[:120]
    result = compression_observation(rows, symbol="UK100", timeframe="M5")
    assert result.status == "OBSERVED"
    assert result.compression is True
    assert result.range_declining is True
    assert result.volatility_declining is True
    assert result.range_percentile <= 0.25
    assert result.volatility_percentile <= 0.25


def test_compression_outcome_records_breakout_direction_timing_and_excursion():
    outcomes = compression_outcomes(
        market_with_compression_and_breakout(),
        symbol="UK100",
        timeframe="M5",
        forward_bars=10,
    )
    breakouts = tuple(item for item in outcomes if item.breakout)
    assert breakouts
    assert any(item.breakout_direction == "UP" for item in breakouts)
    assert all(item.bars_to_breakout is not None for item in breakouts)
    assert all(item.maximum_breakout_excursion_ranges >= 0.0 for item in breakouts)


def test_current_compression_does_not_read_future_candles():
    rows = market_with_compression_and_breakout()
    before = compression_observation(rows[:120], symbol="UK100", timeframe="M5")
    changed_future = tuple(
        Candle(
            row.symbol,
            row.timeframe,
            row.start,
            row.end,
            row.open,
            row.high + 1000,
            row.low - 1000,
            row.close + 500,
            source=row.source,
            source_id=row.source_id,
        )
        for row in rows[120:]
    )
    after = compression_observation((*rows[:120], *changed_future), symbol="UK100", timeframe="M5")
    assert before.observation_index == 119
    assert after.observation_index == len(rows) - 1
    assert before == compression_observation(rows[:120], symbol="UK100", timeframe="M5")


def test_no_future_horizon_means_no_outcome_record_for_latest_event():
    rows = market_with_compression_and_breakout()[:120]
    outcomes = compression_outcomes(rows, symbol="UK100", timeframe="M5", forward_bars=10)
    assert all(item.compression_index + item.forward_bars < len(rows) for item in outcomes)
