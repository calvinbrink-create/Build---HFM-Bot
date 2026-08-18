from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Callable, Any

from .contracts import FeedbackRecord, TradeProposal
from .database import DatabaseLayer


class LearningFeedbackEngine:
    """Consumes closed trades and expired proposals without changing immutable proposals."""

    ENGINE_BY_ASSET = {"forex": "FOREX", "index": "INDICES", "metal": "METALS"}
    CLOSING_DEAL_ENTRIES = {
        "DEAL_ENTRY_OUT", "DEAL_ENTRY_INOUT", "DEAL_ENTRY_OUT_BY",
        "OUT", "INOUT", "OUT_BY", "1", "2", "3",
    }

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
            "model_version": None,
            "outcome_type": "closed_trade",
            "learning_eligible": False,
            "result_r_valid": False,
            "proposal_trace_missing": False,
            "proposal_score": None,
            "proposal_confidence": None,
            "proposal_probability": None,
            "proposal_created_at": None,
            "proposal_expires_at": None,
            "score_bucket": None,
            "risk_source": "execution",
            "outcome_label": "UNKNOWN",
            "broker_order_ticket": 0,
            "broker_deal_ticket": 0,
            "broker_position_ticket": 0,
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
        proposal = self.database.get_proposal(proposal_id or trade_id)
        payload = (proposal or {}).get("payload") or {}
        context = payload.get("context") or {}
        if not isinstance(context, dict):
            context = {}
        result_r_valid = bool(math.isfinite(risk) and risk > 0.0)
        values.update({
            "model_version": context.get("model_version"),
            "side": str(side).upper(),
            "outcome_type": "closed_trade",
            "result_r_valid": result_r_valid,
            "learning_eligible": bool(result_r_valid and context.get("model_version")),
            "proposal_trace_missing": proposal is None,
            "proposal_score": proposal.get("score") if proposal else None,
            "proposal_confidence": proposal.get("confidence") if proposal else None,
            "proposal_probability": proposal.get("probability") if proposal else None,
            "proposal_created_at": proposal.get("created_at") if proposal else None,
            "proposal_expires_at": proposal.get("expires_at") if proposal else None,
            "score_bucket": int(float(proposal.get("score", 0.0)) // 10) * 10 if proposal else None,
            "outcome_label": "WIN" if float(realized_pnl) > 0 else "LOSS" if float(realized_pnl) < 0 else "BREAKEVEN",
        })
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
                str(record.metrics.get("engine") or "").upper()
                or self.ENGINE_BY_ASSET.get(asset_class, asset_class.upper()),
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
        proposal = self.database.get_proposal(proposal_id)
        payload = (proposal or {}).get("payload") or {}
        context = payload.get("context") or {}
        if not isinstance(context, dict):
            context = {}
        values = self._metrics(
            {
                "missed_trade": True,
                "expired_proposal": True,
                "false_positive": False,
                "false_negative": False,
                "outcome_type": "expired_proposal",
                "learning_eligible": False,
                "result_r_valid": False,
                "proposal_trace_missing": proposal is None,
                "model_version": context.get("model_version"),
                "proposal_score": proposal.get("score") if proposal else None,
                "proposal_confidence": proposal.get("confidence") if proposal else None,
                "proposal_probability": proposal.get("probability") if proposal else None,
                "proposal_created_at": proposal.get("created_at") if proposal else None,
                "proposal_expires_at": proposal.get("expires_at") if proposal else None,
                "score_bucket": int(float(proposal.get("score", 0.0)) // 10) * 10 if proposal else None,
                "outcome_label": "EXPIRED",
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

    @classmethod
    def _is_closing_deal(cls, deal: Any) -> bool:
        """Only broker close deals can complete a learning record.

        Older test gateways do not expose deal-entry metadata; those remain
        compatible and are treated as already-filtered close records.
        """
        raw = getattr(deal, "deal_entry", None)
        if raw in (None, ""):
            raw = getattr(deal, "entry", None)
        if raw in (None, ""):
            return True
        return str(raw).strip().upper() in cls.CLOSING_DEAL_ENTRIES

    def reconcile_closed_trades(self, gateway) -> int:
        active = {int(position.ticket) for position in gateway.positions()}
        reconciled = 0
        for row in self.database.open_filled_executions():
            try:
                reconciled += self._reconcile_one(row, active, gateway)
            except Exception as exc:
                # One bad row must not stop reconciliation for every other
                # closed trade this cycle.
                self.database.event(
                    "RECONCILE_ROW_FAILED",
                    {"error": f"{type(exc).__name__}:{str(exc)[:180]}", "proposal_id": row.get("proposal_id")},
                    symbol=str(row.get("symbol") or ""),
                    proposal_id=str(row.get("proposal_id") or ""),
                )
        if reconciled:
            self.database.event("CLOSED_TRADES_RECONCILED", {"count": reconciled})
        return reconciled

    def _reconcile_one(self, row, active: set[int], gateway) -> int:
        ticket = int(row["position_ticket"] or 0)
        if ticket <= 0:
            resolver = getattr(gateway, "resolve_position_ticket", None)
            if callable(resolver):
                ticket = int(
                    resolver(
                        order_ticket=int(row.get("order_ticket") or 0),
                        deal_ticket=int(row.get("deal_ticket") or 0),
                        symbol=str(row.get("symbol") or ""),
                    )
                    or 0
                )
                if ticket > 0:
                    self.database.update_execution_position_ticket(
                        row["proposal_id"], ticket
                    )
        if ticket <= 0 or ticket in active:
            return 0
        deals = gateway.deals_for_position(ticket)
        if not deals:
            return 0
        closing_deals = [deal for deal in deals if self._is_closing_deal(deal)]
        if not closing_deals:
            # The bridge can briefly expose the opening deal while the
            # position is still open. Never learn a zero-P/L close from it.
            return 0
        closed_at = max(
            (self._deal_time(deal, datetime.now(timezone.utc)) for deal in closing_deals),
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
            for deal in closing_deals
        )
        filled_at = row.get("filled_at")
        proposal = self.database.get_proposal(row["proposal_id"])
        entry_delay = None
        holding_time = None
        try:
            opened = datetime.fromisoformat(str(filled_at)) if filled_at else None
            created = datetime.fromisoformat(str(row.get("created_at"))) if row.get("created_at") else None
            if opened is not None:
                holding_time = max(0.0, (closed_at - opened).total_seconds())
            if created is not None and opened is not None:
                entry_delay = max(0.0, (opened - created).total_seconds())
        except (TypeError, ValueError):
            opened = None
        initial_risk = float(row["initial_risk"] or 0.0)
        risk_source = "execution"
        if initial_risk <= 0.0 and proposal:
            try:
                entry = float(row.get("fill_price") or proposal.get("entry_price") or 0.0)
                stop = float(proposal.get("stop_loss") or 0.0)
                volume = float(row.get("volume") or 0.0)
                calculator = getattr(gateway, "order_calc_profit", None)
                if callable(calculator) and entry > 0 and stop > 0 and volume > 0:
                    initial_risk = abs(float(calculator(row["symbol"], row["side"], volume, entry, stop)))
                    if initial_risk > 0:
                        risk_source = "broker_order_calc_profit"
            except (TypeError, ValueError, AttributeError):
                pass
        management_state = self.database.load_management_state(ticket) or {}
        last_metrics = management_state.get("last_metrics") or {}
        telemetry_complete = bool(management_state and last_metrics)
        proposal_context = ((proposal or {}).get("payload") or {}).get("context") or {}
        if not isinstance(proposal_context, dict):
            proposal_context = {}
        metrics = self._metrics(
            {
                "holding_time_seconds": holding_time,
                "entry_timing_seconds": entry_delay,
                "exit_timing_seconds": holding_time,
                "profit": pnl,
                "mfe_r": float(management_state.get("mfe_r", 0.0) or 0.0),
                "mae_r": abs(float(management_state.get("mae_r", 0.0) or 0.0)),
                "drawdown_r": last_metrics.get("drawdown_r"),
                "management_rank": management_state.get("rank") or "UNAVAILABLE",
                "management_telemetry_status": "COMPLETE" if telemetry_complete else "PARTIAL_NO_STATE",
                "management_actions": management_state.get("action_log", []),
                "management_state_updated_at": management_state.get("last_seen_at"),
                # The proposal row already stores the strategy engine; without
                # this the outcome was filed by ASSET CLASS (INDICES/METALS)
                # and SIMPLE's results credited the wrong engine entirely.
                "engine": str((proposal or {}).get("engine") or "").upper() or None,
                "risk_source": risk_source,
                "broker_order_ticket": int(row.get("order_ticket") or 0),
                "broker_deal_ticket": int(row.get("deal_ticket") or 0),
                "broker_position_ticket": int(ticket),
                "spread": last_metrics.get("spread_points"),
                "volatility": last_metrics.get("volatility"),
                "session": last_metrics.get("session", "unknown"),
                "slippage": last_metrics.get("slippage"),
                "model_version": proposal_context.get("model_version"),
                "memory_shape": proposal_context.get("memory_shape"),
            },
            pnl,
        )
        # management.py's _close() computes and stores the real reason
        # (EARLY_EXIT_R, MAX_DURATION_EXCEEDED, GIVEBACK_GUARD_TRIGGERED,
        # etc.) into management_state["last_action_reason"] every time it
        # closes a position - this reconciler is the only code path that
        # writes to trade_history, but was hardcoding "BROKER_HISTORY" and
        # discarding that real reason entirely. Found live 2026-07-22: every
        # one of the last 30 closed trades showed the generic fallback, with
        # zero visibility into which exit rule actually fired, even though
        # the underlying R-multiples clearly showed EARLY_EXIT_R (-0.75)
        # and profit-take were the real cause. Only fall back to the
        # generic label when a position closed with no recorded management
        # action at all (e.g. closed before management_state existed).
        exit_reason = str(management_state.get("last_action_reason") or "").strip() or "BROKER_HISTORY"
        self.record_closed_trade(
            row["proposal_id"],
            row["symbol"],
            row["side"],
            pnl,
            initial_risk,
            exit_reason,
            closed_at,
            row["asset_class"],
            proposal_id=row["proposal_id"],
            metrics=metrics,
        )
        self.database.mark_trade_closed(row["proposal_id"], closed_at, pnl)
        self.database.mark_position_closed(ticket, closed_at, pnl)
        return 1
