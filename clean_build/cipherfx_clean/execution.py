"""Single handoff from clean intelligence to the existing execution bot.

This module never imports a broker client and never builds broker orders.  It
serializes an already-approved immutable decision, submits it exactly once to
an injected bot port, and records the bot's response.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from threading import Lock
from typing import Literal, Mapping, Protocol

from .contracts import TradeDecision, TradeOutcome
from .slippage import BrokerFill, SlippageObservation, build_slippage_observation
from .validation import validate_decision

ExecutionStatus = Literal[
    "NO_TRADE",
    "SUBMITTED",
    "ACCEPTED",
    "FILLED",
    "EXECUTION_BLOCKED",
    "REJECTED",
    "ERROR",
    "IN_PROGRESS",
]


@dataclass(frozen=True)
class BotInstruction:
    """Versioned machine-readable instruction consumed by the existing bot."""

    schema_version: int
    request_hash: str
    decision_id: str
    edge_id: str
    symbol: str
    side: Literal["BUY", "SELL"]
    created_at: str
    evidence_ids: tuple[str, ...]
    entry: float | None
    stop: float | None
    target: float | None
    edge_version: int
    confidence: float
    probability: float
    expected_value: float | None
    setup_type: str
    entry_area: tuple[float, float] | None
    stop_concept: str
    target_concept: str
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class BotExecutionResponse:
    """Execution result returned by the existing CipherFX/MT5 bot."""

    decision_id: str
    status: ExecutionStatus
    broker_reference: str | None = None
    fills: tuple[BrokerFill, ...] = ()
    reason: str = ""
    details: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionReceipt:
    decision_id: str
    status: ExecutionStatus
    broker_reference: str | None = None
    details: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ClaimResult:
    state: Literal["NEW", "IN_PROGRESS", "COMPLETED", "CONFLICT"]
    receipt: ExecutionReceipt | None = None


class ExistingBotPort(Protocol):
    """Deployment-owned adapter to the existing deterministic execution bot."""

    def submit(self, instruction: BotInstruction) -> BotExecutionResponse:
        ...


class ExecutionJournal(Protocol):
    """Durable idempotency boundary for execution handoffs."""

    def claim_instruction(self, instruction: BotInstruction) -> ClaimResult:
        ...

    def complete_instruction(
        self,
        instruction: BotInstruction,
        receipt: ExecutionReceipt,
        slippage: tuple[SlippageObservation, ...],
    ) -> None:
        ...


class ExecutionPort(Protocol):
    """Only boundary permitted to submit an already-created decision."""

    def submit(self, decision: TradeDecision) -> ExecutionReceipt:
        ...


class InMemoryExecutionJournal:
    """Thread-safe journal used by deterministic tests and isolated replay."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._rows: dict[str, tuple[str, ExecutionReceipt | None]] = {}
        self._slippage: dict[str, SlippageObservation] = {}

    def claim_instruction(self, instruction: BotInstruction) -> ClaimResult:
        with self._lock:
            existing = self._rows.get(instruction.decision_id)
            if existing is None:
                self._rows[instruction.decision_id] = (instruction.request_hash, None)
                return ClaimResult("NEW")
            request_hash, receipt = existing
            if request_hash != instruction.request_hash:
                return ClaimResult("CONFLICT")
            if receipt is None:
                return ClaimResult("IN_PROGRESS")
            return ClaimResult("COMPLETED", receipt)

    def complete_instruction(
        self,
        instruction: BotInstruction,
        receipt: ExecutionReceipt,
        slippage: tuple[SlippageObservation, ...],
    ) -> None:
        with self._lock:
            existing = self._rows.get(instruction.decision_id)
            if existing is None or existing[0] != instruction.request_hash:
                raise ValueError("execution instruction was not claimed")
            _validate_slippage_completion(instruction, receipt, slippage)
            for observation in slippage:
                previous = self._slippage.get(observation.observation_id)
                if previous is not None and previous != observation:
                    raise ValueError("immutable slippage observation conflict")
                self._slippage[observation.observation_id] = observation
            self._rows[instruction.decision_id] = (instruction.request_hash, receipt)

    def slippage_observations(self) -> tuple[SlippageObservation, ...]:
        with self._lock:
            return tuple(sorted(self._slippage.values(), key=lambda item: item.observation_id))


def instruction_from_decision(decision: TradeDecision) -> BotInstruction:
    """Create a deterministic instruction without changing or rescoring it."""

    validation = validate_decision(decision)
    if not validation.valid:
        raise ValueError(validation.reason)
    if decision.action == "NO_TRADE":
        raise ValueError("NO_TRADE is not an executable instruction")
    payload = {
        "schema_version": 1,
        "decision_id": decision.decision_id,
        "edge_id": decision.edge_id,
        "symbol": decision.symbol,
        "side": decision.action,
        "created_at": decision.created_at.isoformat(),
        "evidence_ids": tuple(decision.evidence),
        "entry": decision.entry,
        "stop": decision.stop,
        "target": decision.target,
        "edge_version": decision.edge_version,
        "confidence": decision.confidence,
        "probability": decision.probability,
        "expected_value": decision.expected_value,
        "setup_type": decision.setup_type,
        "entry_area": decision.entry_area,
        "stop_concept": decision.stop_concept,
        "target_concept": decision.target_concept,
        "reason_codes": decision.reason_codes,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return BotInstruction(request_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(), **payload)


class BotExecutionHandoff:
    """Submit one validated instruction to the existing bot, with no fallback."""

    _BOT_STATUSES = {
        "SUBMITTED",
        "ACCEPTED",
        "FILLED",
        "EXECUTION_BLOCKED",
        "REJECTED",
        "ERROR",
    }

    def __init__(self, bot: ExistingBotPort, journal: ExecutionJournal):
        self._bot = bot
        self._journal = journal

    def submit(self, decision: TradeDecision) -> ExecutionReceipt:
        validation = validate_decision(decision)
        if not validation.valid:
            return ExecutionReceipt(
                decision.decision_id,
                "REJECTED",
                details={"reason": validation.reason},
            )
        if decision.action == "NO_TRADE":
            return ExecutionReceipt(decision.decision_id, "NO_TRADE")

        instruction = instruction_from_decision(decision)
        claim = self._journal.claim_instruction(instruction)
        if claim.state == "COMPLETED":
            if claim.receipt is None:
                raise RuntimeError("completed execution claim has no receipt")
            return claim.receipt
        if claim.state == "IN_PROGRESS":
            return ExecutionReceipt(
                decision.decision_id,
                "IN_PROGRESS",
                details={"reason": "EXECUTION_ALREADY_IN_PROGRESS", "request_hash": instruction.request_hash},
            )
        if claim.state == "CONFLICT":
            return ExecutionReceipt(
                decision.decision_id,
                "REJECTED",
                details={"reason": "DECISION_ID_CONFLICT", "request_hash": instruction.request_hash},
            )

        slippage: tuple[SlippageObservation, ...] = ()
        try:
            response = self._bot.submit(instruction)
            receipt, slippage = self._receipt_from_response(instruction, response)
        except Exception as exc:
            receipt = ExecutionReceipt(
                decision.decision_id,
                "ERROR",
                details={
                    "reason": "BOT_SUBMISSION_ERROR",
                    "error_type": type(exc).__name__,
                    "request_hash": instruction.request_hash,
                },
            )
        self._journal.complete_instruction(instruction, receipt, slippage)
        return receipt

    def _receipt_from_response(
        self,
        instruction: BotInstruction,
        response: BotExecutionResponse,
    ) -> tuple[ExecutionReceipt, tuple[SlippageObservation, ...]]:
        if response.decision_id != instruction.decision_id:
            return (
                ExecutionReceipt(
                    instruction.decision_id,
                    "ERROR",
                    details={"reason": "BOT_RESPONSE_ID_MISMATCH", "request_hash": instruction.request_hash},
                ),
                (),
            )
        if response.status not in self._BOT_STATUSES:
            return (
                ExecutionReceipt(
                    instruction.decision_id,
                    "ERROR",
                    details={"reason": "BOT_RESPONSE_STATUS_INVALID", "request_hash": instruction.request_hash},
                ),
                (),
            )
        if response.status == "FILLED" and not response.fills:
            return (
                ExecutionReceipt(
                    instruction.decision_id,
                    "ERROR",
                    response.broker_reference,
                    {
                        "reason": "BROKER_FILL_EVIDENCE_INCOMPLETE",
                        "request_hash": instruction.request_hash,
                    },
                ),
                (),
            )
        if response.status == "FILLED" and any(
            fill.requested_at is None for fill in response.fills
        ):
            return (
                ExecutionReceipt(
                    instruction.decision_id,
                    "ERROR",
                    response.broker_reference,
                    {
                        "reason": "BROKER_LATENCY_EVIDENCE_INCOMPLETE",
                        "request_hash": instruction.request_hash,
                    },
                ),
                (),
            )
        fill_keys = tuple((fill.order_ticket, fill.deal_ticket) for fill in response.fills)
        if len(fill_keys) != len(set(fill_keys)):
            return (
                ExecutionReceipt(
                    instruction.decision_id,
                    "ERROR",
                    response.broker_reference,
                    {
                        "reason": "BROKER_FILL_EVIDENCE_DUPLICATE",
                        "request_hash": instruction.request_hash,
                    },
                ),
                (),
            )
        slippage = tuple(
            build_slippage_observation(
                decision_id=instruction.decision_id,
                request_hash=instruction.request_hash,
                edge_id=instruction.edge_id,
                symbol=instruction.symbol,
                side=instruction.side,
                fill=fill,
            )
            for fill in response.fills
        )
        details = {
            **dict(response.details),
            "reason": response.reason,
            "request_hash": instruction.request_hash,
            "edge_id": instruction.edge_id,
            "instruction_entry": instruction.entry,
            "fills": [dict(fill.payload()) for fill in response.fills],
            "slippage_observation_ids": [item.observation_id for item in slippage],
        }
        return (
            ExecutionReceipt(
                instruction.decision_id,
                response.status,
                response.broker_reference,
                details,
            ),
            slippage,
        )


def outcome_from_receipt(receipt: ExecutionReceipt) -> TradeOutcome:
    return TradeOutcome(
        decision_id=receipt.decision_id,
        status=receipt.status,
        broker_reference=receipt.broker_reference,
        details=receipt.details,
    )


def _validate_slippage_completion(
    instruction: BotInstruction,
    receipt: ExecutionReceipt,
    slippage: tuple[SlippageObservation, ...],
) -> None:
    if receipt.status == "FILLED" and not slippage:
        raise ValueError("FILLED receipt requires immutable slippage evidence")
    if receipt.status != "FILLED" and slippage:
        raise ValueError("only a FILLED receipt may persist slippage evidence")
    expected_ids = tuple(receipt.details.get("slippage_observation_ids", ()))
    observed_ids = tuple(item.observation_id for item in slippage)
    if expected_ids != observed_ids:
        raise ValueError("receipt and slippage identities do not match")
    for observation in slippage:
        if (
            observation.decision_id != instruction.decision_id
            or observation.request_hash != instruction.request_hash
            or observation.edge_id != instruction.edge_id
            or observation.symbol != instruction.symbol
            or observation.side != instruction.side
        ):
            raise ValueError("slippage evidence does not match its immutable instruction")
