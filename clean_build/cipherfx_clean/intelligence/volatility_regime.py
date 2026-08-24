"""Empirical probability distribution for realized-volatility regimes."""

from __future__ import annotations

from dataclasses import dataclass
from math import erf, log, sqrt
from statistics import median
from typing import Sequence

from ..market import Candle


VOLATILITY_REGIMES = ("LOW", "NORMAL", "HIGH", "EXTREME")


@dataclass(frozen=True)
class VolatilityRegimeProbability:
    symbol: str
    timeframe: str
    current_realized_volatility: float
    low_boundary: float
    high_boundary: float
    extreme_boundary: float
    uncertainty_log_scale: float
    probabilities: tuple[tuple[str, float], ...]
    dominant_regime: str
    entropy: float
    historical_sample_size: int
    lookback: int
    status: str
    source: str = "EMPIRICAL_REALIZED_VOLATILITY_PROBABILITY_NO_ATR"

    def probability(self, regime: str) -> float:
        return dict(self.probabilities)[regime]


def volatility_regime_probability(
    candles: Sequence[Candle],
    *,
    symbol: str,
    timeframe: str,
    lookback: int = 20,
) -> VolatilityRegimeProbability:
    if lookback < 3:
        raise ValueError("volatility-regime lookback must contain at least three candles")
    rows = tuple(candles)
    if any(row.close <= 0 for row in rows):
        raise ValueError("volatility regime requires positive close prices")
    if any(current.end <= previous.end for previous, current in zip(rows, rows[1:])):
        raise ValueError("volatility-regime candles must have strictly increasing end times")
    samples = rolling_realized_volatility(rows, lookback=lookback)
    if len(samples) < 5:
        return VolatilityRegimeProbability(
            symbol,
            timeframe,
            samples[-1] if samples else 0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            tuple((name, 0.25) for name in VOLATILITY_REGIMES),
            "NORMAL",
            1.0,
            max(0, len(samples) - 1),
            lookback,
            "INSUFFICIENT_HISTORY",
        )
    current = samples[-1]
    history = samples[:-1]
    low = _quantile(history, 0.25)
    high = _quantile(history, 0.75)
    extreme = _quantile(history, 0.95)
    log_history = tuple(log(max(value, 1e-15)) for value in history)
    log_changes = tuple(
        abs(current_value - previous_value)
        for previous_value, current_value in zip(log_history, log_history[1:])
    )
    uncertainty = max(median(log_changes) if log_changes else 0.0, 1e-6)
    current_log = log(max(current, 1e-15))
    low_cdf = _normal_cdf((log(max(low, 1e-15)) - current_log) / uncertainty)
    high_cdf = _normal_cdf((log(max(high, 1e-15)) - current_log) / uncertainty)
    extreme_cdf = _normal_cdf((log(max(extreme, 1e-15)) - current_log) / uncertainty)
    values = (
        max(0.0, low_cdf),
        max(0.0, high_cdf - low_cdf),
        max(0.0, extreme_cdf - high_cdf),
        max(0.0, 1.0 - extreme_cdf),
    )
    total = sum(values)
    normalized = tuple(value / total for value in values)
    probabilities = tuple(zip(VOLATILITY_REGIMES, normalized))
    dominant = max(probabilities, key=lambda item: item[1])[0]
    entropy = -sum(value * log(value) for value in normalized if value > 0) / log(4.0)
    return VolatilityRegimeProbability(
        symbol=symbol,
        timeframe=timeframe,
        current_realized_volatility=current,
        low_boundary=low,
        high_boundary=high,
        extreme_boundary=extreme,
        uncertainty_log_scale=uncertainty,
        probabilities=probabilities,
        dominant_regime=dominant,
        entropy=entropy,
        historical_sample_size=len(history),
        lookback=lookback,
        status="OBSERVED",
    )


def rolling_realized_volatility(
    candles: Sequence[Candle],
    *,
    lookback: int = 20,
) -> tuple[float, ...]:
    rows = tuple(candles)
    if len(rows) < lookback:
        return ()
    output = []
    for end in range(lookback, len(rows) + 1):
        window = rows[end - lookback : end]
        returns = tuple(
            log(current.close / previous.close)
            for previous, current in zip(window, window[1:])
        )
        output.append(sqrt(sum(value * value for value in returns)))
    return tuple(output)


def _quantile(values: Sequence[float], probability: float) -> float:
    ordered = tuple(sorted(values))
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + erf(value / sqrt(2.0)))
