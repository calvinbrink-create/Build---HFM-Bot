"""Whole-market observation and outcome libraries, including non-trades."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Mapping

from ..features import MarketFingerprint


@dataclass(frozen=True)
class StateRecord:
    state_id: str
    symbol: str
    observed_at: datetime
    fingerprint: MarketFingerprint
    kind: Literal["TRADE", "WIN", "LOSS", "BREAKEVEN", "REJECTED", "EXPIRED", "MISSED", "FAILED_PATTERN", "NO_TRADE"]
    reason: str
    metadata: Mapping[str, object]
    dataset_id: str = ""
    source_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class OutcomeRecord:
    state_id: str
    result: Literal["WIN", "LOSS", "EXPIRED", "UNKNOWN"]
    r_multiple: float | None
    mfe: float | None
    mae: float | None
    time_to_mfe_seconds: int | None
    time_to_mae_seconds: int | None
    holding_time_seconds: int | None = None
    total_cost: float | None = None


class MarketStateLibrary:
    def __init__(self):
        self._states: dict[str, StateRecord] = {}
        self._outcomes: dict[str, OutcomeRecord] = {}

    def add_state(self, state: StateRecord) -> None:
        if state.state_id in self._states:
            raise ValueError("state_id already exists")
        self._states[state.state_id] = state

    def add_outcome(self, outcome: OutcomeRecord) -> None:
        if outcome.state_id not in self._states:
            raise KeyError("outcome state is not stored")
        previous = self._outcomes.get(outcome.state_id)
        if previous is not None and previous != outcome:
            raise ValueError("immutable outcome conflict")
        self._outcomes[outcome.state_id] = outcome

    def states(self, kind: str | None = None) -> tuple[StateRecord, ...]:
        rows = tuple(self._states.values())
        return tuple(row for row in rows if kind is None or row.kind == kind)

    def outcome(self, state_id: str) -> OutcomeRecord | None:
        return self._outcomes.get(state_id)
