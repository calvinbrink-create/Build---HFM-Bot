"""Immutable negative examples for liquidity events that did not reverse."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from typing import Mapping

from .contracts import MarketSnapshot


@dataclass(frozen=True)
class LiquidityFailureRecord:
    failure_id: str
    symbol: str
    timeframe: str
    event_time: datetime
    observation_end_time: datetime
    direction: str
    level: float
    sweep_price: float
    penetration: float
    event_index: int
    observation_end_index: int
    lookback: int
    outcome: str
    reason: str
    reversal_excursion: float
    continuation_excursion: float
    source_candle_id: str


def build_liquidity_failure_records(
    *,
    snapshot: MarketSnapshot,
    sweeps: Mapping[str, tuple[object, ...]],
) -> tuple[LiquidityFailureRecord, ...]:
    records = []
    for timeframe, events in sweeps.items():
        candles = tuple(snapshot.candles.get(timeframe, ()))
        for event in events:
            if event.outcome not in {"CONTINUATION", "UNRESOLVED"}:
                continue
            if not 0 <= event.index <= event.observation_end_index < len(candles):
                raise ValueError("liquidity failure observation indices are invalid")
            event_candle = candles[event.index]
            observed = candles[event.index:event.observation_end_index + 1]
            if not event_candle.source_id:
                raise ValueError("liquidity failure requires source-candle identity")
            if event.direction == "UP":
                reversal_excursion = max(0.0, event.sweep_price - min(row.low for row in observed))
                continuation_excursion = max(0.0, max(row.high for row in observed) - event.sweep_price)
                continuation_reason = "ACCEPTANCE_ABOVE_SWEPT_HIGH"
            elif event.direction == "DOWN":
                reversal_excursion = max(0.0, max(row.high for row in observed) - event.sweep_price)
                continuation_excursion = max(0.0, event.sweep_price - min(row.low for row in observed))
                continuation_reason = "ACCEPTANCE_BELOW_SWEPT_LOW"
            else:
                raise ValueError("liquidity failure direction is invalid")
            reason = continuation_reason if event.outcome == "CONTINUATION" else "NO_RECLAIM_WITHIN_OBSERVATION_WINDOW"
            identity = {
                "symbol": snapshot.symbol,
                "timeframe": timeframe,
                "source_candle_id": event_candle.source_id,
                "direction": event.direction,
                "level": event.level,
                "sweep_price": event.sweep_price,
                "lookback": event.lookback,
            }
            failure_id = sha256(
                json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            records.append(
                LiquidityFailureRecord(
                    failure_id=failure_id,
                    symbol=snapshot.symbol,
                    timeframe=str(timeframe),
                    event_time=event_candle.end,
                    observation_end_time=candles[event.observation_end_index].end,
                    direction=event.direction,
                    level=event.level,
                    sweep_price=event.sweep_price,
                    penetration=event.penetration,
                    event_index=event.index,
                    observation_end_index=event.observation_end_index,
                    lookback=event.lookback,
                    outcome=event.outcome,
                    reason=reason,
                    reversal_excursion=reversal_excursion,
                    continuation_excursion=continuation_excursion,
                    source_candle_id=event_candle.source_id,
                )
            )
    return tuple(records)
