"""Outcome-based quality statistics for structural breakout events."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from typing import Mapping, Sequence

from ..market import Candle
from .breakout_engine import StructuralBreakout, _event


@dataclass(frozen=True)
class BreakoutQualityOutcome:
    symbol: str
    timeframe: str
    direction: str
    level: float
    range_ratio: float
    activity_ratio: float | None
    forward_return: float
    genuine: bool
    forward_horizon: int
    event_at_utc: str
    source_digest: str


@dataclass(frozen=True)
class BreakoutQualityContext:
    symbol: str
    timeframe: str
    present: bool
    direction: str | None
    quality_probability: float | None
    comparable_sample_size: int
    genuine_count: int
    forward_horizon: int
    observed_at_utc: str
    source_digest: str
    source: str = "OUTCOME_BASED_BREAKOUT_QUALITY"


@dataclass(frozen=True)
class BreakoutQualityLibrary:
    observed_at: str
    current: tuple[BreakoutQualityContext, ...]
    outcomes: tuple[BreakoutQualityOutcome, ...]
    source: str = "BREAKOUT_QUALITY_STATISTICS_LIBRARY"

    def observations_for_symbol(self, symbol: str) -> Mapping[str, BreakoutQualityContext]:
        return {
            item.timeframe: item
            for item in self.current
            if item.symbol == symbol
        }

    def outcomes_for_symbol(self, symbol: str) -> tuple[BreakoutQualityOutcome, ...]:
        return tuple(item for item in self.outcomes if item.symbol == symbol)


def _digest(rows: Sequence[Candle]) -> str:
    source = "|".join(item.source_id for item in rows)
    return sha256(source.encode("utf-8")).hexdigest()


def _quality_outcome(
    symbol: str,
    timeframe: str,
    rows: Sequence[Candle],
    index: int,
    event: StructuralBreakout,
    *,
    forward_horizon: int,
) -> BreakoutQualityOutcome:
    future = rows[index + 1:index + 1 + forward_horizon]
    entry = rows[index].close
    baseline_range = sum(
        max(item.high - item.low, 1e-12)
        for item in rows[index - 20:index]
    ) / 20.0
    threshold = max(baseline_range * 0.5, 1e-12)
    if event.direction == "UP":
        continuation = max(item.close for item in future) >= event.level + threshold
        invalidation = any(item.close < event.level for item in future)
    else:
        continuation = min(item.close for item in future) <= event.level - threshold
        invalidation = any(item.close > event.level for item in future)
    genuine = continuation and not invalidation
    forward_return = (future[-1].close - entry) / abs(entry) if entry else 0.0
    return BreakoutQualityOutcome(
        symbol=symbol,
        timeframe=timeframe,
        direction=event.direction or "UNKNOWN",
        level=event.level or entry,
        range_ratio=event.range_ratio or 0.0,
        activity_ratio=event.activity_ratio,
        forward_return=forward_return,
        genuine=genuine,
        forward_horizon=forward_horizon,
        event_at_utc=rows[index].end.astimezone(timezone.utc).isoformat(),
        source_digest=_digest(rows[index - 20:index + 1 + forward_horizon]),
    )


def build_breakout_quality_library(
    candles_by_symbol: Mapping[str, Mapping[str, Sequence[Candle]]],
    *,
    observed_at: datetime,
    lookback: int = 20,
    forward_horizon: int = 5,
) -> BreakoutQualityLibrary:
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("breakout quality observation time must be timezone-aware")
    if lookback != 20 or forward_horizon < 1:
        raise ValueError("breakout quality requires a 20-candle lookback")
    observed = observed_at.astimezone(timezone.utc)
    current: list[BreakoutQualityContext] = []
    outcomes: list[BreakoutQualityOutcome] = []
    for symbol, frames in sorted(candles_by_symbol.items()):
        for timeframe, raw_rows in sorted(frames.items()):
            rows = tuple(sorted(
                (item for item in raw_rows if item.end <= observed),
                key=lambda item: item.start,
            ))
            if len(rows) < lookback + forward_horizon + 1:
                continue
            event_rows: list[tuple[int, StructuralBreakout]] = []
            for index in range(lookback, len(rows) - forward_horizon):
                event = _event(
                    symbol, timeframe, rows, index,
                    lookback=lookback, observed_at=observed,
                )
                if event.present:
                    event_rows.append((index, event))
                    outcomes.append(
                        _quality_outcome(
                            symbol, timeframe, rows, index, event,
                            forward_horizon=forward_horizon,
                        )
                    )
            current_event = _event(
                symbol, timeframe, rows, len(rows) - 1,
                lookback=lookback, observed_at=observed,
            )
            comparable: list[BreakoutQualityOutcome] = []
            if current_event.present:
                comparable = [
                    item for item in outcomes
                    if item.symbol == symbol
                    and item.timeframe == timeframe
                    and item.direction == current_event.direction
                    and abs(item.range_ratio - (current_event.range_ratio or 0.0)) <= 0.5
                    and (
                        current_event.activity_ratio is None
                        or item.activity_ratio is None
                        or abs(item.activity_ratio - current_event.activity_ratio) <= 0.75
                    )
                ]
                if len(comparable) < 10:
                    comparable = [
                        item for item in outcomes
                        if item.symbol == symbol
                        and item.timeframe == timeframe
                        and item.direction == current_event.direction
                    ]
            probability = None
            genuine_count = 0
            if comparable:
                genuine_count = sum(item.genuine for item in comparable)
                probability = (genuine_count + 1.0) / (len(comparable) + 2.0)
            current.append(
                BreakoutQualityContext(
                    symbol=symbol,
                    timeframe=timeframe,
                    present=current_event.present,
                    direction=current_event.direction,
                    quality_probability=probability,
                    comparable_sample_size=len(comparable),
                    genuine_count=genuine_count,
                    forward_horizon=forward_horizon,
                    observed_at_utc=observed.isoformat(),
                    source_digest=_digest(rows[-lookback - 1:]),
                )
            )
    return BreakoutQualityLibrary(
        observed_at=observed.isoformat(),
        current=tuple(current),
        outcomes=tuple(outcomes),
    )
