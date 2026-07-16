from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from .contracts import FeedbackRecord
from .database import DatabaseLayer


class LearningFeedbackEngine:
    """Consumes completed outcomes and updates only the owning engine history."""

    def __init__(self, database: DatabaseLayer, outcome_sink: Callable | None = None):
        self.database = database
        self.outcome_sink = outcome_sink

    def record_closed_trade(
        self,
        trade_id: str,
        symbol: str,
        side: str,
        realized_pnl: float,
        initial_risk: float,
        exit_reason: str,
        closed_at: datetime | None = None,
        asset_class: str = "forex",
    ) -> FeedbackRecord:
        risk = float(initial_risk)
        closed = closed_at or datetime.now(timezone.utc)
        record = FeedbackRecord(
            str(trade_id), str(symbol), str(side),
            float(realized_pnl) / risk if risk > 0 else 0.0,
            float(realized_pnl), closed, str(exit_reason),
        )
        if self.outcome_sink is not None:
            self.outcome_sink(asset_class, record.trade_id, record.symbol, record.result_r, record.realized_pnl)
        self.database.event(
            "TRADE_FEEDBACK",
            {
                "trade_id": record.trade_id,
                "result_r": record.result_r,
                "realized_pnl": record.realized_pnl,
                "exit_reason": record.exit_reason,
                "asset_class": asset_class,
                "closed_at": record.closed_at.isoformat(),
            },
            symbol=record.symbol,
            proposal_id=record.trade_id,
        )
        return record
