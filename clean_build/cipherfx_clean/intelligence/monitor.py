"""Expected-versus-realized edge monitoring."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EdgeObservation:
    edge_id: str
    expected_probability: float
    expected_expectancy: float
    realized_r: float
    realized_win: bool


@dataclass(frozen=True)
class EdgeMonitorReport:
    edge_id: str
    sample_size: int
    realized_win_rate: float
    realized_expectancy: float
    expected_win_rate: float
    expected_expectancy: float
    probability_error: float


def monitor(observations: tuple[EdgeObservation, ...]) -> EdgeMonitorReport:
    if not observations:
        raise ValueError("monitor requires observations")
    edge = observations[0].edge_id
    if any(item.edge_id != edge for item in observations):
        raise ValueError("one edge per report")
    return EdgeMonitorReport(
        edge,
        len(observations),
        sum(item.realized_win for item in observations) / len(observations),
        sum(item.realized_r for item in observations) / len(observations),
        sum(item.expected_probability for item in observations) / len(observations),
        sum(item.expected_expectancy for item in observations) / len(observations),
        sum(item.realized_win for item in observations) / len(observations) - sum(item.expected_probability for item in observations) / len(observations),
    )
