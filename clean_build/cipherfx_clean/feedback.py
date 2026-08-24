"""Closed-trade feedback to research, never back into an approved decision."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .contracts import TradeOutcome
from .intelligence.outcomes import StateOutcome
from .intelligence.research import ForwardOutcome


@dataclass(frozen=True)
class ClosedTrade:
    decision_id: str
    edge_id: str
    direction: str
    r_multiple: float
    cost: float
    closed_at: datetime
    regime: str
    session: str
    entry_reason: tuple[str, ...] = ()
    mfe: float | None = None
    mae: float | None = None
    holding_time_seconds: int | None = None
    slippage: float | None = None


class LearningFeedback:
    def to_outcome(self, trade: ClosedTrade) -> ForwardOutcome:
        return ForwardOutcome(
            trade.decision_id,
            trade.edge_id,
            trade.direction,
            trade.r_multiple,
            trade.cost,
            0,
            trade.regime,
            trade.session,
        )

    def accept(self, outcome: TradeOutcome) -> bool:
        return outcome.status in {
            "CLOSED", "EXPIRED", "EXECUTION_FAILED", "REJECTED",
            "EXECUTION_BLOCKED", "NO_TRADE",
        }

    def to_state_outcome(self, trade: ClosedTrade, *, outcome_kind: str) -> StateOutcome:
        allowed = {
            "WINNER", "LOSER", "BREAKEVEN", "MISSED", "REJECTED", "EXPIRED",
            "EXECUTION_FAILED", "FAILED_PATTERN", "NO_TRADE", "TRADED",
        }
        if outcome_kind not in allowed:
            raise ValueError("unsupported learning outcome kind")
        return StateOutcome(
            state_id=trade.decision_id,
            outcome_kind=outcome_kind,
            direction=trade.direction,
            horizons=(),
            tp_before_sl=None,
            time_to_mfe_seconds=None,
            time_to_mae_seconds=None,
            mfe_value=trade.mfe,
            mae_value=trade.mae,
            r_multiple=trade.r_multiple,
            holding_time_seconds=trade.holding_time_seconds,
            total_cost=trade.cost,
        )


@dataclass(frozen=True)
class TradeReview:
    decision_id: str
    edge_id: str
    entry_reason: tuple[str, ...]
    exit_reason: str
    realized_r: float
    mfe: float | None
    mae: float | None
    slippage: float | None
    state_id: str


class PostTradeReview:
    def review(self, trade: ClosedTrade, *, exit_reason: str, mfe: float | None, mae: float | None, slippage: float | None, state_id: str) -> TradeReview:
        return TradeReview(trade.decision_id, trade.edge_id, trade.entry_reason, exit_reason, trade.r_multiple, mfe, mae, slippage, state_id)
