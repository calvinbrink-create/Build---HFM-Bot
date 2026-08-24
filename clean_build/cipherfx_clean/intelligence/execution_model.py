"""Executable bid/ask replay and transaction-cost accounting."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class TransactionCost:
    spread: float
    commission: float
    swap: float
    slippage: float
    latency: float

    @property
    def total(self) -> float:
        return self.spread + self.commission + self.swap + self.slippage + self.latency


@dataclass(frozen=True)
class Quote:
    at_seconds: int
    bid: float
    ask: float


@dataclass(frozen=True)
class SimulatedFill:
    requested: float
    executed: float
    slippage: float
    cost: TransactionCost


def executable_entry(side: str, quote: Quote) -> float:
    if side == "BUY":
        return quote.ask
    if side == "SELL":
        return quote.bid
    raise ValueError("side must be BUY or SELL")


def simulate_fill(side: str, quote: Quote, requested: float, slippage: float, commission: float = 0.0, swap: float = 0.0, latency: float = 0.0) -> SimulatedFill:
    if requested <= 0:
        raise ValueError("requested price must be positive")
    direction = 1 if side == "BUY" else -1 if side == "SELL" else 0
    if not direction:
        raise ValueError("side must be BUY or SELL")
    executed = requested + direction * abs(slippage)
    cost = TransactionCost(quote.ask - quote.bid, commission, swap, abs(executed - requested), latency)
    return SimulatedFill(requested, executed, abs(executed - requested), cost)


def replay_executable_prices(side: str, quotes: Sequence[Quote], requested: float) -> tuple[SimulatedFill, ...]:
    return tuple(simulate_fill(side, quote, requested, abs(executable_entry(side, quote) - requested)) for quote in quotes)
