"""Structural breakout detection over completed candles."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from typing import Mapping, Sequence

from ..market import Candle


@dataclass(frozen=True)
class StructuralBreakout:
    symbol: str
    timeframe: str
    present: bool
    direction: str | None
    level: float | None
    close_distance: float | None
    range_ratio: float | None
    activity_ratio: float | None
    observed_at_utc: str
    source_digest: str
    source: str = "PRIOR_STRUCTURE_HIGH_LOW_BREAKOUT"


@dataclass(frozen=True)
class BreakoutEngineLibrary:
    observed_at: str
    current: tuple[StructuralBreakout, ...]
    history: tuple[StructuralBreakout, ...]
    source: str = "STRUCTURAL_BREAKOUT_ENGINE_LIBRARY"

    def observations_for_symbol(self, symbol: str) -> Mapping[str, StructuralBreakout]:
        return {
            item.timeframe: item
            for item in self.current
            if item.symbol == symbol
        }

    def history_for_symbol(self, symbol: str) -> tuple[StructuralBreakout, ...]:
        return tuple(item for item in self.history if item.symbol == symbol)


def _digest(rows: Sequence[Candle]) -> str:
    source = "|".join(item.source_id for item in rows)
    return sha256(source.encode("utf-8")).hexdigest()


def _event(
    symbol: str,
    timeframe: str,
    rows: Sequence[Candle],
    index: int,
    *,
    lookback: int,
    observed_at: datetime,
) -> StructuralBreakout:
    current = rows[index]
    prior = rows[index - 1]
    prior_rows = rows[index - lookback:index]
    high = max(item.high for item in prior_rows)
    low = min(item.low for item in prior_rows)
    direction: str | None = None
    level: float | None = None
    if prior.close <= high < current.close:
        direction, level = "UP", high
    elif prior.close >= low > current.close:
        direction, level = "DOWN", low
    current_range = max(current.high - current.low, 1e-12)
    prior_ranges = tuple(max(item.high - item.low, 1e-12) for item in prior_rows)
    baseline_range = sum(prior_ranges) / len(prior_ranges)
    activity = current.tick_volume
    baseline_activity = sum(item.tick_volume for item in prior_rows) / len(prior_rows)
    return StructuralBreakout(
        symbol=symbol,
        timeframe=timeframe,
        present=direction is not None,
        direction=direction,
        level=level,
        close_distance=abs(current.close - level) if level is not None else None,
        range_ratio=current_range / baseline_range,
        activity_ratio=activity / baseline_activity if baseline_activity else None,
        observed_at_utc=(rows[index].end if index != len(rows) - 1 else observed_at).astimezone(timezone.utc).isoformat(),
        source_digest=_digest(rows[index - lookback:index + 1]),
    )


def build_breakout_engine_library(
    candles_by_symbol: Mapping[str, Mapping[str, Sequence[Candle]]],
    *,
    observed_at: datetime,
    lookback: int = 20,
) -> BreakoutEngineLibrary:
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("breakout observation time must be timezone-aware")
    if lookback < 2:
        raise ValueError("breakout lookback must contain at least two candles")
    observed = observed_at.astimezone(timezone.utc)
    current: list[StructuralBreakout] = []
    history: list[StructuralBreakout] = []
    for symbol, frames in sorted(candles_by_symbol.items()):
        for timeframe, raw_rows in sorted(frames.items()):
            rows = tuple(sorted(
                (item for item in raw_rows if item.end <= observed),
                key=lambda item: item.start,
            ))
            if len(rows) < lookback + 1:
                continue
            for index in range(lookback, len(rows)):
                item = _event(
                    symbol, timeframe, rows, index,
                    lookback=lookback, observed_at=observed,
                )
                if item.present:
                    history.append(item)
            current.append(
                _event(
                    symbol, timeframe, rows, len(rows) - 1,
                    lookback=lookback, observed_at=observed,
                )
            )
    return BreakoutEngineLibrary(
        observed_at=observed.isoformat(),
        current=tuple(current),
        history=tuple(history),
    )
