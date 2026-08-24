"""Shadow strategy tournament; all candidates receive the same observations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping, Sequence


@dataclass(frozen=True)
class ShadowResult:
    candidate_id: str
    state_ids: tuple[str, ...]
    actions: tuple[str, ...]
    outcomes: tuple[float, ...]


class ShadowTournament:
    def __init__(self, candidates: Mapping[str, Callable[[object], str]]):
        if not candidates:
            raise ValueError("at least one shadow candidate is required")
        self._candidates = dict(candidates)

    def run(self, states: Sequence[tuple[str, object, float]]) -> tuple[ShadowResult, ...]:
        results: list[ShadowResult] = []
        for candidate_id, decide in self._candidates.items():
            ids: list[str] = []
            actions: list[str] = []
            outcomes: list[float] = []
            for state_id, state, outcome in states:
                ids.append(state_id)
                actions.append(decide(state))
                outcomes.append(outcome)
            results.append(ShadowResult(candidate_id, tuple(ids), tuple(actions), tuple(outcomes)))
        return tuple(results)
