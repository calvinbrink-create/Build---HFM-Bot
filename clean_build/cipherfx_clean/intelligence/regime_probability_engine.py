"""Historical probability distribution for market regimes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import sqrt
from typing import Mapping, Sequence

from ..market import Candle
from .market_regime_engine import (
    REGIME_LABELS,
    MarketRegimeObservation,
    _digest,
    _observe,
    build_market_regime_library,
)


@dataclass(frozen=True)
class RegimeProbabilityObservation:
    symbol: str
    timeframe: str
    observed_at_utc: str
    current_label: str
    probabilities: Mapping[str, float]
    counts: Mapping[str, int]
    sample_size: int
    interval_low: float
    interval_high: float
    source_digest: str
    source: str = "HISTORICAL_MARKET_REGIME_PROBABILITY"


@dataclass(frozen=True)
class RegimeProbabilityLibrary:
    observed_at: str
    observations: tuple[RegimeProbabilityObservation, ...]
    source: str = "REGIME_PROBABILITY_LIBRARY"

    def observations_for_symbol(self, symbol: str) -> Mapping[str, RegimeProbabilityObservation]:
        return {
            item.timeframe: item
            for item in self.observations
            if item.symbol == symbol
        }


def _wilson(successes: int, total: int) -> tuple[float, float]:
    if total <= 0:
        return 0.0, 0.0
    z = 1.959963984540054
    p = successes / total
    denominator = 1.0 + z * z / total
    centre = p + z * z / (2.0 * total)
    spread = z * sqrt((p * (1.0 - p) / total) + z * z / (4.0 * total * total))
    return max(0.0, (centre - spread) / denominator), min(1.0, (centre + spread) / denominator)


def build_regime_probability_library(
    candles_by_symbol: Mapping[str, Mapping[str, Sequence[Candle]]],
    *,
    observed_at: datetime,
    lookback: int = 20,
    baseline_window: int = 100,
    max_anchors: int = 100,
) -> RegimeProbabilityLibrary:
    """Estimate regime frequencies from prior completed states only.

    The latest state is scored separately.  Historical anchors end before the
    latest completed candle, so current information cannot be counted as its
    own historical evidence.
    """
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("regime probability observation time must be timezone-aware")
    if lookback < 3 or baseline_window < 1 or max_anchors < 1:
        raise ValueError("regime probability parameters are invalid")
    observed = observed_at.astimezone(timezone.utc)
    current_library = build_market_regime_library(
        candles_by_symbol,
        observed_at=observed,
        lookback=lookback,
        baseline_window=baseline_window,
    )
    output: list[RegimeProbabilityObservation] = []
    for symbol, frames in sorted(candles_by_symbol.items()):
        current_by_timeframe = current_library.observations_for_symbol(symbol)
        for timeframe, raw_rows in sorted(frames.items()):
            rows = tuple(sorted((row for row in raw_rows if row.end <= observed), key=lambda row: row.start))
            prior = rows[:-1]
            counts = {label: 0 for label in REGIME_LABELS}
            if len(prior) >= lookback:
                available = len(prior) - lookback + 1
                step = max(1, (available + max_anchors - 1) // max_anchors)
                for anchor in range(lookback, len(prior) + 1, step):
                    anchor_start = max(0, anchor - (baseline_window + lookback + 1))
                    state = _observe(
                        prior[anchor_start:anchor],
                        symbol=symbol,
                        timeframe=timeframe,
                        observed_at=prior[anchor - 1].end,
                        lookback=lookback,
                        baseline_window=baseline_window,
                        news_state="NOT_OBSERVED",
                    )
                    counts[state.label] += 1
            sample_size = sum(counts.values())
            current: MarketRegimeObservation = current_by_timeframe[timeframe]
            if sample_size:
                denominator = sample_size + len(REGIME_LABELS)
                probabilities = {
                    label: (counts[label] + 1) / denominator
                    for label in REGIME_LABELS
                }
                interval_low, interval_high = _wilson(
                    counts[current.label], sample_size
                )
            else:
                probabilities = dict(current.probabilities)
                interval_low, interval_high = 0.0, 0.0
            output.append(
                RegimeProbabilityObservation(
                    symbol=symbol,
                    timeframe=timeframe,
                    observed_at_utc=observed.isoformat(),
                    current_label=current.label,
                    probabilities=probabilities,
                    counts=counts,
                    sample_size=sample_size,
                    interval_low=interval_low,
                    interval_high=interval_high,
                    source_digest=_digest(rows),
                )
            )
    return RegimeProbabilityLibrary(
        observed_at=observed.isoformat(),
        observations=tuple(output),
    )
