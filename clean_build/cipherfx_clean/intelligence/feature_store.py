"""Versioned feature records; one record is tied to one source state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping


@dataclass(frozen=True)
class FeatureRecord:
    state_id: str
    symbol: str
    observed_at: datetime
    timeframe: str
    values: Mapping[str, float | str | bool | None]
    source_hash: str
    version: int


class FeatureStore:
    def __init__(self):
        self._records: dict[tuple[str, str, int], FeatureRecord] = {}

    def put(self, record: FeatureRecord) -> None:
        key = (record.state_id, record.timeframe, record.version)
        if key in self._records:
            raise ValueError("feature version already exists")
        self._records[key] = record

    def get(self, state_id: str, timeframe: str, version: int) -> FeatureRecord:
        return self._records[(state_id, timeframe, version)]

    def for_state(self, state_id: str) -> tuple[FeatureRecord, ...]:
        return tuple(item for item in self._records.values() if item.state_id == state_id)
