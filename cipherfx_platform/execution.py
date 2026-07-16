from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from mt5_xm_config import MT5RuntimeConfig
from mt5_xm_gateway import MT5Gateway

from .contracts import ExecutionResult, TradeProposal, SIDES
from .database import DatabaseLayer


@dataclass(frozen=True)
class ExecutionLimits:
    risk_pct: float = 0.25
    max_risk_pct: float = 1.0
    max_spread_points: float = 0.0


class ExecutionEngine:
    """Operational validation and single-path broker submission only."""

    def __init__(self, gateway: MT5Gateway, config: MT5RuntimeConfig, database: DatabaseLayer):
        self.gateway = gateway
        self.config = config
        self.database = database
        self.limits = ExecutionLimits(
            max(0.01, float(os.getenv("MT5_RISK_PCT", "0.25"))),
            max(0.01, float(os.getenv("MT5_MAX_RISK_PCT", "1.0"))),
            max(0.0, float(os.getenv("MT5_MAX_SPREAD_POINTS", "0"))),
        )
        self._lock = threading.RLock()
        self._claimed: set[str] = set()

    def _volume(self, proposal: TradeProposal, entry, spec, account):
        budget = max(0.0, float(getattr(account, "equity", 0.0) or 0.0))
        budget *= min(self.limits.risk_pct, self.limits.max_risk_pct) / 100.0
        if budget <= 0:
            return 0.0, 0.0, 0.0
        loss_per_lot = abs(float(self.gateway.order_calc_profit(
            proposal.symbol, proposal.side, 1.0, entry, proposal.stop_loss
        )))
        if loss_per_lot <= 0:
            return 0.0, 0.0, 0.0
        volume = self.gateway.normalize_volume(
            spec,
            min(float(spec.volume_max), max(float(spec.volume_min), budget / loss_per_lot)),
        )
        initial_risk = abs(float(self.gateway.order_calc_profit(
            proposal.symbol, proposal.side, volume, entry, proposal.stop_loss
        ))) if volume > 0 else 0.0
        margin = float(self.gateway.order_calc_margin(
            proposal.symbol, proposal.side, volume, entry
        )) if volume > 0 else 0.0
        return volume, margin, initial_risk

    def _preflight(self, proposal: TradeProposal):
        if proposal.side not in SIDES:
            return False, "INVALID_SIDE", 0.0, 0.0
        if datetime.now(timezone.utc) >= proposal.expires_at:
            return False, "PROPOSAL_EXPIRED", 0.0, 0.0

        account = self.gateway.account_info()
        if not getattr(account, "terminal_connected", True):
            return False, "BROKER_DISCONNECTED", 0.0, 0.0
        if not getattr(account, "trade_allowed", True) or not getattr(account, "account_trade_allowed", True):
            return False, "TRADING_NOT_ALLOWED", 0.0, 0.0
        if self.database.count_today() >= int(self.config.max_daily_trades):
            return False, "MAX_DAILY_TRADES", 0.0, 0.0

        positions = self.gateway.positions()
        if len(positions) >= int(self.config.max_open_trades):
            return False, "MAX_OPEN_TRADES", 0.0, 0.0
        if any(
            str(position.symbol).upper() == proposal.symbol.upper()
            and str(position.direction).upper() == proposal.side
            for position in positions
        ):
            return False, "DUPLICATE_POSITION", 0.0, 0.0

        _, tick = self.gateway.symbol_tick(proposal.symbol)
        entry = float(getattr(tick, "ask" if proposal.side == "BUY" else "bid", 0.0) or 0.0)
        if entry <= 0:
            return False, "INVALID_TICK", 0.0, 0.0

        spec = self.gateway.symbol_info(proposal.symbol)
        spread = (float(tick.ask) - float(tick.bid)) / max(float(spec.point), 1e-12)
        if self.limits.max_spread_points and spread > self.limits.max_spread_points:
            return False, "SPREAD_LIMIT", 0.0, 0.0

        geometry = (
            proposal.stop_loss < entry < proposal.take_profit
            if proposal.side == "BUY"
            else proposal.take_profit < entry < proposal.stop_loss
        )
        if not geometry:
            return False, "INVALID_SL_TP_GEOMETRY", 0.0, 0.0

        volume, margin, initial_risk = self._volume(proposal, entry, spec, account)
        if volume < float(spec.volume_min):
            return False, "MIN_VOLUME", 0.0, 0.0
        free = float(getattr(account, "free_margin", 0.0) or 0.0)
        if free > 0 and margin > free:
            return False, "INSUFFICIENT_MARGIN", 0.0, 0.0
        return True, "PASS", volume, initial_risk

    def _persist_result(self, proposal: TradeProposal, result: ExecutionResult, payload=None):
        self.database.execution(
            proposal.proposal_id,
            result.symbol,
            result.side,
            result.status,
            result.volume,
            initial_risk=result.initial_risk,
        )
        self.database.save_execution_result(proposal, result, response=payload)

    def submit(self, proposal: TradeProposal) -> ExecutionResult:
        with self._lock:
            if proposal.proposal_id in self._claimed or self.database.proposal_seen(proposal.proposal_id):
                result = ExecutionResult(
                    proposal.proposal_id,
                    "DUPLICATE_SUPPRESSED",
                    proposal.symbol,
                    proposal.side,
                    0.0,
                    reason="IDEMPOTENCY_KEY_ALREADY_USED",
                )
                self.database.event(
                    "DUPLICATE_SUPPRESSED",
                    {"reason": result.reason},
                    symbol=proposal.symbol,
                    proposal_id=proposal.proposal_id,
                )
                return result
            self._claimed.add(proposal.proposal_id)

        try:
            try:
                ok, reason, volume, initial_risk = self._preflight(proposal)
            except Exception as exc:
                reason = f"BROKER_ERROR:{type(exc).__name__}:{str(exc)[:180]}"
                result = ExecutionResult(
                    proposal.proposal_id,
                    "EXECUTION_BLOCKED",
                    proposal.symbol,
                    proposal.side,
                    0.0,
                    reason=reason,
                )
                self._persist_result(proposal, result, {"reason": reason})
                self.database.event(
                    "EXECUTION_BLOCKED",
                    {"reason": reason},
                    symbol=proposal.symbol,
                    proposal_id=proposal.proposal_id,
                )
                return result

            if not ok:
                result = ExecutionResult(
                    proposal.proposal_id,
                    "EXECUTION_BLOCKED",
                    proposal.symbol,
                    proposal.side,
                    0.0,
                    reason=reason,
                )
                self._persist_result(proposal, result, {"reason": reason})
                self.database.event(
                    "EXECUTION_BLOCKED",
                    {"reason": reason},
                    symbol=proposal.symbol,
                    proposal_id=proposal.proposal_id,
                )
                return result

            if self.config.dry_run or self.config.trade_mode == "paper":
                result = ExecutionResult(
                    proposal.proposal_id,
                    "DRY_RUN",
                    proposal.symbol,
                    proposal.side,
                    volume,
                    reason="PAPER_OR_DRY_RUN",
                    initial_risk=initial_risk,
                )
                self._persist_result(proposal, result, {"reason": result.reason})
                self.database.event(
                    "EXECUTION_DRY_RUN",
                    {"volume": volume},
                    symbol=proposal.symbol,
                    proposal_id=proposal.proposal_id,
                )
                return result

            submitted = datetime.now(timezone.utc)
            resolved, price, broker = self.gateway.place_market_order(
                proposal.symbol,
                proposal.side,
                volume,
                proposal.stop_loss,
                proposal.take_profit,
                "cipherfx:" + proposal.proposal_id,
            )
            retcode = int(getattr(broker, "retcode", 0) or 0)
            order = int(getattr(broker, "order", 0) or 0)
            deal = int(getattr(broker, "deal", 0) or 0)
            position = int(getattr(broker, "position", 0) or 0)
            status = "FILLED" if deal or retcode in {10008, 10009} else "REJECTED"
            result = ExecutionResult(
                proposal.proposal_id,
                status,
                resolved,
                proposal.side,
                volume,
                order_ticket=order,
                deal_ticket=deal,
                position_ticket=position,
                fill_price=float(price or 0.0),
                reason=str(getattr(broker, "comment", "") or retcode),
                submitted_at=submitted,
                filled_at=datetime.now(timezone.utc) if status == "FILLED" else None,
                initial_risk=initial_risk,
            )
            self._persist_result(
                proposal,
                result,
                {"retcode": retcode, "order": order, "deal": deal, "position": position},
            )
            self.database.event(
                "ORDER_FILLED" if status == "FILLED" else "ORDER_REJECTED",
                {"retcode": retcode, "order": order, "deal": deal, "position": position},
                symbol=resolved,
                proposal_id=proposal.proposal_id,
            )
            return result
        except Exception as exc:
            reason = f"BROKER_ERROR:{type(exc).__name__}:{str(exc)[:180]}"
            result = ExecutionResult(
                proposal.proposal_id,
                "REJECTED",
                proposal.symbol,
                proposal.side,
                0.0,
                reason=reason,
            )
            self._persist_result(proposal, result, {"reason": reason})
            self.database.event(
                "ORDER_REJECTED",
                {"reason": reason},
                symbol=proposal.symbol,
                proposal_id=proposal.proposal_id,
            )
            return result
        finally:
            with self._lock:
                self._claimed.discard(proposal.proposal_id)
