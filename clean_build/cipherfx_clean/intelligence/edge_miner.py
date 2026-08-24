"""Injected hypothesis discovery and deterministic experiment runner."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Mapping, Sequence


@dataclass(frozen=True)
class HypothesisProposal:
    hypothesis_id: str
    description: str
    required_features: tuple[str, ...]
    source: str
    version: int


@dataclass(frozen=True)
class ExperimentResult:
    experiment_id: str
    hypothesis_id: str
    sample_size: int
    values: tuple[float, ...]
    evidence_ids: tuple[str, ...]
    status: str


class EdgeMiner:
    """The text/research provider is injected; no model or web dependency is hidden."""

    def __init__(self, proposer: Callable[[Mapping[str, object]], Sequence[HypothesisProposal]]):
        self._proposer = proposer

    def propose(self, research_context: Mapping[str, object]) -> tuple[HypothesisProposal, ...]:
        proposals = tuple(self._proposer(research_context))
        if any(not item.hypothesis_id or item.version <= 0 for item in proposals):
            raise ValueError("hypothesis identifiers and positive versions are required")
        return proposals


def run_hypothesis(
    proposal: HypothesisProposal,
    states: Iterable[tuple[str, Mapping[str, object], float]],
    predicate: Callable[[Mapping[str, object]], bool],
) -> ExperimentResult:
    values: list[float] = []
    evidence: list[str] = []
    for state_id, features, value in states:
        if predicate(features):
            evidence.append(state_id)
            values.append(value)
    status = "TESTED" if values else "NO_OBSERVATIONS"
    return ExperimentResult(f"{proposal.hypothesis_id}:v{proposal.version}", proposal.hypothesis_id, len(values), tuple(values), tuple(evidence), status)
