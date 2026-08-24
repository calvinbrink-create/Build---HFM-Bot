"""Completed-bar scheduler with persistent decision-key semantics."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping

from .contracts import MarketSnapshot


@dataclass(frozen=True)
class EvaluationEvent:
    event_id: str
    symbol: str
    timeframe: str
    completed_at: datetime


class CompletionScheduler:
    def __init__(self, last_completed: Mapping[tuple[str, str], datetime] | None = None):
        self._last = dict(last_completed or {})

    def ready(self, snapshot: MarketSnapshot) -> tuple[EvaluationEvent, ...]:
        output = []
        for timeframe, rows in snapshot.candles.items():
            if not rows or snapshot.frame_status.get(timeframe) != "COMPLETED":
                continue
            completed_at = rows[-1].end
            key = (snapshot.symbol, timeframe)
            if completed_at <= self._last.get(key, datetime.min.replace(tzinfo=completed_at.tzinfo)):
                continue
            output.append(EvaluationEvent(f"{snapshot.symbol}:{timeframe}:{completed_at.isoformat()}", snapshot.symbol, timeframe, completed_at))
            self._last[key] = completed_at
        return tuple(output)

    def state(self) -> Mapping[tuple[str, str], datetime]:
        return dict(self._last)

    def persist(self, store: object, events: tuple[EvaluationEvent, ...]) -> None:
        for event in events:
            store.write_scheduler_checkpoint(
                event.symbol, event.timeframe, event.completed_at, event.event_id
            )
