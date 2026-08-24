"""Wilder ATR evidence scoped to the market context that produced it."""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import Sequence

from ..market import Candle
from .regime import classify_regime
from .session import session_label


SESSION_SCOPED_TIMEFRAMES = frozenset(
    {"1s", "5s", "15s", "M1", "M3", "M5", "M15", "M30", "H1"}
)


@dataclass(frozen=True)
class ATRIntelligenceObservation:
    symbol: str
    timeframe: str
    session: str
    regime: str
    period: int
    atr: float
    price_normalized_atr: float
    context_baseline_atr: float | None
    atr_to_context_baseline: float | None
    context_percentile: float | None
    context_sample_size: int
    true_range_count: int
    baseline_scope: str
    status: str
    source: str = "WILDER_TRUE_RANGE_CONTEXT_NORMALIZATION"


def true_range_series(candles: Sequence[Candle]) -> tuple[float, ...]:
    rows = tuple(candles)
    if any(
        current.end <= previous.end
        for previous, current in zip(rows, rows[1:])
    ):
        raise ValueError("ATR candles must have strictly increasing end times")
    output = []
    previous_close = None
    for row in rows:
        value = max(0.0, row.high - row.low)
        if previous_close is not None:
            value = max(
                value,
                abs(row.high - previous_close),
                abs(row.low - previous_close),
            )
        output.append(value)
        previous_close = row.close
    return tuple(output)


def wilder_atr_series(
    candles: Sequence[Candle],
    period: int = 14,
) -> tuple[tuple[int, float], ...]:
    if period < 1:
        raise ValueError("ATR period must be positive")
    rows = tuple(candles)
    if not rows:
        return ()
    ranges = true_range_series(rows)
    effective_period = min(period, len(ranges))
    current = sum(ranges[:effective_period]) / effective_period
    output = [(effective_period - 1, current)]
    for index in range(effective_period, len(ranges)):
        current = ((current * (effective_period - 1)) + ranges[index]) / effective_period
        output.append((index, current))
    return tuple(output)


def atr_intelligence_observation(
    candles: Sequence[Candle],
    *,
    symbol: str,
    timeframe: str,
    regime: str,
    period: int = 14,
) -> ATRIntelligenceObservation:
    rows = tuple(candles)
    if not rows:
        return ATRIntelligenceObservation(
            symbol,
            timeframe,
            "UNAVAILABLE",
            regime,
            period,
            0.0,
            0.0,
            None,
            None,
            None,
            0,
            0,
            f"{symbol}|{timeframe}|UNAVAILABLE|{regime}",
            "INSUFFICIENT_DATA",
        )
    if rows[-1].close <= 0:
        raise ValueError("ATR normalization requires a positive latest close")
    series = wilder_atr_series(rows, period)
    current_index, current_atr = series[-1]
    current_session = _session_scope(timeframe, rows[current_index])
    canonical_regime = classify_regime(rows).label
    if canonical_regime != regime:
        raise ValueError("ATR regime must match the canonical timeframe regime")
    context_values = []
    for index, value in series[:-1]:
        if _session_scope(timeframe, rows[index]) != current_session:
            continue
        if classify_regime(rows[: index + 1]).label != regime:
            continue
        context_values.append(value)
    baseline = median(context_values) if context_values else None
    ratio = current_atr / baseline if baseline and baseline > 0 else None
    percentile = (
        sum(value <= current_atr for value in context_values) / len(context_values)
        if context_values
        else None
    )
    scope = f"{symbol}|{timeframe}|{current_session}|{regime}"
    return ATRIntelligenceObservation(
        symbol=symbol,
        timeframe=timeframe,
        session=current_session,
        regime=regime,
        period=period,
        atr=current_atr,
        price_normalized_atr=current_atr / rows[-1].close,
        context_baseline_atr=baseline,
        atr_to_context_baseline=ratio,
        context_percentile=percentile,
        context_sample_size=len(context_values),
        true_range_count=len(rows),
        baseline_scope=scope,
        status="OBSERVED" if context_values else "INSUFFICIENT_CONTEXT_BASELINE",
    )


def _session_scope(timeframe: str, candle: Candle) -> str:
    if timeframe not in SESSION_SCOPED_TIMEFRAMES:
        return "MULTI_SESSION"
    return session_label(candle.end)
