"""Explicit edge promotion and demotion, separate from strategy analysis."""

from __future__ import annotations

from dataclasses import dataclass

from .intelligence.research import EdgeCandidate, EdgeStatistics


@dataclass(frozen=True)
class PromotionPolicy:
    minimum_samples: int
    minimum_expectancy: float
    minimum_lower_win_rate: float


def promote_if_valid(candidate: EdgeCandidate, stats: EdgeStatistics, policy: PromotionPolicy) -> EdgeCandidate:
    if stats.sample_size < policy.minimum_samples:
        raise ValueError("INSUFFICIENT_SAMPLE")
    if stats.expectancy_r < policy.minimum_expectancy:
        raise ValueError("NEGATIVE_EXPECTANCY")
    if stats.lower_win_rate < policy.minimum_lower_win_rate:
        raise ValueError("INSUFFICIENT_CONFIDENCE")
    return EdgeCandidate(candidate.edge_id, candidate.version, "APPROVED", stats, candidate.evidence)


def demote_if_degraded(candidate: EdgeCandidate, stats: EdgeStatistics, minimum_expectancy: float) -> EdgeCandidate:
    status = "DEGRADED" if stats.expectancy_r < minimum_expectancy else candidate.status
    return EdgeCandidate(candidate.edge_id, candidate.version, status, stats, candidate.evidence)
