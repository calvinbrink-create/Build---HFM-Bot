"""Exact broker-lifecycle reconciliation for clean-build learning feedback.

The adapter never guesses a decision from symbol, time proximity, direction,
or a truncated MT5 comment.  A lifecycle is linked only through an exact
broker ticket/reference or an exact decision marker.  Unresolved legacy
history remains visible and is excluded from decision-level learning.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
from typing import Literal, Mapping

from .contracts import TradeDecision
from .execution import ExecutionReceipt
from .hfm_history import HfmDeal, TradeLifecycle


FeedbackStatus = Literal[
    "MATCHED_CLOSED",
    "MATCHED_OPEN",
    "UNRESOLVED_IDENTITY",
    "IDENTITY_MISMATCH",
    "AMBIGUOUS_IDENTITY",
    "INVALID_LIFECYCLE",
]


@dataclass(frozen=True)
class BrokerTradeFeedback:
    feedback_id: str
    lifecycle_id: str
    position_id: int
    symbol: str
    status: FeedbackStatus
    decision_id: str | None
    edge_id: str | None
    direction: str
    opened_at: datetime | None
    closed_at: datetime | None
    entry_price: float | None
    exit_price: float | None
    initial_stop: float | None
    price_r_multiple: float | None
    gross_profit_account_currency: float
    commission_account_currency: float
    swap_account_currency: float
    net_profit_account_currency: float
    holding_seconds: float | None
    mfe_r: float | None
    mae_r: float | None
    training_eligible: bool
    source_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class BrokerFeedbackReport:
    report_id: str
    history_report_id: str
    lifecycle_count: int
    matched_closed_count: int
    matched_open_count: int
    unresolved_count: int
    mismatch_count: int
    ambiguous_count: int
    invalid_count: int
    training_eligible_count: int
    feedback_ids: tuple[str, ...]


def reconcile_broker_feedback(
    *,
    history_report_id: str,
    decisions: tuple[TradeDecision, ...],
    receipts: tuple[ExecutionReceipt, ...],
    deals: tuple[HfmDeal, ...],
    lifecycles: tuple[TradeLifecycle, ...],
    position_metrics: Mapping[int, Mapping[str, float | None]] | None = None,
) -> tuple[BrokerFeedbackReport, tuple[BrokerTradeFeedback, ...]]:
    """Link broker lifecycles to decisions using exact identities only."""

    decision_by_id = _unique_map(decisions, lambda row: row.decision_id, "decision")
    receipt_by_id = _unique_map(receipts, lambda row: row.decision_id, "receipt")
    deals_by_position: dict[int, tuple[HfmDeal, ...]] = {}
    for position_id in sorted({row.position_id for row in deals}):
        deals_by_position[position_id] = tuple(row for row in deals if row.position_id == position_id)
    metrics = dict(position_metrics or {})

    feedback = tuple(
        _reconcile_one(
            lifecycle,
            deals_by_position.get(lifecycle.position_id, ()),
            decision_by_id,
            receipt_by_id,
            metrics.get(lifecycle.position_id, {}),
        )
        for lifecycle in lifecycles
    )
    report_payload = (
        history_report_id,
        tuple(row.feedback_id for row in feedback),
        tuple((row.decision_id, row.status) for row in feedback),
    )
    report = BrokerFeedbackReport(
        report_id=_digest(report_payload),
        history_report_id=history_report_id,
        lifecycle_count=len(feedback),
        matched_closed_count=sum(row.status == "MATCHED_CLOSED" for row in feedback),
        matched_open_count=sum(row.status == "MATCHED_OPEN" for row in feedback),
        unresolved_count=sum(row.status == "UNRESOLVED_IDENTITY" for row in feedback),
        mismatch_count=sum(row.status == "IDENTITY_MISMATCH" for row in feedback),
        ambiguous_count=sum(row.status == "AMBIGUOUS_IDENTITY" for row in feedback),
        invalid_count=sum(row.status == "INVALID_LIFECYCLE" for row in feedback),
        training_eligible_count=sum(row.training_eligible for row in feedback),
        feedback_ids=tuple(row.feedback_id for row in feedback),
    )
    return report, feedback


def _reconcile_one(
    lifecycle: TradeLifecycle,
    deals: tuple[HfmDeal, ...],
    decisions: Mapping[str, TradeDecision],
    receipts: Mapping[str, ExecutionReceipt],
    metrics: Mapping[str, float | None],
) -> BrokerTradeFeedback:
    candidates: set[str] = set()
    evidence: list[str] = []

    order_ids = {str(row.order_id) for row in deals if row.order_id > 0}
    deal_ids = {str(row.deal_id) for row in deals if row.deal_id > 0}
    position_id = str(lifecycle.position_id)
    for decision_id, receipt in receipts.items():
        refs = {
            str(value)
            for value in (
                receipt.broker_reference,
                receipt.details.get("position_ticket"),
                receipt.details.get("order_ticket"),
                receipt.details.get("deal_ticket"),
            )
            if value not in (None, "")
        }
        if position_id in refs or refs.intersection(order_ids | deal_ids):
            candidates.add(decision_id)
            evidence.append(f"EXACT_BROKER_REFERENCE:{decision_id}")

    exact_comments = {row.comment for row in deals if row.entry_type == "DEAL_ENTRY_IN"}
    for decision_id in decisions:
        if f"cipherfx:{decision_id}" in exact_comments:
            candidates.add(decision_id)
            evidence.append(f"EXACT_DECISION_COMMENT:{decision_id}")

    reasons: list[str] = []
    decision: TradeDecision | None = None
    if lifecycle.status == "INVALID":
        status: FeedbackStatus = "INVALID_LIFECYCLE"
        reasons.extend(lifecycle.reason_codes or ("INVALID_BROKER_LIFECYCLE",))
    elif len(candidates) == 0:
        status = "UNRESOLVED_IDENTITY"
        reasons.append("NO_EXACT_DECISION_OR_BROKER_REFERENCE")
        if any(row.comment.startswith("cipherfx:") for row in deals):
            reasons.append("TRUNCATED_OR_UNKNOWN_DECISION_COMMENT")
    elif len(candidates) > 1:
        status = "AMBIGUOUS_IDENTITY"
        reasons.append("MULTIPLE_EXACT_IDENTITIES")
    else:
        decision_id = next(iter(candidates))
        decision = decisions.get(decision_id)
        if decision is None:
            status = "IDENTITY_MISMATCH"
            reasons.append("BROKER_REFERENCE_HAS_NO_IMMUTABLE_DECISION")
        elif decision.symbol != lifecycle.symbol or decision.action != lifecycle.side:
            status = "IDENTITY_MISMATCH"
            if decision.symbol != lifecycle.symbol:
                reasons.append("SYMBOL_MISMATCH")
            if decision.action != lifecycle.side:
                reasons.append("DIRECTION_MISMATCH")
        elif lifecycle.status == "CLOSED":
            status = "MATCHED_CLOSED"
        elif lifecycle.status == "OPEN":
            status = "MATCHED_OPEN"
        else:
            status = "INVALID_LIFECYCLE"
            reasons.append(f"UNSUPPORTED_LIFECYCLE_STATUS:{lifecycle.status}")

    entry = lifecycle.entry_price
    initial_stop = decision.stop if decision else None
    price_r = _price_r(lifecycle.side, entry, lifecycle.exit_price, initial_stop)
    mfe_r = _optional_float(metrics.get("mfe_r"))
    mae_r = _optional_float(metrics.get("mae_r"))
    training_eligible = bool(
        status == "MATCHED_CLOSED"
        and decision is not None
        and price_r is not None
        and lifecycle.closed_at is not None
    )
    source_ids = tuple(sorted(set(lifecycle.source_ids + tuple(evidence))))
    feedback_payload = (
        lifecycle.lifecycle_id,
        status,
        decision.decision_id if decision else None,
        price_r,
        lifecycle.net_profit,
        source_ids,
        tuple(sorted(set(reasons))),
    )
    return BrokerTradeFeedback(
        feedback_id=_digest(feedback_payload),
        lifecycle_id=lifecycle.lifecycle_id,
        position_id=lifecycle.position_id,
        symbol=lifecycle.symbol,
        status=status,
        decision_id=decision.decision_id if decision else None,
        edge_id=decision.edge_id if decision else None,
        direction=lifecycle.side,
        opened_at=lifecycle.opened_at,
        closed_at=lifecycle.closed_at,
        entry_price=entry,
        exit_price=lifecycle.exit_price,
        initial_stop=initial_stop,
        price_r_multiple=price_r,
        gross_profit_account_currency=lifecycle.gross_profit,
        commission_account_currency=lifecycle.commission,
        swap_account_currency=lifecycle.swap,
        net_profit_account_currency=lifecycle.net_profit,
        holding_seconds=lifecycle.holding_seconds,
        mfe_r=mfe_r,
        mae_r=mae_r,
        training_eligible=training_eligible,
        source_ids=source_ids,
        reason_codes=tuple(sorted(set(reasons))),
    )


def _price_r(side: str, entry: float | None, exit_price: float | None, stop: float | None) -> float | None:
    if entry is None or exit_price is None or stop is None:
        return None
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    sign = 1.0 if side == "BUY" else -1.0 if side == "SELL" else 0.0
    return sign * (exit_price - entry) / risk if sign else None


def _unique_map(rows: tuple[object, ...], key, label: str) -> dict[str, object]:
    result: dict[str, object] = {}
    for row in rows:
        identity = str(key(row))
        if identity in result and result[identity] != row:
            raise ValueError(f"duplicate conflicting {label} identity: {identity}")
        result[identity] = row
    return result


def _optional_float(value: float | None) -> float | None:
    return None if value is None else float(value)


def _digest(value: object) -> str:
    return sha256(json.dumps(value, default=str, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--history-report-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    from .store import EvidenceStore

    store = EvidenceStore(args.database)
    try:
        report, rows = reconcile_broker_feedback(
            history_report_id=args.history_report_id,
            decisions=store.load_decisions(),
            receipts=store.load_execution_receipts(),
            deals=store.load_hfm_deals(),
            lifecycles=store.load_trade_lifecycles(),
        )
        store.write_broker_feedback(report, rows)
        store.checkpoint()
    finally:
        store.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({
        "report": asdict(report),
        "status_counts": {
            status: sum(row.status == status for row in rows)
            for status in (
                "MATCHED_CLOSED", "MATCHED_OPEN", "UNRESOLVED_IDENTITY",
                "IDENTITY_MISMATCH", "AMBIGUOUS_IDENTITY", "INVALID_LIFECYCLE",
            )
        },
        "reason_counts": {
            reason: sum(reason in row.reason_codes for row in rows)
            for reason in sorted({reason for row in rows for reason in row.reason_codes})
        },
    }, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    return 0 if not (report.mismatch_count or report.ambiguous_count or report.invalid_count) else 2


if __name__ == "__main__":
    raise SystemExit(main())
