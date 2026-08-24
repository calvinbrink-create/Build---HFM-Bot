"""Completed-candle market-regime classification and empirical context.

This module describes the current market state.  It does not approve, block,
or route trades.  Regime probabilities are derived from completed candles and
are carried as evidence for research and downstream reporting.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from math import copysign
from statistics import mean, median
from typing import Mapping, Sequence

from ..market import Candle


REGIME_LABELS = (
    "TREND_UP",
    "TREND_DOWN",
    "RANGE",
    "BREAKOUT",
    "COMPRESSION",
    "EXPANSION",
    "VOLATILE",
    "QUIET",
    "NEWS",
    "UNKNOWN",
)


@dataclass(frozen=True)
class MarketRegimeObservation:
    symbol: str
    timeframe: str
    observed_at_utc: str
    label: str
    probabilities: Mapping[str, float]
    confidence: float
    volatility_ratio: float
    volatility_percentile: float
    directional_persistence: float
    slope: float
    compression_ratio: float
    breakout: bool
    news_state: str
    sample_size: int
    source_digest: str
    source: str = "COMPLETED_CANDLE_MARKET_REGIME_ENGINE"


@dataclass(frozen=True)
class MarketRegimeLibrary:
    observed_at: str
    observations: tuple[MarketRegimeObservation, ...]
    source: str = "MARKET_REGIME_LIBRARY"

    def observations_for_symbol(self, symbol: str) -> Mapping[str, MarketRegimeObservation]:
        return {
            item.timeframe: item
            for item in self.observations
            if item.symbol == symbol
        }


def _digest(rows: Sequence[Candle]) -> str:
    source = "|".join(item.source_id for item in rows)
    return sha256(source.encode("utf-8")).hexdigest()


def _returns(rows: Sequence[Candle]) -> tuple[float, ...]:
    return tuple(
        (current.close - previous.close) / abs(previous.close)
        for previous, current in zip(rows, rows[1:])
        if previous.close
    )


def _normalise(values: Mapping[str, float]) -> Mapping[str, float]:
    cleaned = {key: max(float(value), 0.0) for key, value in values.items()}
    total = sum(cleaned.values())
    if total <= 0.0:
        return {key: (1.0 if key == "UNKNOWN" else 0.0) for key in REGIME_LABELS}
    return {key: cleaned.get(key, 0.0) / total for key in REGIME_LABELS}


def _volatility_percentile(current: float, history: Sequence[float]) -> float:
    if not history:
        return 0.0
    return sum(value <= current for value in history) / len(history)


def _observe(
    rows: Sequence[Candle],
    *,
    symbol: str,
    timeframe: str,
    observed_at: datetime,
    lookback: int,
    baseline_window: int,
    news_state: str,
) -> MarketRegimeObservation:
    completed = tuple(sorted(rows, key=lambda item: item.start))
    observed = observed_at.astimezone(timezone.utc)
    if len(completed) < 3:
        return MarketRegimeObservation(
            symbol=symbol,
            timeframe=timeframe,
            observed_at_utc=observed.isoformat(),
            label="UNKNOWN",
            probabilities=_normalise({"UNKNOWN": 1.0}),
            confidence=0.0,
            volatility_ratio=0.0,
            volatility_percentile=0.0,
            directional_persistence=0.0,
            slope=0.0,
            compression_ratio=0.0,
            breakout=False,
            news_state=news_state,
            sample_size=len(completed),
            source_digest=_digest(completed),
        )

    current = completed[-lookback:]
    ranges = tuple(max(item.high - item.low, 0.0) for item in completed)
    current_ranges = ranges[-min(lookback, len(ranges)):]
    baseline_start = max(0, len(ranges) - lookback - baseline_window)
    baseline_end = max(0, len(ranges) - lookback)
    baseline_ranges = ranges[baseline_start:baseline_end]
    baseline_reference = median(baseline_ranges) if baseline_ranges else median(ranges[:-1] or ranges)
    current_reference = mean(current_ranges) if current_ranges else 0.0
    volatility_ratio = current_reference / max(baseline_reference, 1e-12)
    rolling_start = max(lookback, len(ranges) - baseline_window)
    rolling_source_start = max(0, rolling_start - lookback)
    rolling_source = ranges[rolling_source_start:]
    cumulative = [0.0]
    for value in rolling_source:
        cumulative.append(cumulative[-1] + float(value))
    rolling_volatility = tuple(
        (
            cumulative[index - rolling_source_start]
            - cumulative[index - rolling_source_start - lookback]
        ) / lookback
        for index in range(rolling_start, len(ranges) + 1)
    ) if len(ranges) >= lookback else ()
    volatility_percentile = _volatility_percentile(current_reference, rolling_volatility)

    returns = _returns(current)
    directional_persistence = (
        abs(sum(1 if value > 0 else -1 if value < 0 else 0 for value in returns)) / len(returns)
        if returns else 0.0
    )
    first_close = current[0].close
    slope = (current[-1].close - first_close) / abs(first_close) if first_close else 0.0
    prior = completed[-min(lookback + 1, len(completed)):-1]
    latest = completed[-1]
    breakout = bool(
        prior
        and (
            latest.close > max(item.high for item in prior)
            or latest.close < min(item.low for item in prior)
        )
    )
    compression_ratio = current_reference / max(
        mean(ranges[-min(baseline_window, len(ranges)):]) if ranges else 0.0,
        1e-12,
    )

    scale = max(abs(slope) * 20.0, 0.0)
    scores = {
        "TREND_UP": max(0.0, slope) * 20.0 + directional_persistence,
        "TREND_DOWN": max(0.0, -slope) * 20.0 + directional_persistence,
        "RANGE": max(0.0, 1.0 - directional_persistence) + max(0.0, 0.15 - abs(slope)) * 4.0,
        "BREAKOUT": 2.0 if breakout else 0.0,
        "COMPRESSION": max(0.0, 0.8 - compression_ratio) * 3.0,
        "EXPANSION": max(0.0, volatility_ratio - 1.0) * 2.0,
        "VOLATILE": max(0.0, volatility_ratio - 1.5) * 2.0,
        "QUIET": max(0.0, 0.8 - volatility_ratio),
        "NEWS": 2.0 if news_state == "NEWS" else 0.0,
        "UNKNOWN": 0.0,
    }
    if news_state == "NEWS":
        label = "NEWS"
    elif breakout:
        label = "BREAKOUT"
    elif volatility_ratio >= 2.5:
        label = "VOLATILE"
    elif volatility_ratio >= 1.5:
        label = "EXPANSION"
    elif compression_ratio <= 0.65:
        label = "COMPRESSION"
    elif volatility_ratio <= 0.65:
        label = "QUIET"
    elif directional_persistence >= 0.55 and slope > 0:
        label = "TREND_UP"
    elif directional_persistence >= 0.55 and slope < 0:
        label = "TREND_DOWN"
    else:
        label = "RANGE"
    probabilities = _normalise(scores)
    return MarketRegimeObservation(
        symbol=symbol,
        timeframe=timeframe,
        observed_at_utc=observed.isoformat(),
        label=label,
        probabilities=probabilities,
        confidence=probabilities[label],
        volatility_ratio=volatility_ratio,
        volatility_percentile=volatility_percentile,
        directional_persistence=directional_persistence,
        slope=slope,
        compression_ratio=compression_ratio,
        breakout=breakout,
        news_state=news_state,
        sample_size=len(current),
        source_digest=_digest(current),
    )


def build_market_regime_library(
    candles_by_symbol: Mapping[str, Mapping[str, Sequence[Candle]]],
    *,
    observed_at: datetime,
    lookback: int = 20,
    baseline_window: int = 100,
    news_events: Mapping[tuple[str, str], bool] | None = None,
) -> MarketRegimeLibrary:
    """Build one descriptive regime observation per symbol/timeframe.

    Only bars whose end is not later than ``observed_at`` are used.  News is an
    explicit external annotation; a volatility spike alone is never labelled
    as confirmed news.
    """
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("market regime observation time must be timezone-aware")
    if lookback < 3 or baseline_window < 1:
        raise ValueError("market regime windows are invalid")
    observed = observed_at.astimezone(timezone.utc)
    events = news_events or {}
    observations: list[MarketRegimeObservation] = []
    for symbol, frames in sorted(candles_by_symbol.items()):
        for timeframe, raw_rows in sorted(frames.items()):
            completed = tuple(
                item for item in raw_rows
                if item.end <= observed
            )
            news_state = "NEWS" if events.get((symbol, timeframe), False) else "NOT_OBSERVED"
            observations.append(
                _observe(
                    completed,
                    symbol=symbol,
                    timeframe=timeframe,
                    observed_at=observed,
                    lookback=lookback,
                    baseline_window=baseline_window,
                    news_state=news_state,
                )
            )
    return MarketRegimeLibrary(
        observed_at=observed.isoformat(),
        observations=tuple(observations),
    )
