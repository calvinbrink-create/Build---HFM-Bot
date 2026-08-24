"""Versioned edge governance and live degradation observations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal


@dataclass(frozen=True)
class EdgeVersion:
    edge_id: str
    version: int
    status: Literal["CANDIDATE", "HISTORICAL_VALIDATED", "SHADOW", "LIMITED_LIVE", "FULL_LIVE", "DEGRADED", "REJECTED"]
    created_at: datetime
    parent_version: int | None
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class EdgeScorecard:
    edge_id: str
    version: int
    sample_size: int
    win_rate: float
    expectancy: float
    profit_factor: float | None
    drawdown: float
    sharpe: float | None
    cost_total: float
    uncertainty: tuple[float, float]
    oos_sample_size: int = 0
    holdout_sample_size: int = 0
    shadow_sample_size: int = 0
    holdout_expectancy: float | None = None
    shadow_expectancy: float | None = None


@dataclass(frozen=True)
class GovernancePolicy:
    minimum_sample: int
    minimum_expectancy: float
    minimum_oos_samples: int
    maximum_drawdown: float
    minimum_holdout_samples: int = 0
    minimum_shadow_samples: int = 0


def eligible_for_promotion(scorecard: EdgeScorecard, policy: GovernancePolicy) -> bool:
    return (
        scorecard.sample_size >= policy.minimum_sample
        and scorecard.oos_sample_size >= policy.minimum_oos_samples
        and scorecard.holdout_sample_size >= policy.minimum_holdout_samples
        and scorecard.shadow_sample_size >= policy.minimum_shadow_samples
        and scorecard.expectancy > policy.minimum_expectancy
        and (scorecard.holdout_expectancy is None or scorecard.holdout_expectancy > policy.minimum_expectancy)
        and (scorecard.shadow_expectancy is None or scorecard.shadow_expectancy > policy.minimum_expectancy)
        and scorecard.uncertainty[0] > 0
        and scorecard.drawdown <= policy.maximum_drawdown
    )


def monitor_decay(historical: EdgeScorecard, live: EdgeScorecard, expectancy_drop: float = 0.0) -> tuple[bool, str]:
    if live.sample_size == 0:
        return False, "NO_LIVE_SAMPLE"
    if live.expectancy < historical.expectancy - expectancy_drop:
        return True, "EXPECTANCY_DECAY"
    if live.uncertainty[1] < historical.uncertainty[0]:
        return True, "PROBABILITY_DETERIORATION"
    return False, "NO_DECAY_EVIDENCE"


def concept_drift(reference: tuple[float, ...], current: tuple[float, ...], threshold: float) -> bool:
    if not reference or not current:
        return False
    return distribution_shift(reference, current) > threshold


def distribution_shift(reference: tuple[float, ...], current: tuple[float, ...]) -> float:
    if not reference or not current:
        return 0.0
    values = sorted(set(reference + current))
    return max(
        abs(
            sum(item <= value for item in reference) / len(reference)
            - sum(item <= value for item in current) / len(current)
        )
        for value in values
    )


def transition_version(
    current: EdgeVersion,
    target: str,
    *,
    evidence_ids: tuple[str, ...],
) -> EdgeVersion:
    allowed = {
        "CANDIDATE": ("HISTORICAL_VALIDATED", "REJECTED"),
        "HISTORICAL_VALIDATED": ("SHADOW", "REJECTED"),
        "SHADOW": ("LIMITED_LIVE", "REJECTED"),
        "LIMITED_LIVE": ("FULL_LIVE", "DEGRADED"),
        "FULL_LIVE": ("DEGRADED",),
        "DEGRADED": ("SHADOW", "REJECTED"),
        "REJECTED": (),
    }
    if target not in allowed[current.status]:
        raise ValueError(f"illegal governance transition: {current.status}->{target}")
    if not evidence_ids:
        raise ValueError("governance transition requires evidence")
    return EdgeVersion(
        current.edge_id,
        current.version,
        target,
        current.created_at,
        current.parent_version,
        current.evidence_ids + evidence_ids,
    )


def version_transition(
    current: EdgeVersion,
    target: str,
    *,
    evidence_ids: tuple[str, ...],
) -> EdgeVersion:
    """Transition an edge while creating a new immutable version number."""

    transitioned = transition_version(current, target, evidence_ids=evidence_ids)
    return EdgeVersion(
        transitioned.edge_id,
        current.version + 1,
        transitioned.status,
        transitioned.created_at,
        current.version,
        transitioned.evidence_ids,
    )
