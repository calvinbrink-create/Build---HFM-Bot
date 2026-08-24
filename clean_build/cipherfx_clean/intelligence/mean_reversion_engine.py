"""Statistical mean-reversion observations over completed broker candles."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from math import isfinite
from statistics import mean, pstdev
from typing import Mapping, Sequence

from ..market import Candle
from .advanced import mean_reversion_observation


@dataclass(frozen=True)
class MeanReversionObservation:
    symbol: str
    timeframe: str
    value: float
    reference: float | None
    scale: float | None
    zscore: float | None
    state: str
    stretched: bool
    historical_stretch_count: int
    historical_reversion_count: int
    reversion_probability: float | None
    forward_horizon: int
    observed_at_utc: str
    source_digest: str
    source: str = "STATISTICAL_MEAN_REVERSION_ENGINE"


@dataclass(frozen=True)
class MeanReversionLibrary:
    observed_at: str
    observations: tuple[MeanReversionObservation, ...]
    source: str = "MEAN_REVERSION_STATISTICS_LIBRARY"

    def observations_for_symbol(self, symbol: str) -> Mapping[str, MeanReversionObservation]:
        return {
            item.timeframe: item
            for item in self.observations
            if item.symbol == symbol
        }


def _digest(rows: Sequence[Candle]) -> str:
    source = "|".join(item.source_id for item in rows)
    return sha256(source.encode("utf-8")).hexdigest()


def _state(zscore: float | None, threshold: float) -> str:
    if zscore is None:
        return "UNAVAILABLE"
    if zscore >= threshold:
        return "STRETCHED_HIGH"
    if zscore <= -threshold:
        return "STRETCHED_LOW"
    return "WITHIN_RANGE"


def build_mean_reversion_library(
    candles_by_symbol: Mapping[str, Mapping[str, Sequence[Candle]]],
    *,
    observed_at: datetime,
    lookback: int = 20,
    forward_horizon: int = 5,
    zscore_threshold: float = 2.0,
    max_anchors: int = 5000,
) -> MeanReversionLibrary:
    """Measure current stretch and historical return-toward-reference outcomes.

    The reference window ends before the evaluated candle.  Historical outcome
    windows are used only for prior anchors with enough later candles; the
    current observation never receives a future outcome.
    """
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("mean-reversion observation time must be timezone-aware")
    if lookback < 2 or forward_horizon < 1 or zscore_threshold <= 0 or max_anchors < 1:
        raise ValueError("mean-reversion parameters are invalid")
    observed = observed_at.astimezone(timezone.utc)
    observations: list[MeanReversionObservation] = []
    for symbol, frames in sorted(candles_by_symbol.items()):
        for timeframe, raw_rows in sorted(frames.items()):
            rows = tuple(sorted(
                (item for item in raw_rows if item.end <= observed),
                key=lambda item: item.start,
            ))
            if len(rows) < lookback + 1:
                continue
            baseline = tuple(item.close for item in rows[-lookback - 1:-1])
            current_value = rows[-1].close
            reference = mean(baseline)
            scale = pstdev(baseline) if len(baseline) > 1 else 0.0
            current_zscore = (current_value - reference) / scale if scale else None
            current_state = mean_reversion_observation(
                current_value, reference, scale
            )
            historical_events = 0
            historical_reversions = 0
            step = max(1, (len(rows) - lookback - forward_horizon) // max_anchors)
            for anchor in range(lookback, len(rows) - forward_horizon, step):
                window = tuple(item.close for item in rows[anchor - lookback:anchor])
                anchor_reference = mean(window)
                anchor_scale = pstdev(window) if len(window) > 1 else 0.0
                if not anchor_scale:
                    continue
                anchor_value = rows[anchor].close
                anchor_zscore = (anchor_value - anchor_reference) / anchor_scale
                if abs(anchor_zscore) < zscore_threshold:
                    continue
                historical_events += 1
                future_value = rows[anchor + forward_horizon - 1].close
                moved_toward = abs(future_value - anchor_reference) < abs(anchor_value - anchor_reference)
                historical_reversions += int(moved_toward)
            probability = None
            if historical_events:
                probability = (historical_reversions + 1.0) / (historical_events + 2.0)
            observations.append(
                MeanReversionObservation(
                    symbol=symbol,
                    timeframe=timeframe,
                    value=current_value,
                    reference=reference,
                    scale=scale,
                    zscore=current_zscore,
                    state=_state(current_zscore, zscore_threshold),
                    stretched=current_state.stretched,
                    historical_stretch_count=historical_events,
                    historical_reversion_count=historical_reversions,
                    reversion_probability=probability,
                    forward_horizon=forward_horizon,
                    observed_at_utc=observed.isoformat(),
                    source_digest=_digest(rows[-lookback - 1:]),
                )
            )
    return MeanReversionLibrary(
        observed_at=observed.isoformat(),
        observations=tuple(observations),
    )
