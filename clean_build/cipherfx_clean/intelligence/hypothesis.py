"""Testable hypothesis and counterfactual records, not automatic promotion."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

from .research import ForwardOutcome, EdgeStatistics, calculate_statistics


@dataclass(frozen=True)
class Hypothesis:
    hypothesis_id: str
    description: str
    feature_names: tuple[str, ...]


@dataclass(frozen=True)
class HypothesisResult:
    hypothesis: Hypothesis
    matched: tuple[ForwardOutcome, ...]
    statistics: EdgeStatistics | None


def evaluate_hypothesis(
    hypothesis: Hypothesis,
    outcomes: Iterable[ForwardOutcome],
    predicate: Callable[[ForwardOutcome], bool],
) -> HypothesisResult:
    matched = tuple(row for row in outcomes if predicate(row))
    stats = calculate_statistics(hypothesis.hypothesis_id, matched) if matched else None
    return HypothesisResult(hypothesis, matched, stats)


@dataclass(frozen=True)
class CounterfactualResult:
    traded: tuple[ForwardOutcome, ...]
    not_traded: tuple[ForwardOutcome, ...]


def compare_counterfactuals(traded: Iterable[ForwardOutcome], not_traded: Iterable[ForwardOutcome]) -> CounterfactualResult:
    return CounterfactualResult(tuple(traded), tuple(not_traded))
