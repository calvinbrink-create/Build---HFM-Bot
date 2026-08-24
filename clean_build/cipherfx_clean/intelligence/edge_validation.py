"""Candidate-edge validation and experiment graveyard records."""

from __future__ import annotations

from dataclasses import dataclass
from random import Random

from .research import ForwardOutcome, EdgeStatistics, calculate_statistics


@dataclass(frozen=True)
class ValidationSlice:
    instrument: str
    session: str
    regime: str
    outcomes: tuple[ForwardOutcome, ...]


@dataclass(frozen=True)
class ValidationReport:
    edge_id: str
    slices: tuple[ValidationSlice, ...]
    statistics: EdgeStatistics
    out_of_sample: EdgeStatistics


def validate_by_slice(edge_id: str, slices: tuple[ValidationSlice, ...]) -> ValidationReport:
    rows = tuple(row for item in slices for row in item.outcomes)
    if not rows:
        raise ValueError("validation requires outcomes")
    split = max(1, len(rows) // 2)
    out_rows = rows[split:] or rows
    return ValidationReport(edge_id, slices, calculate_statistics(edge_id, rows), calculate_statistics(edge_id, out_rows))


def monte_carlo_expectancy(outcomes: tuple[ForwardOutcome, ...], iterations: int = 1000, seed: int = 7) -> tuple[float, ...]:
    if not outcomes:
        return ()
    rng = Random(seed)
    values: list[float] = []
    for _ in range(iterations):
        sample = tuple(rng.choice(outcomes) for _ in outcomes)
        values.append(sum(item.r_multiple - item.cost for item in sample) / len(sample))
    return tuple(values)


@dataclass(frozen=True)
class GraveyardEntry:
    edge_id: str
    version: int
    reason: str
    statistics: EdgeStatistics | None
