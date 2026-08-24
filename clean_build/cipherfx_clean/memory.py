"""Persistent in-process market-state memory with explicit outcomes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class MemoryCase:
    case_id: str
    symbol: str
    observed_at: datetime
    fingerprint: tuple[float, ...]
    outcome: str | None = None
    r_multiple: float | None = None


class MemoryLibrary:
    def __init__(self):
        self._cases: dict[str, MemoryCase] = {}

    def add(self, case: MemoryCase) -> None:
        if case.case_id in self._cases:
            raise ValueError("duplicate case id")
        self._cases[case.case_id] = case

    def resolve(self, case_id: str, outcome: str, r_multiple: float) -> None:
        old = self._cases[case_id]
        self._cases[case_id] = MemoryCase(old.case_id, old.symbol, old.observed_at, old.fingerprint, outcome, r_multiple)

    def nearest(self, fingerprint: tuple[float, ...], limit: int = 10) -> tuple[MemoryCase, ...]:
        def distance(item: MemoryCase) -> float:
            return sum((left - right) ** 2 for left, right in zip(item.fingerprint, fingerprint)) ** 0.5
        return tuple(sorted(self._cases.values(), key=distance)[:limit])
