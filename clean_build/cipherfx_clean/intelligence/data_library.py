"""Whole-market state library with explicit trade and non-trade records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Mapping, Sequence

from ..features import MarketFingerprint
from .outcomes import StateOutcome


@dataclass(frozen=True)
class MarketStateRecord:
    state_id: str
    symbol: str
    observed_at: datetime
    fingerprint: MarketFingerprint
    kind: Literal["TRADED", "REJECTED", "MISSED", "NO_TRADE"]
    reason_codes: tuple[str, ...]
    source_tick_ids: tuple[str, ...]
    metadata: Mapping[str, object]


class WholeMarketLibrary:
    def __init__(self):
        self._states: dict[str, MarketStateRecord] = {}
        self._outcomes: dict[str, StateOutcome] = {}

    def add_state(self, record: MarketStateRecord) -> None:
        if record.state_id in self._states:
            raise ValueError("state_id already exists")
        self._states[record.state_id] = record

    def add_outcome(self, outcome: StateOutcome) -> None:
        if outcome.state_id not in self._states:
            raise KeyError("outcome state is not stored")
        self._outcomes[outcome.state_id] = outcome

    def states(self, kind: str | None = None) -> tuple[MarketStateRecord, ...]:
        rows = tuple(self._states.values())
        return tuple(item for item in rows if kind is None or item.kind == kind)

    def outcome(self, state_id: str) -> StateOutcome | None:
        return self._outcomes.get(state_id)

    def outcomes(self, state_ids: Sequence[str]) -> tuple[StateOutcome, ...]:
        return tuple(self._outcomes[state_id] for state_id in state_ids if state_id in self._outcomes)
