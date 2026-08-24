"""Standardized, descriptive trend observations over completed candles."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from typing import Mapping, Sequence

from ..market import Candle
from .advanced import trend_observation


@dataclass(frozen=True)
class TrendEngineObservation:
    symbol: str
    timeframe: str
    direction: str
    slope: float
    normalized_slope: float
    fit: float
    persistence: float
    sample_size: int
    observed_at_utc: str
    source_digest: str
    source: str = "STANDARDIZED_CLOSE_SLOPE_FIT_PERSISTENCE_TREND"


@dataclass(frozen=True)
class TrendEngineLibrary:
    observed_at: str
    observations: tuple[TrendEngineObservation, ...]
    source: str = "STANDARDIZED_TREND_ENGINE_LIBRARY"

    def observations_for_symbol(self, symbol: str) -> Mapping[str, TrendEngineObservation]:
        return {
            item.timeframe: item
            for item in self.observations
            if item.symbol == symbol
        }


def build_trend_engine_library(
    candles_by_symbol: Mapping[str, Mapping[str, Sequence[Candle]]],
    *,
    observed_at: datetime,
    lookback: int = 20,
) -> TrendEngineLibrary:
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("trend observation time must be timezone-aware")
    if lookback < 2:
        raise ValueError("trend lookback must contain at least two candles")
    canonical_observed_at = observed_at.astimezone(timezone.utc)
    observations: list[TrendEngineObservation] = []
    for symbol, frames in sorted(candles_by_symbol.items()):
        for timeframe, raw_rows in sorted(frames.items()):
            rows = tuple(
                sorted(
                    (item for item in raw_rows if item.end <= canonical_observed_at),
                    key=lambda item: item.start,
                )
            )
            base = trend_observation(rows, lookback=lookback)
            source = "|".join(item.source_id for item in rows[-lookback:])
            observations.append(
                TrendEngineObservation(
                    symbol=symbol,
                    timeframe=timeframe,
                    direction=base.direction,
                    slope=base.slope,
                    normalized_slope=base.normalized_slope,
                    fit=base.fit,
                    persistence=base.persistence,
                    sample_size=base.sample_size,
                    observed_at_utc=canonical_observed_at.isoformat(),
                    source_digest=sha256(source.encode("utf-8")).hexdigest(),
                )
            )
    return TrendEngineLibrary(
        observed_at=canonical_observed_at.isoformat(),
        observations=tuple(observations),
    )
