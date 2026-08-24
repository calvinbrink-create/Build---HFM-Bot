"""Data lineage records for state -> analysis -> decision -> broker outcome."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Lineage:
    event_id: str
    state_id: str
    decision_id: str | None
    edge_id: str | None
    source_tick_start: datetime
    source_tick_end: datetime
    chart_hash: str | None
    outcome_id: str | None


class LineageLedger:
    def __init__(self):
        self._events: dict[str, Lineage] = {}

    def record(self, item: Lineage) -> None:
        if item.event_id in self._events:
            raise ValueError("event_id already exists")
        self._events[item.event_id] = item

    def get(self, event_id: str) -> Lineage:
        return self._events[event_id]
