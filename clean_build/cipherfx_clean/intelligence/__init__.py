"""Complete research and chart-intelligence boundary for the clean build."""

from __future__ import annotations

from typing import Protocol

from ..contracts import MarketState, TradeDecision
from .engine import IntelligenceEngine
from .model import IntelligenceReport


class IntelligencePort(Protocol):
    """Only authority allowed to produce a structured intelligence decision."""

    def decide(self, state: MarketState) -> TradeDecision:
        ...


__all__ = ["IntelligenceEngine", "IntelligencePort", "IntelligenceReport"]
