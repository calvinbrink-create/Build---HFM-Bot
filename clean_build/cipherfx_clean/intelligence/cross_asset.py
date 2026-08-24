"""Cross-asset observations, rolling correlations, and decoupling evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from math import sqrt
from statistics import mean
from typing import Mapping, Sequence

from ..market import Candle


@dataclass(frozen=True)
class CrossAssetObservation:
    symbol: str
    timeframe: str
    peer_symbols: tuple[str, ...]
    aligned_sample_size: int
    latest_returns: Mapping[str, float]
    source_digest: str


@dataclass(frozen=True)
class CorrelationObservation:
    symbol: str
    peer_symbol: str
    timeframe: str
    current_correlation: float | None
    baseline_correlation: float | None
    regime: str
    sample_size: int
    source_digest: str


@dataclass(frozen=True)
class CorrelationBreakdownObservation:
    symbol: str
    peer_symbol: str
    timeframe: str
    current_correlation: float | None
    baseline_correlation: float | None
    breakdown: bool
    sign_flip: bool
    source_digest: str


@dataclass(frozen=True)
class CrossAssetLibrary:
    observed_at: str
    observations: tuple[CrossAssetObservation, ...]
    correlations: tuple[CorrelationObservation, ...]
    breakdowns: tuple[CorrelationBreakdownObservation, ...]
    source: str = "CROSS_ASSET_CORRELATION_LIBRARY"

    def observations_for_symbol(self, symbol: str) -> Mapping[str, CrossAssetObservation]:
        return {item.timeframe: item for item in self.observations if item.symbol == symbol}

    def correlations_for_symbol(self, symbol: str) -> Mapping[str, CorrelationObservation]:
        return {
            f"{item.peer_symbol}:{item.timeframe}": item
            for item in self.correlations if item.symbol == symbol
        }

    def breakdowns_for_symbol(self, symbol: str) -> Mapping[str, CorrelationBreakdownObservation]:
        return {
            f"{item.peer_symbol}:{item.timeframe}": item
            for item in self.breakdowns if item.symbol == symbol
        }


@dataclass(frozen=True)
class AssetCorrelation:
    left: str
    right: str
    coefficient: float
    sample_size: int
    baseline_coefficient: float | None
    baseline_sample_size: int
    source: str = "ALIGNED_CLOSE_RETURNS"


def aligned_closes(
    frames: Mapping[str, Sequence[Candle]],
) -> Mapping[str, tuple[tuple[object, float], ...]]:
    """Return timestamp-keyed closes without inventing values for missing bars."""
    return {
        symbol: tuple((item.end, item.close) for item in sorted(rows, key=lambda row: row.end))
        for symbol, rows in frames.items()
    }


def compare_assets(
    closes: Mapping[str, Sequence[tuple[object, float]]],
    *,
    window: int = 30,
    baseline_window: int = 120,
) -> tuple[AssetCorrelation, ...]:
    if window < 3 or baseline_window < 0:
        raise ValueError("correlation windows are invalid")
    output: list[AssetCorrelation] = []
    symbols = tuple(sorted(closes))
    for index, left_symbol in enumerate(symbols):
        left_map = dict(closes[left_symbol])
        for right_symbol in symbols[index + 1:]:
            right_map = dict(closes[right_symbol])
            timestamps = tuple(sorted(set(left_map) & set(right_map)))
            left_values = tuple(left_map[item] for item in timestamps)
            right_values = tuple(right_map[item] for item in timestamps)
            left_returns = tuple(
                (current - previous) / abs(previous)
                for previous, current in zip(left_values, left_values[1:])
                if previous
            )
            right_returns = tuple(
                (current - previous) / abs(previous)
                for previous, current in zip(right_values, right_values[1:])
                if previous
            )
            current_left = left_returns[-window:]
            current_right = right_returns[-window:]
            coefficient = _pearson(current_left, current_right)
            if coefficient is None:
                continue
            base_left = left_returns[-(baseline_window + window):-window] if baseline_window else ()
            base_right = right_returns[-(baseline_window + window):-window] if baseline_window else ()
            baseline = _pearson(base_left, base_right)
            output.append(
                AssetCorrelation(
                    left_symbol,
                    right_symbol,
                    coefficient,
                    min(len(current_left), len(current_right)),
                    baseline,
                    min(len(base_left), len(base_right)),
                )
            )
    return tuple(output)


def contexts_at(
    frames: Mapping[str, Sequence[Candle]],
    *,
    observed_at: datetime,
) -> Mapping[str, Mapping[str, float]]:
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("cross-asset context time must be timezone-aware")
    frozen = {
        symbol: tuple(item for item in rows if item.end <= observed_at)
        for symbol, rows in frames.items()
    }
    observations = compare_assets(aligned_closes(frozen), window=30, baseline_window=120)
    result: dict[str, dict[str, float]] = {
        symbol: {"available": 0.0, "count": 0.0, "max_absolute_correlation": 0.0}
        for symbol in frozen
    }
    for item in observations:
        for symbol in (item.left, item.right):
            state = result[symbol]
            state["count"] += 1.0
            state["max_absolute_correlation"] = max(
                state["max_absolute_correlation"], abs(item.coefficient)
            )
            if item.sample_size >= 3:
                state["available"] = 1.0
    return result


class CrossAssetContextIndex:
    """Timestamp-frozen compatibility index used by memory population and live research."""

    def __init__(self, frames: Mapping[str, Sequence[Candle]]):
        self._frames = {symbol: tuple(rows) for symbol, rows in frames.items()}

    def at(self, observed_at: datetime) -> Mapping[str, Mapping[str, float]]:
        return contexts_at(self._frames, observed_at=observed_at)


def _returns(rows: Sequence[Candle]) -> tuple[float, ...]:
    values = tuple(item.close for item in rows)
    return tuple(
        (current - previous) / abs(previous)
        for previous, current in zip(values, values[1:])
        if previous
    )


def _pearson(left: Sequence[float], right: Sequence[float]) -> float | None:
    count = min(len(left), len(right))
    if count < 3:
        return None
    x = tuple(left[-count:])
    y = tuple(right[-count:])
    x_mean = mean(x)
    y_mean = mean(y)
    numerator = sum((a - x_mean) * (b - y_mean) for a, b in zip(x, y))
    x_var = sum((a - x_mean) ** 2 for a in x)
    y_var = sum((b - y_mean) ** 2 for b in y)
    denominator = sqrt(x_var * y_var)
    return numerator / denominator if denominator else None


def _digest(*rows: Sequence[Candle]) -> str:
    source_ids = [item.source_id for group in rows for item in group]
    return sha256("|".join(source_ids).encode("utf-8")).hexdigest()


def build_cross_asset_library(
    candles_by_symbol: Mapping[str, Mapping[str, Sequence[Candle]]],
    *,
    observed_at: datetime,
    window: int = 60,
    baseline_window: int = 120,
) -> CrossAssetLibrary:
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("cross-asset observation time must be timezone-aware")
    if window < 10 or baseline_window <= window:
        raise ValueError("cross-asset windows are invalid")
    observed = observed_at.astimezone(timezone.utc)
    symbols = tuple(sorted(candles_by_symbol))
    timeframes = tuple(sorted({timeframe for frames in candles_by_symbol.values() for timeframe in frames}))
    observations: list[CrossAssetObservation] = []
    correlations: list[CorrelationObservation] = []
    breakdowns: list[CorrelationBreakdownObservation] = []
    for timeframe in timeframes:
        rows_by_symbol = {
            symbol: tuple(sorted(
                (item for item in candles_by_symbol[symbol].get(timeframe, ()) if item.end <= observed),
                key=lambda item: item.start,
            ))
            for symbol in symbols
        }
        returns_by_symbol = {symbol: _returns(rows) for symbol, rows in rows_by_symbol.items()}
        for symbol in symbols:
            peers = tuple(peer for peer in symbols if peer != symbol and returns_by_symbol[peer])
            aligned = min(
                (len(returns_by_symbol[item]) for item in (symbol,) + peers),
                default=0,
            )
            observations.append(
                CrossAssetObservation(
                    symbol=symbol,
                    timeframe=timeframe,
                    peer_symbols=peers,
                    aligned_sample_size=aligned,
                    latest_returns={
                        peer: returns_by_symbol[peer][-1]
                        for peer in peers if returns_by_symbol[peer]
                    },
                    source_digest=_digest(
                        rows_by_symbol[symbol],
                        *(rows_by_symbol[peer] for peer in peers),
                    ),
                )
            )
            for peer in peers:
                left = returns_by_symbol[symbol]
                right = returns_by_symbol[peer]
                current = _pearson(left[-window:], right[-window:])
                baseline = _pearson(left[-baseline_window:-window], right[-baseline_window:-window])
                if current is None:
                    regime = "INSUFFICIENT_SAMPLE"
                elif baseline is None:
                    regime = "CURRENT_ONLY"
                elif abs(current - baseline) >= 0.25:
                    regime = "CORRELATION_SHIFT"
                else:
                    regime = "STABLE"
                digest = _digest(rows_by_symbol[symbol], rows_by_symbol[peer])
                correlations.append(
                    CorrelationObservation(
                        symbol, peer, timeframe, current, baseline, regime,
                        min(len(left), len(right), window), digest,
                    )
                )
                sign_flip = bool(current is not None and baseline is not None and current * baseline < 0)
                breakdown = bool(
                    sign_flip
                    or (
                        current is not None and baseline is not None
                        and abs(baseline) >= 0.5
                        and abs(current) <= abs(baseline) - 0.25
                    )
                )
                breakdowns.append(
                    CorrelationBreakdownObservation(
                        symbol, peer, timeframe, current, baseline, breakdown, sign_flip, digest
                    )
                )
    return CrossAssetLibrary(
        observed_at=observed.isoformat(),
        observations=tuple(observations),
        correlations=tuple(correlations),
        breakdowns=tuple(breakdowns),
    )
