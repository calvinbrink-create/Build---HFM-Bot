"""Conditional pre-market planning and plan-versus-reality records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal


@dataclass(frozen=True)
class ConditionalPlan:
    plan_id: str
    symbol: str
    session: str
    created_at: datetime
    scenarios: tuple[str, ...]
    invalidation: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class PlanReality:
    plan_id: str
    realized: Literal["MATCHED", "PARTIAL", "FAILED", "NOT_OBSERVED"]
    actual_state_id: str | None
    reason: str


class PlanningLedger:
    def __init__(self):
        self._plans: dict[str, ConditionalPlan] = {}
        self._reality: dict[str, PlanReality] = {}

    def add(self, plan: ConditionalPlan) -> None:
        if plan.plan_id in self._plans:
            raise ValueError("plan id already exists")
        self._plans[plan.plan_id] = plan

    def reconcile(self, item: PlanReality) -> None:
        if item.plan_id not in self._plans:
            raise KeyError("plan is not stored")
        self._reality[item.plan_id] = item

    def plan(self, plan_id: str) -> ConditionalPlan:
        return self._plans[plan_id]
