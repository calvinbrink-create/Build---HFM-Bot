"""Observed changes in realized volatility, independent of candle range and ATR."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from math import log
from statistics import median
from typing import Sequence

from ..market import Candle
from .volatility_regime import rolling_realized_volatility


@dataclass(frozen=True)
class VolatilityOfVolatilityObservation:
    symbol: str
    timeframe: str
    current_realized_volatility: float
    previous_realized_volatility: float
    log_volatility_change: float
    absolute_log_volatility_change: float
    change_rate_per_second: float
    baseline_median_absolute_change: float
    robust_change_zscore: float
    rapid_change_percentile: float
    rapid_change: bool
    direction: str
    historical_change_sample_size: int
    lookback: int
    status: str
    source: str = "ROLLING_REALIZED_VOLATILITY_LOG_CHANGE_NO_ATR"


@dataclass(frozen=True)
class CompressionObservation:
    symbol: str
    timeframe: str
    observation_index: int
    range_level: float
    realized_volatility: float
    range_to_baseline: float | None
    volatility_to_baseline: float | None
    range_percentile: float | None
    volatility_percentile: float | None
    range_declining: bool
    volatility_declining: bool
    compression: bool
    historical_sample_size: int
    lookback: int
    status: str
    source: str = "EMPIRICAL_RANGE_AND_REALIZED_VOLATILITY_CONTRACTION"


@dataclass(frozen=True)
class CompressionOutcome:
    event_id: str
    symbol: str
    timeframe: str
    compression_index: int
    compression_at: str
    reference_low: float
    reference_high: float
    breakout: bool
    breakout_direction: str
    bars_to_breakout: int | None
    maximum_breakout_excursion_ranges: float
    failed_back_inside: bool
    forward_bars: int
    source: str = "COMPRESSION_FORWARD_CLOSE_BREAKOUT_OUTCOME"


def volatility_of_volatility_observation(
    candles: Sequence[Candle],
    *,
    symbol: str,
    timeframe: str,
    lookback: int = 20,
) -> VolatilityOfVolatilityObservation:
    rows = tuple(candles)
    if any(row.close <= 0 for row in rows):
        raise ValueError("volatility-of-volatility requires positive close prices")
    if any(current.end <= previous.end for previous, current in zip(rows, rows[1:])):
        raise ValueError("volatility-of-volatility candles must have strictly increasing end times")
    values = rolling_realized_volatility(rows, lookback=lookback)
    if len(values) < 7:
        return VolatilityOfVolatilityObservation(
            symbol,
            timeframe,
            values[-1] if values else 0.0,
            values[-2] if len(values) > 1 else 0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            False,
            "STABLE",
            max(0, len(values) - 2),
            lookback,
            "INSUFFICIENT_HISTORY",
        )
    log_values = tuple(log(max(value, 1e-15)) for value in values)
    changes = tuple(current - previous for previous, current in zip(log_values, log_values[1:]))
    current_change = changes[-1]
    current_absolute = abs(current_change)
    history = tuple(abs(value) for value in changes[:-1])
    baseline = median(history)
    deviations = tuple(abs(value - baseline) for value in history)
    mad = median(deviations) if deviations else 0.0
    scale = max(1.4826 * mad, baseline * 1e-9, 1e-12)
    percentile = sum(value <= current_absolute for value in history) / len(history)
    elapsed = (rows[-1].end - rows[-2].end).total_seconds()
    direction = "INCREASING" if current_change > 0 else "DECREASING" if current_change < 0 else "STABLE"
    robust_zscore = (current_absolute - baseline) / scale
    rapid = (
        percentile >= 0.95
        and robust_zscore >= 3.0
        and current_absolute > max(baseline, 1e-12)
    )
    return VolatilityOfVolatilityObservation(
        symbol=symbol,
        timeframe=timeframe,
        current_realized_volatility=values[-1],
        previous_realized_volatility=values[-2],
        log_volatility_change=current_change,
        absolute_log_volatility_change=current_absolute,
        change_rate_per_second=current_change / elapsed,
        baseline_median_absolute_change=baseline,
        robust_change_zscore=robust_zscore,
        rapid_change_percentile=percentile,
        rapid_change=rapid,
        direction=direction,
        historical_change_sample_size=len(history),
        lookback=lookback,
        status="OBSERVED",
    )


def compression_observation(
    candles: Sequence[Candle],
    *,
    symbol: str,
    timeframe: str,
    lookback: int = 20,
) -> CompressionObservation:
    observations = _rolling_compression_observations(
        candles,
        symbol=symbol,
        timeframe=timeframe,
        lookback=lookback,
    )
    if observations:
        return observations[-1]
    return CompressionObservation(
        symbol,
        timeframe,
        -1,
        0.0,
        0.0,
        None,
        None,
        None,
        None,
        False,
        False,
        False,
        0,
        lookback,
        "INSUFFICIENT_HISTORY",
    )


def compression_outcomes(
    candles: Sequence[Candle],
    *,
    symbol: str,
    timeframe: str,
    lookback: int = 20,
    forward_bars: int = 10,
) -> tuple[CompressionOutcome, ...]:
    if forward_bars < 1:
        raise ValueError("compression outcome horizon must be positive")
    rows = tuple(candles)
    observations = _rolling_compression_observations(
        rows,
        symbol=symbol,
        timeframe=timeframe,
        lookback=lookback,
    )
    output = []
    for observation in observations:
        index = observation.observation_index
        if not observation.compression or index + forward_bars >= len(rows):
            continue
        reference = rows[index - lookback + 1 : index + 1]
        reference_low = min(row.low for row in reference)
        reference_high = max(row.high for row in reference)
        reference_span = max(reference_high - reference_low, 1e-12)
        future = rows[index + 1 : index + 1 + forward_bars]
        breakout_offset = None
        direction = "NONE"
        for offset, row in enumerate(future, start=1):
            if row.close > reference_high:
                breakout_offset = offset
                direction = "UP"
                break
            if row.close < reference_low:
                breakout_offset = offset
                direction = "DOWN"
                break
        breakout = breakout_offset is not None
        excursion = 0.0
        failed = False
        if breakout:
            after_breakout = future[breakout_offset - 1 :]
            if direction == "UP":
                excursion = max(0.0, max(row.high for row in after_breakout) - reference_high) / reference_span
            else:
                excursion = max(0.0, reference_low - min(row.low for row in after_breakout)) / reference_span
            failed = any(reference_low <= row.close <= reference_high for row in after_breakout[1:])
        event_id = sha256(
            f"{symbol}|{timeframe}|{rows[index].end.isoformat()}|{lookback}|{forward_bars}".encode("utf-8")
        ).hexdigest()
        output.append(
            CompressionOutcome(
                event_id=event_id,
                symbol=symbol,
                timeframe=timeframe,
                compression_index=index,
                compression_at=rows[index].end.isoformat(),
                reference_low=reference_low,
                reference_high=reference_high,
                breakout=breakout,
                breakout_direction=direction,
                bars_to_breakout=breakout_offset,
                maximum_breakout_excursion_ranges=excursion,
                failed_back_inside=failed,
                forward_bars=forward_bars,
            )
        )
    return tuple(output)


def _rolling_compression_observations(
    candles: Sequence[Candle],
    *,
    symbol: str,
    timeframe: str,
    lookback: int,
) -> tuple[CompressionObservation, ...]:
    if lookback < 3:
        raise ValueError("compression lookback must contain at least three candles")
    rows = tuple(candles)
    if any(row.close <= 0 for row in rows):
        raise ValueError("compression requires positive close prices")
    if any(current.end <= previous.end for previous, current in zip(rows, rows[1:])):
        raise ValueError("compression candles must have strictly increasing end times")
    volatility_values = rolling_realized_volatility(rows, lookback=lookback)
    if not volatility_values:
        return ()
    range_values = tuple(
        sum((row.high - row.low) / row.close for row in rows[end - lookback : end]) / lookback
        for end in range(lookback, len(rows) + 1)
    )
    output = []
    for offset, (range_level, realized) in enumerate(zip(range_values, volatility_values)):
        index = lookback - 1 + offset
        range_history = range_values[:offset]
        volatility_history = volatility_values[:offset]
        if len(range_history) < 10:
            output.append(
                CompressionObservation(
                    symbol,
                    timeframe,
                    index,
                    range_level,
                    realized,
                    None,
                    None,
                    None,
                    None,
                    False,
                    False,
                    False,
                    len(range_history),
                    lookback,
                    "INSUFFICIENT_HISTORY",
                )
            )
            continue
        range_baseline = median(range_history)
        volatility_baseline = median(volatility_history)
        range_percentile = sum(value <= range_level for value in range_history) / len(range_history)
        volatility_percentile = sum(value <= realized for value in volatility_history) / len(volatility_history)
        range_declining = range_level < range_values[offset - 1]
        volatility_declining = realized < volatility_values[offset - 1]
        compressed = (
            range_percentile <= 0.25
            and volatility_percentile <= 0.25
            and range_declining
            and volatility_declining
        )
        output.append(
            CompressionObservation(
                symbol=symbol,
                timeframe=timeframe,
                observation_index=index,
                range_level=range_level,
                realized_volatility=realized,
                range_to_baseline=range_level / max(range_baseline, 1e-15),
                volatility_to_baseline=realized / max(volatility_baseline, 1e-15),
                range_percentile=range_percentile,
                volatility_percentile=volatility_percentile,
                range_declining=range_declining,
                volatility_declining=volatility_declining,
                compression=compressed,
                historical_sample_size=len(range_history),
                lookback=lookback,
                status="OBSERVED",
            )
        )
    return tuple(output)


@dataclass(frozen=True)
class ExpansionObservation:
    symbol: str
    timeframe: str
    compression_index: int | None
    bars_since_compression: int | None
    expansion: bool
    direction: str
    range_expansion_ratio: float | None
    volatility_expansion_ratio: float | None
    close_distance_ranges: float
    status: str
    source: str = "CLOSE_BREAK_WITH_RANGE_AND_REALIZED_VOLATILITY_EXPANSION"


@dataclass(frozen=True)
class ExpansionAfterCompressionOutcome:
    event_id: str
    symbol: str
    timeframe: str
    compression_index: int
    expansion_index: int | None
    expansion_at: str | None
    expansion: bool
    direction: str
    bars_to_expansion: int | None
    range_expansion_ratio: float
    volatility_expansion_ratio: float
    close_distance_ranges: float
    forward_bars: int
    source: str = "COMPRESSION_TO_EXPANSION_FORWARD_OUTCOME"


@dataclass(frozen=True)
class ExpansionStatistics:
    compression_event_count: int
    expansion_count: int
    no_expansion_count: int
    expansion_rate: float
    upward_expansion_count: int
    downward_expansion_count: int
    median_bars_to_expansion: float | None
    median_range_expansion_ratio: float | None
    median_volatility_expansion_ratio: float | None


def expansion_observation(
    candles: Sequence[Candle],
    *,
    symbol: str,
    timeframe: str,
    lookback: int = 20,
    recent_bars: int = 10,
) -> ExpansionObservation:
    rows = tuple(candles)
    compression_rows = _rolling_compression_observations(
        rows,
        symbol=symbol,
        timeframe=timeframe,
        lookback=lookback,
    )
    latest_index = len(rows) - 1
    recent = next(
        (
            item
            for item in reversed(compression_rows)
            if item.compression and 0 < latest_index - item.observation_index <= recent_bars
        ),
        None,
    )
    if recent is None:
        return ExpansionObservation(
            symbol,
            timeframe,
            None,
            None,
            False,
            "NONE",
            None,
            None,
            0.0,
            "NO_RECENT_COMPRESSION",
        )
    return _expansion_at_index(
        rows,
        symbol=symbol,
        timeframe=timeframe,
        compression=recent,
        expansion_index=latest_index,
        lookback=lookback,
    )


def expansion_after_compression_outcomes(
    candles: Sequence[Candle],
    *,
    symbol: str,
    timeframe: str,
    lookback: int = 20,
    forward_bars: int = 10,
) -> tuple[ExpansionAfterCompressionOutcome, ...]:
    if forward_bars < 1:
        raise ValueError("expansion outcome horizon must be positive")
    rows = tuple(candles)
    compression_rows = _rolling_compression_observations(
        rows,
        symbol=symbol,
        timeframe=timeframe,
        lookback=lookback,
    )
    output = []
    for compression in compression_rows:
        index = compression.observation_index
        if not compression.compression or index + forward_bars >= len(rows):
            continue
        best_range_ratio = 0.0
        best_volatility_ratio = 0.0
        observed = None
        for offset in range(1, forward_bars + 1):
            candidate = _expansion_at_index(
                rows,
                symbol=symbol,
                timeframe=timeframe,
                compression=compression,
                expansion_index=index + offset,
                lookback=lookback,
            )
            best_range_ratio = max(best_range_ratio, candidate.range_expansion_ratio or 0.0)
            best_volatility_ratio = max(
                best_volatility_ratio,
                candidate.volatility_expansion_ratio or 0.0,
            )
            if candidate.expansion:
                observed = candidate
                break
        event_id = sha256(
            f"{symbol}|{timeframe}|{rows[index].end.isoformat()}|EXPANSION|{forward_bars}".encode("utf-8")
        ).hexdigest()
        output.append(
            ExpansionAfterCompressionOutcome(
                event_id=event_id,
                symbol=symbol,
                timeframe=timeframe,
                compression_index=index,
                expansion_index=(index + observed.bars_since_compression) if observed else None,
                expansion_at=(
                    rows[index + observed.bars_since_compression].end.isoformat()
                    if observed and observed.bars_since_compression is not None
                    else None
                ),
                expansion=observed is not None,
                direction=observed.direction if observed else "NONE",
                bars_to_expansion=observed.bars_since_compression if observed else None,
                range_expansion_ratio=(observed.range_expansion_ratio or 0.0) if observed else best_range_ratio,
                volatility_expansion_ratio=(
                    observed.volatility_expansion_ratio or 0.0
                    if observed
                    else best_volatility_ratio
                ),
                close_distance_ranges=observed.close_distance_ranges if observed else 0.0,
                forward_bars=forward_bars,
            )
        )
    return tuple(output)


def expansion_statistics(
    outcomes: Sequence[ExpansionAfterCompressionOutcome],
) -> ExpansionStatistics:
    rows = tuple(outcomes)
    expansions = tuple(item for item in rows if item.expansion)
    return ExpansionStatistics(
        compression_event_count=len(rows),
        expansion_count=len(expansions),
        no_expansion_count=len(rows) - len(expansions),
        expansion_rate=len(expansions) / len(rows) if rows else 0.0,
        upward_expansion_count=sum(item.direction == "UP" for item in expansions),
        downward_expansion_count=sum(item.direction == "DOWN" for item in expansions),
        median_bars_to_expansion=(
            median(tuple(item.bars_to_expansion for item in expansions if item.bars_to_expansion is not None))
            if expansions
            else None
        ),
        median_range_expansion_ratio=(
            median(tuple(item.range_expansion_ratio for item in expansions))
            if expansions
            else None
        ),
        median_volatility_expansion_ratio=(
            median(tuple(item.volatility_expansion_ratio for item in expansions))
            if expansions
            else None
        ),
    )


def _expansion_at_index(
    rows: tuple[Candle, ...],
    *,
    symbol: str,
    timeframe: str,
    compression: CompressionObservation,
    expansion_index: int,
    lookback: int,
) -> ExpansionObservation:
    compression_index = compression.observation_index
    reference = rows[compression_index - lookback + 1 : compression_index + 1]
    reference_low = min(row.low for row in reference)
    reference_high = max(row.high for row in reference)
    reference_span = max(reference_high - reference_low, 1e-12)
    baseline_range = median(tuple((row.high - row.low) / row.close for row in reference))
    candidate = rows[expansion_index]
    candidate_range = (candidate.high - candidate.low) / candidate.close
    volatility_values = rolling_realized_volatility(rows[: expansion_index + 1], lookback=lookback)
    candidate_volatility = volatility_values[-1] if volatility_values else 0.0
    range_ratio = candidate_range / max(baseline_range, 1e-15)
    volatility_ratio = candidate_volatility / max(compression.realized_volatility, 1e-15)
    direction = (
        "UP"
        if candidate.close > reference_high
        else "DOWN"
        if candidate.close < reference_low
        else "NONE"
    )
    close_distance = (
        (candidate.close - reference_high) / reference_span
        if direction == "UP"
        else (reference_low - candidate.close) / reference_span
        if direction == "DOWN"
        else 0.0
    )
    expanded = direction != "NONE" and range_ratio > 1.0 and volatility_ratio > 1.0
    return ExpansionObservation(
        symbol=symbol,
        timeframe=timeframe,
        compression_index=compression_index,
        bars_since_compression=expansion_index - compression_index,
        expansion=expanded,
        direction=direction if expanded else "NONE",
        range_expansion_ratio=range_ratio,
        volatility_expansion_ratio=volatility_ratio,
        close_distance_ranges=close_distance if expanded else 0.0,
        status="OBSERVED",
    )
