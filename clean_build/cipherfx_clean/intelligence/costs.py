"""Executable transaction-cost and fill evidence models."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TransactionCosts:
    spread: float
    commission: float
    swap: float
    slippage: float
    latency_cost: float

    @property
    def total(self) -> float:
        return self.spread + self.commission + self.swap + self.slippage + self.latency_cost


@dataclass(frozen=True)
class FillEvidence:
    requested_price: float
    filled_price: float
    requested_at_ns: int
    filled_at_ns: int

    @property
    def slippage(self) -> float:
        return self.filled_price - self.requested_price

    @property
    def latency_ns(self) -> int:
        return self.filled_at_ns - self.requested_at_ns
