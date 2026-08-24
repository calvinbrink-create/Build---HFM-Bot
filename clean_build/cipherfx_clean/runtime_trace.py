"""Immutable observations of the canonical clean runtime path.

Trace events describe what the runtime actually completed. They are not
strategy inputs and are never consulted to approve, reject, or modify a trade.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Mapping


@dataclass(frozen=True)
class RuntimeTraceEvent:
    trace_id: str
    sequence: int
    stage: str
    observed_at: datetime
    artifact_id: str | None
    details: Mapping[str, object]


@dataclass(frozen=True)
class RuntimeTraceVerification:
    valid: bool
    reason: str
    observed_stages: tuple[str, ...]
    expected_stages: tuple[str, ...]


COMMON_STAGES = (
    "SOURCE_DATASET_STORED",
    "DATA_QUALITY_VALIDATED",
    "MARKET_SNAPSHOT_FROZEN",
    "MULTI_TIMEFRAME_STATE_BUILT",
    "PRE_EVENT_SNAPSHOT_FROZEN",
    "MARKET_INTELLIGENCE_BUILT",
    "MARKET_STATE_STORED",
    "FEATURE_VECTOR_STORED",
    "HISTORICAL_ANALOGUES_RETRIEVED",
    "STRUCTURED_DECISION_CREATED",
    "ANNOTATED_CHARTS_STORED",
    "DECISION_VALIDATED",
)

TRADE_STAGES = COMMON_STAGES + (
    "EXECUTION_HANDOFF_SUBMITTED",
    "BROKER_RECEIPT_RECORDED",
    "OUTCOME_STORED",
    "LINEAGE_STORED",
    "MEMORY_UPDATED",
    "PROCESS_COMPLETED",
)

NO_TRADE_STAGES = COMMON_STAGES + (
    "EXECUTION_NOT_REQUIRED",
    "OUTCOME_STORED",
    "LINEAGE_STORED",
    "MEMORY_UPDATED",
    "PROCESS_COMPLETED",
)


def verify_completed_trace(
    events: Iterable[RuntimeTraceEvent],
    *,
    execution_required: bool,
) -> RuntimeTraceVerification:
    """Verify an already-recorded path without influencing runtime decisions."""

    rows = tuple(events)
    observed = tuple(row.stage for row in rows)
    expected = TRADE_STAGES if execution_required else NO_TRADE_STAGES
    sequences = tuple(row.sequence for row in rows)
    if sequences != tuple(range(1, len(rows) + 1)):
        return RuntimeTraceVerification(False, "NON_CONTIGUOUS_SEQUENCE", observed, expected)
    if len({row.trace_id for row in rows}) > 1:
        return RuntimeTraceVerification(False, "MULTIPLE_TRACE_IDS", observed, expected)
    if observed != expected:
        return RuntimeTraceVerification(False, "STAGE_ORDER_MISMATCH", observed, expected)
    return RuntimeTraceVerification(True, "COMPLETE_ORDERED_RUNTIME_PATH", observed, expected)
