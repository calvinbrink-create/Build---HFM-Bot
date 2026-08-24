"""Evidence, outcome statistics, and edge governance for intelligence research."""

from __future__ import annotations

from dataclasses import dataclass, field
from math import sqrt
from typing import Iterable, Literal, Mapping, Sequence


@dataclass(frozen=True)
class ForwardOutcome:
    state_id: str
    edge_id: str
    direction: Literal["BUY", "SELL"]
    r_multiple: float
    cost: float
    horizon_seconds: int
    regime: str
    session: str


@dataclass(frozen=True)
class EdgeStatistics:
    edge_id: str
    sample_size: int
    win_rate: float
    expectancy_r: float
    lower_win_rate: float
    upper_win_rate: float
    total_cost: float

    @property
    def cost_adjusted(self) -> bool:
        return self.total_cost >= 0


@dataclass(frozen=True)
class EdgeCandidate:
    edge_id: str
    version: int
    status: Literal["CANDIDATE", "HISTORICAL_VALIDATED", "SHADOW", "APPROVED", "DEGRADED", "REJECTED"]
    statistics: EdgeStatistics | None = None
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class RMultipleDistribution:
    edge_id: str
    probabilities: Mapping[float, float]
    sample_size: int


@dataclass(frozen=True)
class OverfitEvidence:
    experiment_id: str
    tested_variants: int
    selected_expectancy: float
    median_expectancy: float
    optimism: float
    concern: bool


def r_multiple_distribution(edge_id: str, outcomes: Iterable[ForwardOutcome], levels: tuple[float, ...] = (.5, 1.0, 1.5, 2.0)) -> RMultipleDistribution:
    rows = tuple(outcomes)
    return RMultipleDistribution(edge_id, {level: sum(item.r_multiple >= level for item in rows) / len(rows) if rows else 0.0 for level in levels}, len(rows))


def expected_value_after_costs(outcomes: Iterable[ForwardOutcome]) -> float:
    rows = tuple(outcomes)
    return sum(item.r_multiple - item.cost for item in rows) / len(rows) if rows else 0.0


def overfit_diagnostic(experiment_id: str, expectancies: Sequence[float]) -> OverfitEvidence:
    if not expectancies:
        raise ValueError("at least one experiment result is required")
    ordered = sorted(expectancies)
    median_value = ordered[len(ordered) // 2]
    selected = max(ordered)
    optimism = selected - median_value
    return OverfitEvidence(experiment_id, len(ordered), selected, median_value, optimism, optimism > max(.0, abs(median_value) * .5))


def calculate_statistics(edge_id: str, outcomes: Iterable[ForwardOutcome]) -> EdgeStatistics:
    rows = tuple(outcomes)
    if not rows:
        raise ValueError("statistics require outcomes")
    wins = sum(1 for row in rows if row.r_multiple > 0)
    rate = wins / len(rows)
    total_cost = sum(row.cost for row in rows)
    mean_r = sum(row.r_multiple - row.cost for row in rows) / len(rows)
    z = 1.959963984540054
    denominator = 1 + z * z / len(rows)
    center = (rate + z * z / (2 * len(rows))) / denominator
    margin = z * sqrt(rate * (1 - rate) / len(rows) + z * z / (4 * len(rows) ** 2)) / denominator
    return EdgeStatistics(edge_id, len(rows), rate, mean_r, max(0.0, center - margin), min(1.0, center + margin), total_cost)


class EdgeRegistry:
    def __init__(self):
        self._edges: dict[str, EdgeCandidate] = {}

    def register(self, candidate: EdgeCandidate) -> None:
        key = f"{candidate.edge_id}:v{candidate.version}"
        if key in self._edges:
            raise ValueError("edge version already exists")
        self._edges[key] = candidate

    def get(self, edge_id: str, version: int) -> EdgeCandidate:
        return self._edges[f"{edge_id}:v{version}"]

    def promote(self, edge_id: str, version: int, statistics: EdgeStatistics) -> EdgeCandidate:
        current = self.get(edge_id, version)
        if current.status != "SHADOW":
            raise ValueError("edge must complete historical and shadow stages before approval")
        updated = EdgeCandidate(edge_id, version, "APPROVED", statistics, current.evidence)
        self._edges[f"{edge_id}:v{version}"] = updated
        return updated

    def transition(self, edge_id: str, version: int, target: str, evidence: tuple[str, ...]) -> EdgeCandidate:
        current = self.get(edge_id, version)
        allowed = {
            "CANDIDATE": ("HISTORICAL_VALIDATED", "REJECTED"),
            "HISTORICAL_VALIDATED": ("SHADOW", "REJECTED"),
            "SHADOW": ("REJECTED",),
            "APPROVED": ("DEGRADED",),
            "DEGRADED": ("SHADOW", "REJECTED"),
            "REJECTED": (),
        }
        if target not in allowed[current.status]:
            raise ValueError(f"illegal edge transition: {current.status}->{target}")
        if not evidence:
            raise ValueError("edge transition requires evidence")
        updated = EdgeCandidate(edge_id, version, target, current.statistics, current.evidence + evidence)
        self._edges[f"{edge_id}:v{version}"] = updated
        return updated
