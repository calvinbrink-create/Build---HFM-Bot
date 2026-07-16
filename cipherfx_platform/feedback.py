from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Any

from .contracts import FeedbackRecord, TradeProposal
from .database import DatabaseLayer


class LearningFeedbackEngine:
    """Consumes closed trades and expired proposals without changing immutable proposals."""

    ENGINE_BY_ASSET = {"forex": "FOREX", "index": "INDICES", "metal": "METALS"}

    def __init__(self, database: DatabaseLayer, outcome_sink: Callable | None = None):
        self.database = database
        self.outcome_sink = outcome_sink

    @staticmethod
    def _metrics(metrics: dict[str, Any] | None, realized_pnl: float) -> dict[str, Any]:
        value = dict(metrics or {})
        defaults = {
            "spread": None,
            "volatility": None,
            "session": "unknown",
            "entry_timing_seconds": None,
            "exit_timing_seconds": None,
            "holding_time_seconds": None,
            "drawdown": None,
            "profit": float(realized_pnl),
            "false_positive": False,
            "false_negative": False,
            "missed_trade": False,
            "slippage": None,
            "market_regime": None,
        }
        for key, default in defaults.items():
            value.setdefault(key, default)
        return value

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
        *,
        proposal_id: str | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> FeedbackRecord:
        risk = float(initial_risk)
        closed = closed_at or datetime.now(timezone.utc)
        values = self._metrics(metrics, float(realized_pnl))
        record = FeedbackRecord(
            str(trade_id),
            str(symbol),
            str(side),
            float(realized_pnl) / risk if risk > 0 else 0.0,
            float(realized_pnl),
            closed,
            str(exit_reason),
            values,
        )
        inserted = self.database.record_trade_history(
            record.trade_id,
            proposal_id or record.trade_id,
            record.symbol,
            asset_class,
            record.side,
            record.realized_pnl,
            risk,
            record.result_r,
            record.exit_reason,
            record.closed_at,
            record.metrics,
        )
        if inserted:
            self.database.record_learning_metric(
                self.ENGINE_BY_ASSET.get(asset_class, asset_class.upper()),
                record.trade_id,
                record.symbol,
                asset_class,
                record.metrics,
                proposal_id=proposal_id or record.trade_id,
            )
            if self.outcome_sink is not None:
                self.outcome_sink(
                    asset_class,
                    record.trade_id,
                    record.symbol,
                    record.result_r,
                    record.realized_pnl,
                    record.metrics,
                )
        self.database.event(
            "TRADE_FEEDBACK",
            {
                "trade_id": record.trade_id,
                "result_r": record.result_r,
                "realized_pnl": record.realized_pnl,
                "exit_reason": record.exit_reason,
                "asset_class": asset_class,
                "metrics": record.metrics,
                "closed_at": record.closed_at.isoformat(),
                "duplicate_feedback": not inserted,
            },
            symbol=record.symbol,
            proposal_id=proposal_id or record.trade_id,
        )
        return record

    def record_expired_proposal(
        self,
        proposal_id: str,
        symbol: str,
        side: str,
        asset_class: str,
        *,
        expired_at: datetime | None = None,
    ) -> None:
        stamp = expired_at or datetime.now(timezone.utc)
        values = self._metrics(
            {
                "missed_trade": True,
                "expired_proposal": True,
                "false_positive": False,
                "false_negative": False,
            },
            0.0,
        )
        trade_id = f"expired:{proposal_id}"
        inserted = self.database.record_trade_history(
            trade_id,
            proposal_id,
            symbol,
            asset_class,
            side,
            0.0,
            0.0,
            0.0,
            "PROPOSAL_EXPIRED",
            stamp,
            values,
        )
        self.database.update_proposal_state(proposal_id, "EXPIRED")
        if inserted:
            engine = self.ENGINE_BY_ASSET.get(asset_class, asset_class.upper())
            self.database.record_learning_metric(
                engine, trade_id, symbol, asset_class, values, proposal_id=proposal_id
            )
            if self.outcome_sink is not None:
                if hasattr(self.outcome_sink, "__self__"):
                    self.outcome_sink.__self__.record_expired(
                        asset_class, proposal_id, symbol, values
                    )
                else:
                    self.outcome_sink(
                        asset_class, trade_id, symbol, 0.0, 0.0, values
                    )
        self.database.event(
            "PROPOSAL_EXPIRED_FEEDBACK",
            {"proposal_id": proposal_id, "metrics": values},
            symbol=symbol,
            proposal_id=proposal_id,
        )

    @staticmethod
    def _deal_time(deal: Any, fallback: datetime) -> datetime:
        value = getattr(deal, "time_utc", None)
        try:
            if value:
                return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            pass
        return fallback

    def reconcile_closed_trades(self, gateway) -> int:
        active = {int(position.ticket) for position in gateway.positions()}
        reconciled = 0
        for row in self.database.open_filled_executions():
            ticket = int(row["position_ticket"] or 0)
            if ticket <= 0 or ticket in active:
                continue
            deals = gateway.deals_for_position(ticket)
            if not deals:
                continue
            closed_at = max(
                (self._deal_time(deal, datetime.now(timezone.utc)) for deal in deals),
                default=datetime.now(timezone.utc),
            )
            pnl = sum(
                float(
                    getattr(
                        deal,
                        "net_profit",
                        getattr(deal, "profit", 0.0)
                        + getattr(deal, "swap", 0.0)
                        + getattr(deal, "commission", 0.0),
                    )
                    or 0.0
                )
                for deal in deals
            )
            filled_at = row.get("filled_at")
            entry_delay = None
            holding_time = None
            try:
                opened = datetime.fromisoformat(str(filled_at)) if filled_at else None
                if opened is not None:
                    holding_time = max(0.0, (closed_at - opened).total_seconds())
            except ValueError:
                pass
            metrics = self._metrics(
                {
                    "holding_time_seconds": holding_time,
                    "entry_timing_seconds": entry_delay,
                    "exit_timing_seconds": holding_time,
                    "profit": pnl,
                },
                pnl,
            )
            self.record_closed_trade(
                row["proposal_id"],
                row["symbol"],
                row["side"],
                pnl,
                float(row["initial_risk"] or 0.0),
                "BROKER_HISTORY",
                closed_at,
                row["asset_class"],
                proposal_id=row["proposal_id"],
                metrics=metrics,
            )
            self.database.mark_trade_closed(row["proposal_id"], closed_at, pnl)
            reconciled += 1
        if reconciled:
            self.database.event("CLOSED_TRADES_RECONCILED", {"count": reconciled})
        return reconciled
