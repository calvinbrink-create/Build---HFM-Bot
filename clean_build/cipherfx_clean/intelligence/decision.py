"""Decision creation from validated research evidence only."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..contracts import TradeDecision
from .research import EdgeCandidate


@dataclass(frozen=True)
class ResearchDecision:
    decision: TradeDecision
    edge: EdgeCandidate


def create_decision(
    *,
    decision_id: str,
    symbol: str,
    action: str,
    created_at: datetime,
    edge: EdgeCandidate,
    evidence: tuple[str, ...],
    entry: float | None = None,
    stop: float | None = None,
    target: float | None = None,
    confidence: float | None = None,
    probability: float | None = None,
    expected_value: float | None = None,
    setup_type: str = "",
    entry_area: tuple[float, float] | None = None,
    stop_concept: str = "",
    target_concept: str = "",
    reason_codes: tuple[str, ...] = (),
) -> ResearchDecision:
    if edge.status != "APPROVED":
        raise ValueError("only an approved edge can create a decision")
    if action not in ("BUY", "SELL", "NO_TRADE"):
        raise ValueError("invalid decision action")
    return ResearchDecision(
        TradeDecision(
            decision_id, symbol, action, created_at, evidence, edge.edge_id, entry, stop, target,
            edge.version, confidence, probability, expected_value, setup_type, entry_area,
            stop_concept, target_concept, reason_codes,
        ),
        edge,
    )
