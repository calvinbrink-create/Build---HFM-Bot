"""Liquidity-sweep reversal observations and historical follow-through."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
from typing import Mapping, Sequence

from ..market import Candle


@dataclass(frozen=True)
class ReversalOutcome:
    symbol: str
    timeframe: str
    direction: str
    sweep: bool
    exhaustion: bool
    rejection: bool
    reclaim: bool
    follow_through: bool
    forward_return: float
    forward_horizon: int
    event_at_utc: str
    source_digest: str


@dataclass(frozen=True)
class ReversalContext:
    symbol: str
    timeframe: str
    present: bool
    direction: str | None
    sweep: bool
    exhaustion: bool
    rejection: bool
    reclaim: bool
    historical_sample_size: int
    follow_through_count: int
    follow_through_probability: float | None
    forward_horizon: int
    observed_at_utc: str
    source_digest: str
    source: str = "SWEEP_EXHAUSTION_REJECTION_RECLAIM_REVERSAL"


@dataclass(frozen=True)
class ReversalLibrary:
    observed_at: str
    current: tuple[ReversalContext, ...]
    outcomes: tuple[ReversalOutcome, ...]
    source: str = "REVERSAL_OUTCOME_LIBRARY"

    def observations_for_symbol(self, symbol: str) -> Mapping[str, ReversalContext]:
        return {
            item.timeframe: item
            for item in self.current
            if item.symbol == symbol
        }

    def outcomes_for_symbol(self, symbol: str) -> tuple[ReversalOutcome, ...]:
        return tuple(item for item in self.outcomes if item.symbol == symbol)


def _digest(rows: Sequence[Candle]) -> str:
    return sha256("|".join(item.source_id for item in rows).encode("utf-8")).hexdigest()


def _detect(symbol: str, timeframe: str, rows: Sequence[Candle], index: int, lookback: int, observed: datetime) -> ReversalContext:
    current = rows[index]
    prior = rows[index - lookback:index]
    prior_high = max(item.high for item in prior)
    prior_low = min(item.low for item in prior)
    body = abs(current.close - current.open)
    lower_wick = max(0.0, min(current.open, current.close) - current.low)
    upper_wick = max(0.0, current.high - max(current.open, current.close))
    bullish_sweep = current.low < prior_low and current.close > prior_low
    bearish_sweep = current.high > prior_high and current.close < prior_high
    bullish_rejection = lower_wick >= max(body * 1.5, 1e-12)
    bearish_rejection = upper_wick >= max(body * 1.5, 1e-12)
    bullish_exhaustion = lower_wick >= max(body * 2.0, 1e-12)
    bearish_exhaustion = upper_wick >= max(body * 2.0, 1e-12)
    bullish_reclaim = current.close > prior_low
    bearish_reclaim = current.close < prior_high
    bullish = bullish_sweep and bullish_exhaustion and bullish_rejection and bullish_reclaim
    bearish = bearish_sweep and bearish_exhaustion and bearish_rejection and bearish_reclaim
    direction = "UP" if bullish else "DOWN" if bearish else None
    if direction == "UP":
        sweep = bullish_sweep
        exhaustion = bullish_exhaustion
        rejection = bullish_rejection
        reclaim = bullish_reclaim
    elif direction == "DOWN":
        sweep = bearish_sweep
        exhaustion = bearish_exhaustion
        rejection = bearish_rejection
        reclaim = bearish_reclaim
    else:
        sweep = bullish_sweep or bearish_sweep
        exhaustion = False
        rejection = False
        reclaim = False
    return ReversalContext(
        symbol=symbol,
        timeframe=timeframe,
        present=direction is not None,
        direction=direction,
        sweep=sweep,
        exhaustion=exhaustion,
        rejection=rejection,
        reclaim=reclaim,
        historical_sample_size=0,
        follow_through_count=0,
        follow_through_probability=None,
        forward_horizon=0,
        observed_at_utc=(observed if index == len(rows) - 1 else current.end).astimezone(timezone.utc).isoformat(),
        source_digest=_digest(rows[index - lookback:index + 1]),
    )


def build_reversal_library(
    candles_by_symbol: Mapping[str, Mapping[str, Sequence[Candle]]],
    *,
    observed_at: datetime,
    lookback: int = 20,
    forward_horizon: int = 5,
) -> ReversalLibrary:
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("reversal observation time must be timezone-aware")
    if lookback < 2 or forward_horizon < 1:
        raise ValueError("reversal parameters are invalid")
    observed = observed_at.astimezone(timezone.utc)
    current: list[ReversalContext] = []
    outcomes: list[ReversalOutcome] = []
    for symbol, frames in sorted(candles_by_symbol.items()):
        for timeframe, raw_rows in sorted(frames.items()):
            rows = tuple(sorted(
                (item for item in raw_rows if item.end <= observed),
                key=lambda item: item.start,
            ))
            if len(rows) < lookback + forward_horizon + 1:
                continue
            events: list[ReversalOutcome] = []
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
                    ReversalOutcome(
                        symbol, timeframe, context.direction or "UNKNOWN", context.sweep,
                        context.exhaustion, context.rejection, context.reclaim, follow, forward_return,
                        forward_horizon, rows[index].end.astimezone(timezone.utc).isoformat(),
                        _digest(rows[index - lookback:index + 1 + forward_horizon]),
                    )
                )
            outcomes.extend(events)
            current_context = _detect(symbol, timeframe, rows, len(rows) - 1, lookback, observed)
            comparable = [
                item for item in events
                if current_context.present and item.direction == current_context.direction
            ]
            probability = None
            count = 0
            if comparable:
                count = sum(item.follow_through for item in comparable)
                probability = (count + 1.0) / (len(comparable) + 2.0)
            current.append(
                replace(
                    current_context,
                    historical_sample_size=len(comparable),
                    follow_through_count=count,
                    follow_through_probability=probability,
                    forward_horizon=forward_horizon,
                )
            )
    return ReversalLibrary(
        observed_at=observed.isoformat(),
        current=tuple(current),
        outcomes=tuple(outcomes),
    )
