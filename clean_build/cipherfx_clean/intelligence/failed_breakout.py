"""Persistent records of structural breakouts that failed to follow through."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping

from .breakout_quality_engine import (
    BreakoutQualityOutcome,
    build_breakout_quality_library,
)
from ..market import Candle


@dataclass(frozen=True)
class FailedBreakoutRecord:
    symbol: str
    timeframe: str
    direction: str
    level: float
    failure_reason: str
    forward_return: float
    forward_horizon: int
    event_at_utc: str
    source_digest: str


@dataclass(frozen=True)
class FailedBreakoutSummary:
    symbol: str
    timeframe: str
    failed_count: int
    invalidated_count: int
    no_follow_through_count: int
    latest_failure_at_utc: str | None
    source: str = "FAILED_BREAKOUT_LIBRARY"


@dataclass(frozen=True)
class FailedBreakoutLibrary:
    observed_at: str
    summaries: tuple[FailedBreakoutSummary, ...]
    records: tuple[FailedBreakoutRecord, ...]
    source: str = "FAILED_BREAKOUT_CASE_LIBRARY"

    def observations_for_symbol(self, symbol: str) -> Mapping[str, FailedBreakoutSummary]:
        return {
            item.timeframe: item
            for item in self.summaries
            if item.symbol == symbol
        }

    def records_for_symbol(self, symbol: str) -> tuple[FailedBreakoutRecord, ...]:
        return tuple(item for item in self.records if item.symbol == symbol)


def _record(item: BreakoutQualityOutcome) -> FailedBreakoutRecord:
    invalidated = (
        item.direction == "UP" and item.forward_return < 0
    ) or (
        item.direction == "DOWN" and item.forward_return > 0
    )
    return FailedBreakoutRecord(
        symbol=item.symbol,
        timeframe=item.timeframe,
        direction=item.direction,
        level=item.level,
        failure_reason="LEVEL_INVALIDATED" if invalidated else "NO_FOLLOW_THROUGH",
        forward_return=item.forward_return,
        forward_horizon=item.forward_horizon,
        event_at_utc=item.event_at_utc,
        source_digest=item.source_digest,
    )


def build_failed_breakout_library(
    candles_by_symbol: Mapping[str, Mapping[str, tuple[Candle, ...] | list[Candle]]],
    *,
    observed_at: datetime,
    lookback: int = 20,
    forward_horizon: int = 5,
) -> FailedBreakoutLibrary:
    quality = build_breakout_quality_library(
        candles_by_symbol,
        observed_at=observed_at,
        lookback=lookback,
        forward_horizon=forward_horizon,
    )
    records = tuple(_record(item) for item in quality.outcomes if not item.genuine)
    summaries: list[FailedBreakoutSummary] = []
    keys = sorted({(item.symbol, item.timeframe) for item in quality.outcomes})
    for symbol, timeframe in keys:
        rows = tuple(item for item in records if item.symbol == symbol and item.timeframe == timeframe)
        invalidated = sum(item.failure_reason == "LEVEL_INVALIDATED" for item in rows)
        no_follow = sum(item.failure_reason == "NO_FOLLOW_THROUGH" for item in rows)
        summaries.append(
            FailedBreakoutSummary(
                symbol=symbol,
                timeframe=timeframe,
                failed_count=len(rows),
                invalidated_count=invalidated,
                no_follow_through_count=no_follow,
                latest_failure_at_utc=rows[-1].event_at_utc if rows else None,
            )
        )
    return FailedBreakoutLibrary(
        observed_at=observed_at.astimezone(timezone.utc).isoformat(),
        summaries=tuple(summaries),
        records=records,
    )
