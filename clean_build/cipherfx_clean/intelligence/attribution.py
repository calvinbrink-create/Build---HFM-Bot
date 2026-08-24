"""Feature, filter, governor and decision attribution records."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class Attribution:
    subject: str
    subject_type: Literal["FEATURE", "FILTER", "GOVERNOR", "DECISION"]
    state_id: str
    outcome_r: float
    action: str
    reason: str


def marginal_value(with_feature: tuple[float, ...], without_feature: tuple[float, ...]) -> float:
    if not with_feature or not without_feature:
        raise ValueError("both attribution samples are required")
    return sum(with_feature) / len(with_feature) - sum(without_feature) / len(without_feature)
