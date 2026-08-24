"""Historical continuation probabilities for standardized trend states."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from math import sqrt
from typing import Mapping, Sequence

from ..market import Candle
from .advanced import trend_observation
from .trend_engine import TrendEngineObservation


@dataclass(frozen=True)
class TrendPersistenceAnalogue:
    symbol: str
    timeframe: str
    anchor_end_utc: str
    direction: str
    fit: float
    persistence: float
    forward_horizon: int
    forward_return: float
    continued: bool
    source_digest: str


@dataclass(frozen=True)
class TrendPersistenceProbability:
    symbol: str
    timeframe: str
    observed_at_utc: str
    current_direction: str
    current_fit: float
    current_persistence: float
    analogue_count: int
    continuation_count: int
    continuation_probability: float
    interval_low: float
    interval_high: float
    forward_horizon: int
    source_digest: str
    source: str = "HISTORICAL_TREND_ANALOGUE_CONTINUATION_PROBABILITY"


@dataclass(frozen=True)
class TrendPersistenceLibrary:
    observed_at: str
    probabilities: tuple[TrendPersistenceProbability, ...]
    analogues: tuple[TrendPersistenceAnalogue, ...]
    source: str = "TREND_PERSISTENCE_PROBABILITY_LIBRARY"

    def observations_for_symbol(self, symbol: str) -> Mapping[str, TrendPersistenceProbability]:
        return {
            item.timeframe: item
            for item in self.probabilities
            if item.symbol == symbol
        }


def _wilson_interval(successes: int, total: int) -> tuple[float, float]:
    if total <= 0:
        return 0.0, 0.0
    z = 1.959963984540054
    denominator = 1.0 + (z * z / total)
    centre = (successes / total) + (z * z / (2.0 * total))
    spread = z * sqrt(
        (successes / total) * (1.0 - successes / total) / total
        + (z * z / (4.0 * total * total))
    )
    return max(0.0, (centre - spread) / denominator), min(1.0, (centre + spread) / denominator)


def _digest(rows: Sequence[Candle]) -> str:
    source = "|".join(item.source_id for item in rows)
    return sha256(source.encode("utf-8")).hexdigest()


def build_trend_persistence_library(
    candles_by_symbol: Mapping[str, Mapping[str, Sequence[Candle]]],
    *,
    observed_at: datetime,
    lookback: int = 20,
    forward_horizon: int = 5,
    max_anchors: int = 5000,
    fit_tolerance: float = 0.25,
    persistence_tolerance: float = 0.25,
) -> TrendPersistenceLibrary:
    """Build continuation probabilities using only completed historical candles.

    Historical anchors are matched by direction and nearby fit/persistence.  If
    that neighbourhood has too few examples, the model falls back to the same
    direction so sparse instruments remain measurable without inventing data.
    The current observation is never used as an outcome-bearing anchor.
    """
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("trend persistence observation time must be timezone-aware")
    if lookback < 2 or forward_horizon < 1 or max_anchors < 1:
        raise ValueError("trend persistence parameters are invalid")
    observed = observed_at.astimezone(timezone.utc)
    probabilities: list[TrendPersistenceProbability] = []
    analogues: list[TrendPersistenceAnalogue] = []

    for symbol, frames in sorted(candles_by_symbol.items()):
        for timeframe, raw_rows in sorted(frames.items()):
            rows = tuple(sorted(
                (item for item in raw_rows if item.end <= observed),
                key=lambda item: item.start,
            ))
            if len(rows) < lookback + forward_horizon + 1:
                continue
            current_base = trend_observation(rows, lookback=lookback)
            current = TrendEngineObservation(
                symbol=symbol,
                timeframe=timeframe,
                direction=current_base.direction,
                slope=current_base.slope,
                normalized_slope=current_base.normalized_slope,
                fit=current_base.fit,
                persistence=current_base.persistence,
                sample_size=current_base.sample_size,
                observed_at_utc=observed.isoformat(),
                source_digest=_digest(rows[-lookback:]),
            )
            step = max(1, (len(rows) - lookback - forward_horizon) // max_anchors)
            candidates: list[TrendPersistenceAnalogue] = []
            for anchor in range(lookback, len(rows) - forward_horizon, step):
                state = trend_observation(rows[:anchor], lookback=lookback)
                if state.direction not in {"UP", "DOWN"} or state.direction != current.direction:
                    continue
                anchor_close = rows[anchor - 1].close
                future_close = rows[anchor + forward_horizon - 1].close
                if anchor_close == 0:
                    continue
                forward_return = (future_close - anchor_close) / abs(anchor_close)
                continued = (forward_return > 0) if state.direction == "UP" else (forward_return < 0)
                window = rows[anchor - lookback:anchor + forward_horizon]
                candidates.append(
                    TrendPersistenceAnalogue(
                        symbol=symbol,
                        timeframe=timeframe,
                        anchor_end_utc=rows[anchor - 1].end.astimezone(timezone.utc).isoformat(),
                        direction=state.direction,
                        fit=state.fit,
                        persistence=state.persistence,
                        forward_horizon=forward_horizon,
                        forward_return=forward_return,
                        continued=continued,
                        source_digest=_digest(window),
                    )
                )
            neighbourhood = [
                item for item in candidates
                if abs(item.fit - current.fit) <= fit_tolerance
                and abs(item.persistence - current.persistence) <= persistence_tolerance
            ]
            selected = neighbourhood if len(neighbourhood) >= 10 else candidates
            if not selected:
                continue
            successes = sum(item.continued for item in selected)
            probability = (successes + 1.0) / (len(selected) + 2.0)
            interval_low, interval_high = _wilson_interval(successes, len(selected))
            analogues.extend(selected)
            probabilities.append(
                TrendPersistenceProbability(
                    symbol=symbol,
                    timeframe=timeframe,
                    observed_at_utc=observed.isoformat(),
                    current_direction=current.direction,
                    current_fit=current.fit,
                    current_persistence=current.persistence,
                    analogue_count=len(selected),
                    continuation_count=successes,
                    continuation_probability=probability,
                    interval_low=interval_low,
                    interval_high=interval_high,
                    forward_horizon=forward_horizon,
                    source_digest=current.source_digest,
                )
            )
    return TrendPersistenceLibrary(
        observed_at=observed.isoformat(),
        probabilities=tuple(probabilities),
        analogues=tuple(analogues),
    )
