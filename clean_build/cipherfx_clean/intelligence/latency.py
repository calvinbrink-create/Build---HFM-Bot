"""Immutable data-to-analysis-to-decision-to-fill latency evidence.

Local process timestamps and MT5 broker timestamps are intentionally retained
as separate clock domains.  The module never subtracts one clock domain from
the other, so clock skew cannot be presented as execution latency.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from math import isfinite
from typing import Mapping


@dataclass(frozen=True)
class LatencyTrace:
    """Backward-compatible nanosecond trace used by isolated cost models."""

    data_ns: int
    analysis_ns: int
    decision_ns: int
    order_ns: int
    fill_ns: int

    @property
    def data_to_fill_ns(self) -> int:
        return self.fill_ns - self.data_ns

    @property
    def analysis_to_fill_ns(self) -> int:
        return self.fill_ns - self.analysis_ns


@dataclass(frozen=True)
class RuntimeLatencyContext:
    """Measured local stages completed before the broker handoff."""

    trace_id: str
    decision_id: str
    symbol: str
    source_data_at: datetime
    data_received_at: datetime
    analysis_started_at: datetime
    analysis_completed_at: datetime
    decision_created_at: datetime
    source_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not all((self.trace_id, self.decision_id, self.symbol)) or not self.source_ids:
            raise ValueError("latency context requires complete identity and lineage")
        _require_aware(
            self.source_data_at,
            self.data_received_at,
            self.analysis_started_at,
            self.analysis_completed_at,
            self.decision_created_at,
        )
        _require_ordered(
            self.data_received_at,
            self.analysis_started_at,
            self.analysis_completed_at,
            self.decision_created_at,
            label="local pre-execution",
        )


@dataclass(frozen=True)
class ExecutionLatencyObservation:
    """One complete timing record for one exact MT5 deal fill."""

    observation_id: str
    trace_id: str
    decision_id: str
    request_hash: str
    edge_id: str
    symbol: str
    order_ticket: str
    deal_ticket: str
    source_data_at: datetime
    data_received_at: datetime
    analysis_started_at: datetime
    analysis_completed_at: datetime
    decision_created_at: datetime
    order_handoff_at: datetime
    fill_received_at: datetime
    broker_requested_at: datetime
    broker_filled_at: datetime
    source_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        identities = (
            self.observation_id,
            self.trace_id,
            self.decision_id,
            self.request_hash,
            self.edge_id,
            self.symbol,
            self.order_ticket,
            self.deal_ticket,
        )
        if not all(identities) or not self.source_ids:
            raise ValueError("latency observation requires complete identity and lineage")
        _require_aware(
            self.source_data_at,
            self.data_received_at,
            self.analysis_started_at,
            self.analysis_completed_at,
            self.decision_created_at,
            self.order_handoff_at,
            self.fill_received_at,
            self.broker_requested_at,
            self.broker_filled_at,
        )
        _require_ordered(
            self.data_received_at,
            self.analysis_started_at,
            self.analysis_completed_at,
            self.decision_created_at,
            self.order_handoff_at,
            self.fill_received_at,
            label="local end-to-end",
        )
        _require_ordered(
            self.broker_requested_at,
            self.broker_filled_at,
            label="broker execution",
        )
        if not all(isfinite(value) and value >= 0 for value in self.durations_seconds.values()):
            raise ValueError("latency durations must be finite and non-negative")

    @property
    def source_age_at_receive_seconds(self) -> float:
        """Signed source age; kept separate because MT5 and VPS clocks may skew."""

        return (self.data_received_at - self.source_data_at).total_seconds()

    @property
    def durations_seconds(self) -> Mapping[str, float]:
        return {
            "receive_to_analysis_start": (
                self.analysis_started_at - self.data_received_at
            ).total_seconds(),
            "analysis": (
                self.analysis_completed_at - self.analysis_started_at
            ).total_seconds(),
            "analysis_to_decision": (
                self.decision_created_at - self.analysis_completed_at
            ).total_seconds(),
            "decision_to_order_handoff": (
                self.order_handoff_at - self.decision_created_at
            ).total_seconds(),
            "order_handoff_to_fill_received": (
                self.fill_received_at - self.order_handoff_at
            ).total_seconds(),
            "local_data_to_fill_received": (
                self.fill_received_at - self.data_received_at
            ).total_seconds(),
            "broker_request_to_fill": (
                self.broker_filled_at - self.broker_requested_at
            ).total_seconds(),
        }

    def payload(self) -> Mapping[str, object]:
        return {
            "observation_id": self.observation_id,
            "trace_id": self.trace_id,
            "decision_id": self.decision_id,
            "request_hash": self.request_hash,
            "edge_id": self.edge_id,
            "symbol": self.symbol,
            "order_ticket": self.order_ticket,
            "deal_ticket": self.deal_ticket,
            "source_data_at": self.source_data_at.isoformat(),
            "data_received_at": self.data_received_at.isoformat(),
            "analysis_started_at": self.analysis_started_at.isoformat(),
            "analysis_completed_at": self.analysis_completed_at.isoformat(),
            "decision_created_at": self.decision_created_at.isoformat(),
            "order_handoff_at": self.order_handoff_at.isoformat(),
            "fill_received_at": self.fill_received_at.isoformat(),
            "broker_requested_at": self.broker_requested_at.isoformat(),
            "broker_filled_at": self.broker_filled_at.isoformat(),
            "source_age_at_receive_seconds": self.source_age_at_receive_seconds,
            "durations_seconds": dict(self.durations_seconds),
            "source_ids": self.source_ids,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "ExecutionLatencyObservation":
        return cls(
            observation_id=str(payload["observation_id"]),
            trace_id=str(payload["trace_id"]),
            decision_id=str(payload["decision_id"]),
            request_hash=str(payload["request_hash"]),
            edge_id=str(payload["edge_id"]),
            symbol=str(payload["symbol"]),
            order_ticket=str(payload["order_ticket"]),
            deal_ticket=str(payload["deal_ticket"]),
            source_data_at=datetime.fromisoformat(str(payload["source_data_at"])),
            data_received_at=datetime.fromisoformat(str(payload["data_received_at"])),
            analysis_started_at=datetime.fromisoformat(str(payload["analysis_started_at"])),
            analysis_completed_at=datetime.fromisoformat(str(payload["analysis_completed_at"])),
            decision_created_at=datetime.fromisoformat(str(payload["decision_created_at"])),
            order_handoff_at=datetime.fromisoformat(str(payload["order_handoff_at"])),
            fill_received_at=datetime.fromisoformat(str(payload["fill_received_at"])),
            broker_requested_at=datetime.fromisoformat(str(payload["broker_requested_at"])),
            broker_filled_at=datetime.fromisoformat(str(payload["broker_filled_at"])),
            source_ids=tuple(str(value) for value in payload["source_ids"]),
        )


def build_execution_latency_observation(
    *,
    context: RuntimeLatencyContext,
    request_hash: str,
    edge_id: str,
    order_ticket: str,
    deal_ticket: str,
    order_handoff_at: datetime,
    fill_received_at: datetime,
    broker_requested_at: datetime,
    broker_filled_at: datetime,
    broker_source_id: str,
) -> ExecutionLatencyObservation:
    identity = {
        "trace_id": context.trace_id,
        "decision_id": context.decision_id,
        "request_hash": request_hash,
        "order_ticket": order_ticket,
        "deal_ticket": deal_ticket,
        "broker_requested_at": broker_requested_at.isoformat(),
        "broker_filled_at": broker_filled_at.isoformat(),
        "broker_source_id": broker_source_id,
    }
    observation_id = sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return ExecutionLatencyObservation(
        observation_id=observation_id,
        trace_id=context.trace_id,
        decision_id=context.decision_id,
        request_hash=request_hash,
        edge_id=edge_id,
        symbol=context.symbol,
        order_ticket=order_ticket,
        deal_ticket=deal_ticket,
        source_data_at=context.source_data_at,
        data_received_at=context.data_received_at,
        analysis_started_at=context.analysis_started_at,
        analysis_completed_at=context.analysis_completed_at,
        decision_created_at=context.decision_created_at,
        order_handoff_at=order_handoff_at,
        fill_received_at=fill_received_at,
        broker_requested_at=broker_requested_at,
        broker_filled_at=broker_filled_at,
        source_ids=tuple(
            dict.fromkeys(
                (
                    *context.source_ids,
                    f"instruction:{request_hash}",
                    f"mt5-order:{order_ticket}",
                    f"mt5-deal:{deal_ticket}",
                    broker_source_id,
                )
            )
        ),
    )


def _require_aware(*values: datetime) -> None:
    if any(value.tzinfo is None or value.utcoffset() is None for value in values):
        raise ValueError("latency timestamps must be timezone-aware")


def _require_ordered(*values: datetime, label: str) -> None:
    if any(right < left for left, right in zip(values, values[1:])):
        raise ValueError(f"{label} timestamps are out of order")
