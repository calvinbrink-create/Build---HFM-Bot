"""Impulse-pullback-continuation observations and historical outcomes."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
from statistics import mean
from typing import Mapping, Sequence

from ..market import Candle


@dataclass(frozen=True)
class ContinuationOutcome:
    symbol: str
    timeframe: str
    direction: str
    impulse: bool
    pullback: bool
    resumed: bool
    invalidated: bool
    follow_through: bool
    forward_return: float
    forward_horizon: int
    event_at_utc: str
    source_digest: str


@dataclass(frozen=True)
class ContinuationContext:
    symbol: str
    timeframe: str
    present: bool
    direction: str | None
    impulse: bool
    pullback: bool
    resumed: bool
    invalidated: bool
    historical_sample_size: int
    follow_through_count: int
    follow_through_probability: float | None
    forward_horizon: int
    observed_at_utc: str
    source_digest: str
    source: str = "IMPULSE_PULLBACK_CONTINUATION"


@dataclass(frozen=True)
class ContinuationLibrary:
    observed_at: str
    current: tuple[ContinuationContext, ...]
    outcomes: tuple[ContinuationOutcome, ...]
    source: str = "CONTINUATION_OUTCOME_LIBRARY"

    def observations_for_symbol(self, symbol: str) -> Mapping[str, ContinuationContext]:
        return {item.timeframe: item for item in self.current if item.symbol == symbol}

    def outcomes_for_symbol(self, symbol: str) -> tuple[ContinuationOutcome, ...]:
        return tuple(item for item in self.outcomes if item.symbol == symbol)


def _digest(rows: Sequence[Candle]) -> str:
    return sha256("|".join(item.source_id for item in rows).encode("utf-8")).hexdigest()


def _detect(
    symbol: str,
    timeframe: str,
    rows: Sequence[Candle],
    index: int,
    lookback: int,
    observed: datetime,
) -> ContinuationContext:
    current = rows[index]
    window = rows[index - lookback:index]
    ranges = tuple(max(item.high - item.low, 1e-12) for item in window)
    average_range = mean(ranges) if ranges else 1e-12
    impulse_start = rows[index - 8].close
    impulse_end = rows[index - 3].close
    pullback_start = rows[index - 3].close
    pullback_end = rows[index - 1].close
    impulse_move = impulse_end - impulse_start
    pullback_move = pullback_end - pullback_start
    up_impulse = impulse_move >= average_range
    down_impulse = impulse_move <= -average_range
    up_pullback = pullback_move <= -average_range * 0.25
    down_pullback = pullback_move >= average_range * 0.25
    up_resumed = current.close > max(item.high for item in rows[index - 2:index])
    down_resumed = current.close < min(item.low for item in rows[index - 2:index])
    up_invalidated = current.close < min(item.low for item in rows[index - 3:index])
    down_invalidated = current.close > max(item.high for item in rows[index - 3:index])
    if up_impulse and up_pullback and up_resumed:
        direction = "UP"
        impulse, pullback, resumed, invalidated = True, True, True, up_invalidated
    elif down_impulse and down_pullback and down_resumed:
        direction = "DOWN"
        impulse, pullback, resumed, invalidated = True, True, True, down_invalidated
    else:
        direction = None
        impulse = up_impulse or down_impulse
        pullback = (up_impulse and up_pullback) or (down_impulse and down_pullback)
        resumed = False
        invalidated = up_invalidated if up_impulse else down_invalidated if down_impulse else False
    return ContinuationContext(
        symbol=symbol,
        timeframe=timeframe,
        present=direction is not None,
        direction=direction,
        impulse=impulse,
        pullback=pullback,
        resumed=resumed,
        invalidated=invalidated,
        historical_sample_size=0,
        follow_through_count=0,
        follow_through_probability=None,
        forward_horizon=0,
        observed_at_utc=(observed if index == len(rows) - 1 else current.end).astimezone(timezone.utc).isoformat(),
        source_digest=_digest(rows[index - lookback:index + 1]),
    )


def build_continuation_library(
    candles_by_symbol: Mapping[str, Mapping[str, Sequence[Candle]]],
    *,
    observed_at: datetime,
    lookback: int = 20,
    forward_horizon: int = 5,
) -> ContinuationLibrary:
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("continuation observation time must be timezone-aware")
    if lookback < 8 or forward_horizon < 1:
        raise ValueError("continuation parameters are invalid")
    observed = observed_at.astimezone(timezone.utc)
    current: list[ContinuationContext] = []
    outcomes: list[ContinuationOutcome] = []
    for symbol, frames in sorted(candles_by_symbol.items()):
        for timeframe, raw_rows in sorted(frames.items()):
            rows = tuple(sorted(
                (item for item in raw_rows if item.end <= observed),
                key=lambda item: item.start,
            ))
            if len(rows) < lookback + forward_horizon + 1:
                continue
            events: list[ContinuationOutcome] = []
            for index in range(lookback, len(rows) - forward_horizon):
                context = _detect(symbol, timeframe, rows, index, lookback, observed)
                if not context.present:
                    continue
                future = rows[index + 1:index + 1 + forward_horizon]
                entry = rows[index].close
                threshold = max(rows[index].high - rows[index].low, 1e-12) * 0.5
                if context.direction == "UP":
                    follow = max(item.close for item in future) >= entry + threshold
                else:
                    follow = min(item.close for item in future) <= entry - threshold
                forward_return = (future[-1].close - entry) / abs(entry) if entry else 0.0
                events.append(
                    ContinuationOutcome(
                        symbol, timeframe, context.direction or "UNKNOWN", context.impulse,
                        context.pullback, context.resumed, context.invalidated, follow,
                        forward_return, forward_horizon,
                        rows[index].end.astimezone(timezone.utc).isoformat(),
                        _digest(rows[index - lookback:index + 1 + forward_horizon]),
                    )
                )
            outcomes.extend(events)
            current_context = _detect(symbol, timeframe, rows, len(rows) - 1, lookback, observed)
            comparable = [
                item for item in events
                if current_context.present and item.direction == current_context.direction
            ]
            count = sum(item.follow_through for item in comparable)
            probability = (count + 1.0) / (len(comparable) + 2.0) if comparable else None
            current.append(
                replace(
                    current_context,
                    historical_sample_size=len(comparable),
                    follow_through_count=count,
                    follow_through_probability=probability,
                    forward_horizon=forward_horizon,
                )
            )
    return ContinuationLibrary(
        observed_at=observed.isoformat(),
        current=tuple(current),
        outcomes=tuple(outcomes),
    )
