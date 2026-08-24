"""Broker-provided tick-volume activity observations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from statistics import mean, median
from typing import Mapping, Sequence

from ..market import Candle


@dataclass(frozen=True)
class TickVolumeObservation:
    symbol: str
    timeframe: str
    observed_at_utc: str
    candle_count: int
    nonzero_candle_count: int
    coverage_ratio: float
    total_tick_volume: float
    mean_tick_volume: float | None
    median_tick_volume: float | None
    latest_tick_volume: float | None
    source_digest: str
    source: str = "HFM_BROKER_CANDLE_TICK_VOLUME_ACTIVITY_PROXY"


@dataclass(frozen=True)
class TickVolumeLibrary:
    observed_at: str
    observations: tuple[TickVolumeObservation, ...]
    source: str = "HISTORICAL_AND_CURRENT_BROKER_TICK_VOLUME_LIBRARY"

    def observations_for_symbol(self, symbol: str) -> Mapping[str, TickVolumeObservation]:
        return {
            item.timeframe: item
            for item in self.observations
            if item.symbol == symbol
        }


def build_tick_volume_library(
    candles_by_symbol: Mapping[str, Mapping[str, Sequence[Candle]]],
    *,
    observed_at: datetime,
) -> TickVolumeLibrary:
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("tick-volume observation time must be timezone-aware")
    canonical_observed_at = observed_at.astimezone(timezone.utc)
    observations: list[TickVolumeObservation] = []
    for symbol, frames in sorted(candles_by_symbol.items()):
        for timeframe, raw_rows in sorted(frames.items()):
            rows = tuple(sorted((row for row in raw_rows if row.end <= canonical_observed_at), key=lambda item: item.start))
            values = tuple(max(0.0, float(item.tick_volume)) for item in rows)
            source = "|".join(item.source_id for item in rows)
            observations.append(
                TickVolumeObservation(
                    symbol=symbol,
                    timeframe=timeframe,
                    observed_at_utc=canonical_observed_at.isoformat(),
                    candle_count=len(rows),
                    nonzero_candle_count=sum(value > 0.0 for value in values),
                    coverage_ratio=sum(value > 0.0 for value in values) / len(values) if values else 0.0,
                    total_tick_volume=sum(values),
                    mean_tick_volume=mean(values) if values else None,
                    median_tick_volume=median(values) if values else None,
                    latest_tick_volume=values[-1] if values else None,
                    source_digest=sha256(source.encode("utf-8")).hexdigest(),
                )
            )
    return TickVolumeLibrary(
        observed_at=canonical_observed_at.isoformat(),
        observations=tuple(observations),
    )
