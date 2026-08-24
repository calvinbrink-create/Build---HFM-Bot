"""Schema validation only; no strategy scoring or market veto is performed."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .contracts import TradeDecision


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    reason: str


def validate_decision(decision: TradeDecision) -> ValidationResult:
    if not decision.decision_id or not decision.symbol:
        return ValidationResult(False, "MISSING_ID_OR_SYMBOL")
    if decision.action not in ("BUY", "SELL", "NO_TRADE"):
        return ValidationResult(False, "INVALID_ACTION")
    if decision.action in ("BUY", "SELL") and not decision.evidence:
        return ValidationResult(False, "MISSING_EVIDENCE")
    if decision.action in ("BUY", "SELL") and not decision.edge_id:
        return ValidationResult(False, "MISSING_EDGE_ID")
    if decision.action in ("BUY", "SELL") and (decision.edge_version is None or decision.edge_version <= 0):
        return ValidationResult(False, "MISSING_EDGE_VERSION")
    if decision.action in ("BUY", "SELL") and not decision.setup_type:
        return ValidationResult(False, "MISSING_SETUP_TYPE")
    if decision.action in ("BUY", "SELL") and not decision.reason_codes:
        return ValidationResult(False, "MISSING_REASON_CODES")
    if decision.action in ("BUY", "SELL") and (decision.confidence is None or not 0 <= decision.confidence <= 1):
        return ValidationResult(False, "INVALID_CONFIDENCE")
    if decision.action in ("BUY", "SELL") and (decision.probability is None or not 0 <= decision.probability <= 1):
        return ValidationResult(False, "INVALID_PROBABILITY")
    if decision.action in ("BUY", "SELL") and any(value is None or value <= 0 for value in (decision.entry, decision.stop, decision.target)):
        return ValidationResult(False, "INVALID_PRICE_GEOMETRY")
    if decision.action == "BUY" and not decision.stop < decision.entry < decision.target:
        return ValidationResult(False, "INVALID_PRICE_GEOMETRY")
    if decision.action == "SELL" and not decision.target < decision.entry < decision.stop:
        return ValidationResult(False, "INVALID_PRICE_GEOMETRY")
    if decision.entry_area is not None and (len(decision.entry_area) != 2 or decision.entry_area[0] > decision.entry_area[1]):
        return ValidationResult(False, "INVALID_ENTRY_AREA")
    return ValidationResult(True, "SCHEMA_VALID")


def validate_against_active_edges(
    decision: TradeDecision,
    active_edges: Mapping[str, int],
) -> ValidationResult:
    """Validate an immutable decision against the active edge registry.

    This is an authority check, not a second strategy scorer.  The registry is
    supplied by governance and this function performs no market-data or broker
    work.
    """

    schema = validate_decision(decision)
    if not schema.valid:
        return schema
    if decision.action == "NO_TRADE":
        return ValidationResult(True, "NO_TRADE_VALID")
    active_version = active_edges.get(decision.edge_id)
    if active_version is None:
        return ValidationResult(False, "UNAPPROVED_EDGE")
    if decision.edge_version != active_version:
        return ValidationResult(False, "INACTIVE_EDGE_VERSION")
    return ValidationResult(True, "ACTIVE_EDGE_VALID")
