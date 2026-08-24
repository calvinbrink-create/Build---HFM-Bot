"""Structured research-provider adapter. It accepts research, not free text."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Mapping

from ..contracts import MarketState, TradeDecision


@dataclass(frozen=True)
class ResearchRequest:
    symbol: str
    observed_at: datetime
    report: Mapping[str, object]
    analogue_ids: tuple[str, ...]
    statistics: Mapping[str, object]


class StructuredResearchProvider:
    """Translate a structured research response into an immutable decision."""

    def __init__(self, responder: Callable[[ResearchRequest], Mapping[str, object]], approved_edges: Mapping[str, int] | None = None):
        self._responder = responder
        self._approved_edges = dict(approved_edges or {})

    def decide(self, state: MarketState, request: ResearchRequest) -> TradeDecision:
        if request.symbol != state.symbol:
            raise ValueError("research request symbol mismatch")
        response = self._responder(request)
        action = response.get("action")
        if action not in ("BUY", "SELL", "NO_TRADE"):
            raise ValueError("research response has invalid action")
        evidence = response.get("evidence", ())
        if not isinstance(evidence, (list, tuple)) or not all(isinstance(item, str) for item in evidence):
            raise ValueError("research response evidence must be text")
        edge_id = str(response.get("edge_id") or "")
        edge_version = response.get("edge_version")
        if action in ("BUY", "SELL"):
            if edge_id not in self._approved_edges:
                raise ValueError("research response edge is not approved")
            if edge_version != self._approved_edges[edge_id]:
                raise ValueError("research response edge version is not active")
        entry_area = response.get("entry_area")
        if entry_area is not None:
            if not isinstance(entry_area, (list, tuple)) or len(entry_area) != 2:
                raise ValueError("research response entry_area must contain two prices")
            entry_area = (float(entry_area[0]), float(entry_area[1]))
        return TradeDecision(
            decision_id=str(response.get("decision_id") or ""),
            symbol=state.symbol,
            action=action,
            created_at=state.observed_at,
            evidence=tuple(evidence),
            edge_id=edge_id,
            entry=response.get("entry"),
            stop=response.get("stop"),
            target=response.get("target"),
            edge_version=int(edge_version) if edge_version is not None else None,
            confidence=response.get("confidence"),
            probability=response.get("probability"),
            expected_value=response.get("expected_value"),
            setup_type=str(response.get("setup_type") or ""),
            entry_area=entry_area,
            stop_concept=str(response.get("stop_concept") or ""),
            target_concept=str(response.get("target_concept") or ""),
            reason_codes=tuple(str(item) for item in response.get("reason_codes", ())),
        )
