"""Same-symbol, same-session relative broker tick-volume activity."""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from statistics import mean, median
from typing import Mapping, Sequence

from ..market import Candle
from .session_vwap import SessionVWAPLibrary


@dataclass(frozen=True)
class RelativeVolumeBaseline:
    symbol: str
    session: str
    sample_size: int
    mean_session_tick_volume: float
    median_session_tick_volume: float
    p95_session_tick_volume: float
    source_digest: str
    source: str = "COMPLETED_SAME_SYMBOL_SESSION_TICK_VOLUME_BASELINE"


@dataclass(frozen=True)
class RelativeVolumeObservation:
    symbol: str
    session: str
    session_date: str
    observed_at_utc: str
    candle_count: int
    current_mean_tick_volume: float | None
    baseline_mean_tick_volume: float | None
    baseline_p95_tick_volume: float | None
    relative_volume_ratio: float | None
    baseline_percentile: float | None
    source_digest: str
    source: str = "CURRENT_SESSION_TICK_VOLUME_RELATIVE_TO_COMPLETED_BASELINE"


@dataclass(frozen=True)
class RelativeVolumeLibrary:
    observed_at: str
    baselines: tuple[RelativeVolumeBaseline, ...]
    observations: tuple[RelativeVolumeObservation, ...]
    source: str = "HISTORICAL_RELATIVE_VOLUME_LIBRARY"

    def baselines_for_symbol(self, symbol: str) -> Mapping[str, RelativeVolumeBaseline]:
        return {
            item.session: item for item in self.baselines if item.symbol == symbol
        }

    def observations_for_symbol(self, symbol: str) -> Mapping[str, RelativeVolumeObservation]:
        return {
            item.session: item for item in self.observations if item.symbol == symbol
        }


def build_relative_volume_library(
    candles_by_symbol: Mapping[str, Sequence[Candle]],
    *,
    observed_at: datetime,
    session_vwap_library: SessionVWAPLibrary,
) -> RelativeVolumeLibrary:
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("relative-volume observation time must be timezone-aware")
    canonical_observed_at = observed_at.astimezone(timezone.utc)
    baselines: list[RelativeVolumeBaseline] = []
    observations: list[RelativeVolumeObservation] = []
    for symbol, raw_rows in sorted(candles_by_symbol.items()):
        rows = tuple(sorted(raw_rows, key=lambda item: item.start))
        starts = tuple(item.start for item in rows)
        completed = tuple(
            item for item in session_vwap_library.observations
            if item.symbol == symbol and item.complete_session
        )
        grouped: dict[str, list[tuple[str, float]]] = {}
        for session in completed:
            start = datetime.fromisoformat(session.session_start_utc)
            end = datetime.fromisoformat(session.session_end_utc)
            selected = rows[bisect_left(starts, start):bisect_left(starts, end)]
            values = tuple(max(0.0, float(item.tick_volume)) for item in selected)
            if values:
                grouped.setdefault(session.session, []).append((session.session_date, mean(values)))
        baseline_map: dict[str, RelativeVolumeBaseline] = {}
        for session, values in sorted(grouped.items()):
            samples = tuple(value for _, value in values)
            ordered = tuple(sorted(samples))
            p95_index = min(len(ordered) - 1, max(0, int(round(0.95 * (len(ordered) - 1)))))
            source = "|".join(date for date, _ in values)
            baseline = RelativeVolumeBaseline(
                symbol=symbol,
                session=session,
                sample_size=len(samples),
                mean_session_tick_volume=mean(samples),
                median_session_tick_volume=median(samples),
                p95_session_tick_volume=ordered[p95_index],
                source_digest=sha256(source.encode("utf-8")).hexdigest(),
            )
            baselines.append(baseline)
            baseline_map[session] = baseline
        for session in tuple({item.session for item in session_vwap_library.observations if item.symbol == symbol}):
            current = next(
                (
                    item for item in session_vwap_library.observations
                    if item.symbol == symbol
                    and item.session == session
                    and not item.complete_session
                ),
                None,
            )
            if current is None:
                continue
            start = datetime.fromisoformat(current.session_start_utc)
            through = datetime.fromisoformat(current.observed_through_utc)
            selected = rows[bisect_left(starts, start):bisect_left(starts, through)]
            values = tuple(max(0.0, float(item.tick_volume)) for item in selected)
            baseline = baseline_map.get(session)
            current_mean = mean(values) if values else None
            ratio = current_mean / baseline.mean_session_tick_volume if baseline and current_mean is not None else None
            percentile = None
            if baseline and current_mean is not None:
                history = tuple(
                    mean(max(0.0, float(item.tick_volume)) for item in rows[bisect_left(starts, datetime.fromisoformat(done.session_start_utc)):bisect_left(starts, datetime.fromisoformat(done.session_end_utc))])
                    for done in completed if done.session == session
                )
                percentile = sum(value <= current_mean for value in history) / len(history) if history else None
            source = "|".join(item.source_id for item in selected)
            observations.append(
                RelativeVolumeObservation(
                    symbol=symbol,
                    session=session,
                    session_date=current.session_date,
                    observed_at_utc=canonical_observed_at.isoformat(),
                    candle_count=len(selected),
                    current_mean_tick_volume=current_mean,
                    baseline_mean_tick_volume=baseline.mean_session_tick_volume if baseline else None,
                    baseline_p95_tick_volume=baseline.p95_session_tick_volume if baseline else None,
                    relative_volume_ratio=ratio,
                    baseline_percentile=percentile,
                    source_digest=sha256(source.encode("utf-8")).hexdigest(),
                )
            )
    return RelativeVolumeLibrary(
        observed_at=canonical_observed_at.isoformat(),
        baselines=tuple(baselines),
        observations=tuple(observations),
    )
