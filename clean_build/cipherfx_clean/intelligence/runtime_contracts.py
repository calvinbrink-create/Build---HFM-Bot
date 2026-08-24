"""Structured intelligence input/output and execution-facing gateway."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Mapping, Protocol


@dataclass(frozen=True)
class StructuredInput:
    symbol: str
    observed_at: datetime
    raw_state_id: str
    feature_state: Mapping[str, object]
    chart_ids: tuple[str, ...]
    analogue_ids: tuple[str, ...]
    edge_statistics: Mapping[str, object]


@dataclass(frozen=True)
class StructuredOutput:
    decision_id: str
    action: Literal["BUY", "SELL", "NO_TRADE"]
    confidence: float | None
    setup_type: str
    entry_area: tuple[float, float] | None
    stop_concept: str | None
    target_concept: str | None
    evidence_ids: tuple[str, ...]
    edge_id: str
    reason_codes: tuple[str, ...]


class IntelligenceResearcher(Protocol):
    def analyse(self, payload: StructuredInput) -> StructuredOutput:
        ...


class InstructionGateway:
    """Validate shape and forward structured intelligence to the bot port."""

    def __init__(self, send: callable):
        self._send = send

    def send(self, output: StructuredOutput) -> object:
        if not output.decision_id or not output.edge_id:
            raise ValueError("decision and edge identifiers are required")
        if output.action in {"BUY", "SELL"} and not output.evidence_ids:
            raise ValueError("trade intelligence requires evidence identifiers")
        if output.action not in {"BUY", "SELL", "NO_TRADE"}:
            raise ValueError("invalid structured action")
        return self._send(output) or output
