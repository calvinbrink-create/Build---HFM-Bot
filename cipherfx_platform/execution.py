from __future__ import annotations

import math
import os
import threading
from datetime import datetime, timedelta, timezone

from mt5_xm_gateway import MT5Gateway
from mt5_xm_config import MT5RuntimeConfig

from .contracts import ExecutionResult, TradeProposal
from .database import DatabaseLayer


class ExecutionEngine:
    """Operational execution for one immutable proposal.

    A proposal is one trade idea. If configured, that idea is submitted as a
    single tiered burst of up to 4 legs. This class never calculates market
    direction, indicators, setup evidence, memory, score, confidence, or
    probability. Every broker call goes through _send_leg().
    """

    def __init__(self, gateway: MT5Gateway, config: MT5RuntimeConfig, database: DatabaseLayer):
        self.gateway = gateway
        self.config = config
        self.database = database
        self._claimed: set[str] = set()
        self._lock = threading.RLock()

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _symbol_key(value: str) -> str:
        return "".join(ch for ch in str(value or "").upper() if ch.isalnum())

    def _gold_daily_loss_reason(self, proposal: TradeProposal) -> str:
        """Block new Gold entries after the configured SAST-day loss count."""
        canonical = str(getattr(proposal, "symbol", "") or "").upper().replace(".", "_")
        if canonical != "XAUUSD":
            return ""
        limit = max(0, int(getattr(self.config, "max_xauusd_losses_per_day", 2) or 0))
        if limit <= 0:
            return ""
        losses = self.database.count_realized_losses_today_for_symbol("XAUUSD")
        if losses >= limit:
            return f"GOLD_DAILY_LOSS_LIMIT:{losses}/{limit}"
        return ""

    def _symbol_reentry_cooldown_reason(self, proposal: TradeProposal) -> str:
        """Block a second burst on the same symbol for the configured window."""
        seconds = max(
            0,
            int(getattr(self.config, "symbol_reentry_cooldown_seconds", 0) or 0),
        )
        if seconds <= 0:
            return ""
        latest = self.database.latest_filled_execution_at_for_symbol(proposal.symbol)
        if latest is None:
            return ""
        remaining = seconds - int((self._now() - latest).total_seconds())
        if remaining > 0:
            return f"SYMBOL_COOLDOWN_ACTIVE:{max(1, remaining)}"
        return ""

    def _setup_duplicate_reason(self, proposal: TradeProposal) -> str:
        """Suppress the same persisted setup identity within the cooldown window."""
        context = proposal.context if isinstance(proposal.context, dict) else {}
        fingerprint = str(context.get("setup_fingerprint") or "")
        seconds = max(
            0,
            int(getattr(self.config, "symbol_reentry_cooldown_seconds", 0) or 0),
        )
        if not fingerprint or seconds <= 0:
            return ""
        since = self._now() - timedelta(seconds=seconds)
        if self.database.setup_fingerprint_seen_recently(
            proposal.symbol,
            fingerprint,
            since,
            exclude_proposal_id=proposal.proposal_id,
        ):
            return "SETUP_DUPLICATE_SUPPRESSED"
        return ""

    def _result(
        self,
        proposal: TradeProposal,
        status: str,
        volume: float = 0.0,
        reason: str = "",
        *,
        response: dict | None = None,
        **kwargs,
    ) -> ExecutionResult:
        submitted_at = self._now()
        filled_at = submitted_at if status in {"FILLED", "DRY_RUN"} else None
        result = ExecutionResult(
            proposal_id=proposal.proposal_id,
            status=status,
            symbol=proposal.symbol,
            side=proposal.side,
            volume=float(volume),
            reason=reason,
            submitted_at=submitted_at,
            filled_at=filled_at,
            initial_risk=float(proposal.risk_amount or 0.0),
            order_ticket=int(kwargs.get("order_ticket", 0)),
            deal_ticket=int(kwargs.get("deal_ticket", 0)),
            position_ticket=int(kwargs.get("position_ticket", 0)),
            fill_price=float(kwargs.get("fill_price", 0.0)),
        )
        self.database.save_execution_result(
            proposal,
            result,
            response=response or {"reason": result.reason},
        )
        self.database.event(
            "EXECUTION_RESULT",
            {
                "status": status,
                "reason": reason,
                "volume": volume,
                "filled_at": filled_at.isoformat() if filled_at else "",
                "legs": (response or {}).get("legs", []),
            },
            symbol=proposal.symbol,
            proposal_id=proposal.proposal_id,
        )
        return result

    @staticmethod
    def _risk_percent(symbol: str, asset_class: str) -> float:
        canonical = str(symbol or "").upper().replace(".", "_")
        specific = f"MT5_RISK_PCT_{canonical}"
        if os.getenv(specific) is not None:
            raw = os.getenv(specific, "1.0")
        else:
            group = {
                "metal": "METALS",
                "index": "INDICES",
                "forex": "FOREX",
            }.get(str(asset_class or "").lower(), "")
            raw = os.getenv(f"MT5_RISK_PCT_{group}", "1.0") if group else "1.0"
        try:
            return min(10.0, max(0.01, float(raw)))
        except (TypeError, ValueError):
            return 1.0

    @staticmethod
    def _max_lot(symbol: str, asset_class: str) -> float:
        canonical = str(symbol or "").upper().replace(".", "_")
        specific = f"MT5_MAX_LOT_{canonical}"
        if os.getenv(specific) is not None:
            raw = os.getenv(specific, "0")
        else:
            group = {
                "metal": "METALS",
                "index": "INDICES",
                "forex": "FOREX",
            }.get(str(asset_class or "").lower(), "")
            raw = os.getenv(f"MT5_MAX_LOT_{group}", "0") if group else "0"
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _legacy_env_override(prefix: str, proposal: TradeProposal, default: float) -> float:
        symbol = str(getattr(proposal, "symbol", "") or "").upper().replace(".", "_")
        suffix = {
            "forex": "FOREX",
            "index": "INDICES",
            "metal": "METALS",
        }.get(str(getattr(proposal, "asset_class", "") or "").lower(), "")
        for key in (f"{prefix}_{symbol}", f"{prefix}_{suffix}" if suffix else ""):
            if not key:
                continue
            raw = os.getenv(key)
            if raw is None:
                continue
            try:
                return float(raw)
            except (TypeError, ValueError):
                continue
        return float(default)

    def _sizing_loss_per_lot(self, proposal: TradeProposal, loss_per_lot: float) -> float:
        """Size the position as if the stop were at least ATR-floored.

        Risk-based sizing assumes the stop holds. When the structural stop lands
        inside the noise - a 0.45pt stop on gold against a 0.34 spread - it
        cannot hold, budget/loss_per_lot explodes, and the only remaining brake
        is spec.volume_max. That is how one gold position reached 30 lots and
        lost 12.9% of equity on a 1.667% budget.

        The stop itself is left exactly where the engine placed it, so R:R,
        targets and the ratchet are unaffected. Only the lot size is computed
        against a realistic adverse distance. Risk per trade is NOT reduced -
        this is what makes the configured risk percentage true.
        """
        try:
            setup = proposal.context.get("setup", {}) if isinstance(proposal.context, dict) else {}
            atr = abs(float(setup.get("atr", 0.0) or 0.0))
            actual = abs(float(setup.get("stop_distance", 0.0) or 0.0))
            floor_mult = float(os.getenv("MT5_SIZING_ATR_FLOOR", "1.0") or 1.0)
        except (TypeError, ValueError, AttributeError):
            return loss_per_lot
        if atr <= 0.0 or actual <= 0.0 or floor_mult <= 0.0:
            return loss_per_lot
        sizing_distance = max(actual, atr * floor_mult)
        if sizing_distance <= actual:
            return loss_per_lot
        return loss_per_lot * (sizing_distance / actual)

    def _legacy_volume(self, proposal: TradeProposal, entry: float, spec, account) -> float:
        """Preserve the pre-Gold sizing path for every non-XAUUSD symbol."""
        budget = max(0.0, float(getattr(account, "equity", 0.0) or 0.0))
        risk_pct = self._legacy_env_override("MT5_RISK_PCT", proposal, 0.25)
        max_risk_pct = self._legacy_env_override("MT5_MAX_RISK_PCT", proposal, 1.0)
        budget *= min(risk_pct, max_risk_pct) / 100.0
        capital_cap = max(0.0, float(getattr(self.config, "capital_cap_usd", 0.0) or 0.0))
        position_pct = max(
            0.0,
            self._legacy_env_override(
                "MT5_MAX_POSITION_PCT",
                proposal,
                float(getattr(self.config, "max_position_pct", 1.0) or 1.0),
            ),
        )
        if capital_cap > 0.0:
            budget = min(budget, capital_cap * position_pct)
        if budget <= 0.0:
            return 0.0
        try:
            loss_per_lot = abs(float(self.gateway.order_calc_profit(
                proposal.symbol, proposal.side, 1.0, entry, proposal.stop_loss
            )))
        except Exception:
            return 0.0
        if loss_per_lot <= 0.0:
            return 0.0
        loss_per_lot = self._sizing_loss_per_lot(proposal, loss_per_lot)
        volume = self.gateway.normalize_volume(
            spec,
            min(
                float(spec.volume_max),
                max(float(spec.volume_min), budget / loss_per_lot),
            ),
        )
        margin = float(self.gateway.order_calc_margin(
            proposal.symbol, proposal.side, volume, entry
        )) if volume > 0 else 0.0
        free_margin = max(0.0, float(getattr(account, "free_margin", 0.0) or 0.0))
        if volume > 0.0 and free_margin > 0.0 and margin > free_margin:
            low = 0.0
            high = float(volume)
            best_volume = 0.0
            for _ in range(20):
                candidate = self.gateway.normalize_volume(spec, (low + high) / 2.0)
                if candidate <= low or candidate <= 0.0:
                    break
                candidate_margin = float(self.gateway.order_calc_margin(
                    proposal.symbol, proposal.side, candidate, entry
                ))
                if candidate_margin <= free_margin:
                    best_volume = candidate
                    low = candidate
                else:
                    high = candidate
            volume = best_volume
        return float(volume)

    def _volume(self, proposal: TradeProposal, entry: float, spec, account) -> float:
        canonical = str(getattr(proposal, "symbol", "") or "").upper().replace(".", "_")
        if canonical != "XAUUSD":
            return self._legacy_volume(proposal, entry, spec, account)

        balance = float(getattr(account, "balance", 0.0) or 0.0)
        if balance <= 0.0:
            balance = float(getattr(account, "equity", 0.0) or 0.0)
        risk_percent = self._risk_percent(proposal.symbol, proposal.asset_class)
        risk_budget = balance * risk_percent / 100.0
        try:
            loss_per_lot = abs(
                float(
                    self.gateway.order_calc_profit(
                        spec.symbol,
                        proposal.side,
                        1.0,
                        float(entry),
                        float(proposal.stop_loss),
                    )
                )
            )
        except Exception:
            return 0.0
        if risk_budget <= 0.0 or loss_per_lot <= 0.0:
            return 0.0
        loss_per_lot = self._sizing_loss_per_lot(proposal, loss_per_lot)
        volume_by_risk = risk_budget / loss_per_lot
        free_margin = float(getattr(account, "free_margin", 0.0) or 0.0)
        leverage = max(1.0, float(getattr(account, "leverage", 1) or 1))
        contract = max(0.0000001, float(getattr(spec, "contract_size", 0.0) or 0.0))
        usage = float(os.getenv("MT5_MARGIN_USAGE_PERCENT", "100") or 100.0)
        usage = min(100.0, max(1.0, usage))
        notional_per_lot = max(entry * contract, 0.0000001)
        volume_by_margin = free_margin * (usage / 100.0) * leverage / notional_per_lot
        max_lot = self._max_lot(proposal.symbol, proposal.asset_class)
        caps = [
            volume_by_risk,
            volume_by_margin,
            float(getattr(spec, "volume_max", volume_by_risk) or volume_by_risk),
        ]
        if max_lot > 0.0:
            caps.append(max_lot)
        requested = min(caps)
        return float(self.gateway.normalize_volume(spec, requested))

    @staticmethod
    def _leg_count(config) -> int:
        # The production env sets this to one to prevent bursts. Keep the
        # existing config contract for isolated tests and controlled overrides.
        raw = os.getenv("MT5_MAX_PYRAMID_TRADES")
        configured = raw if raw is not None else getattr(config, "max_pyramid_trades", 4)
        try:
            return max(1, min(4, int(float(configured))))
        except (TypeError, ValueError):
            return 4

    @staticmethod
    def _entry_drift_reason(proposal: TradeProposal, live_entry: float) -> str:
        setup = proposal.context.get("setup", {}) if isinstance(proposal.context, dict) else {}
        try:
            stop_distance = abs(float(setup.get("stop_distance", 0.0) or 0.0))
            fraction = float(os.getenv("MT5_MAX_ENTRY_DRIFT_STOP_FRACTION", "0.10") or 0.10)
            fraction = min(1.0, max(0.01, fraction))
            proposed = float(proposal.entry_price)
            live = float(live_entry)
        except (TypeError, ValueError):
            return ""
        if stop_distance <= 0.0 or proposed <= 0.0 or live <= 0.0:
            return ""
        allowance = stop_distance * fraction
        adverse = (live - proposed) if proposal.side == "BUY" else (proposed - live)
        if adverse > allowance:
            return (
                f"ENTRY_PRICE_DRIFT:{proposal.side}:"
                f"proposed={proposed:.8f}:live={live:.8f}:"
                f"adverse={adverse:.8f}:allowance={allowance:.8f}"
            )
        return ""


    @staticmethod
    def _effective_leg_count(total_volume: float, requested: int, spec) -> int:
        step = float(getattr(spec, "volume_step", 0.01) or 0.01)
        minimum = float(getattr(spec, "volume_min", step) or step)
        step = max(step, 1e-12)
        minimum_steps = max(1, int(math.ceil((minimum / step) - 1e-9)))
        available_steps = max(0, int(math.floor((float(total_volume) / step) + 1e-9)))
        supported = available_steps // minimum_steps
        return max(1, min(int(requested), supported)) if supported else 0

    @staticmethod
    def _leg_volumes(total_volume: float, count: int, spec) -> list[float]:
        count = max(1, int(count))
        decay = float(os.getenv("MT5_PYRAMID_SIZE_DECAY", "0.70") or 0.70)
        decay = min(1.0, max(0.05, decay))
        weights = [decay ** index for index in range(count)]
        weight_total = sum(weights)
        step = float(getattr(spec, "volume_step", 0.01) or 0.01)
        minimum = float(getattr(spec, "volume_min", step) or step)
        step = max(step, 1e-12)
        minimum_steps = max(1, int(math.ceil((minimum / step) - 1e-9)))
        total_steps = max(0, int(math.floor((float(total_volume) / step) + 1e-9)))
        if total_steps < count * minimum_steps:
            raise ValueError("TOTAL_VOLUME_CANNOT_SUPPORT_REQUESTED_LEGS")

        units = [minimum_steps] * count
        remaining = total_steps - sum(units)
        if remaining:
            raw = [remaining * weight / weight_total for weight in weights]
            extras = [int(math.floor(value)) for value in raw]
            for index, extra in enumerate(extras):
                units[index] += extra
            left = remaining - sum(extras)
            order = sorted(
                range(count),
                key=lambda index: raw[index] - extras[index],
                reverse=True,
            )
            for index in order[:left]:
                units[index] += 1
        return [round(unit * step, 8) for unit in units]

    def _send_leg(
        self,
        proposal: TradeProposal,
        symbol: str,
        volume: float,
        leg_index: int,
    ) -> dict:
        comment = f"cipherfx:{proposal.proposal_id}:leg{leg_index:02d}"
        resolved_symbol, fill_price, broker = self.gateway.place_market_order(
            symbol,
            proposal.side,
            volume,
            proposal.stop_loss,
            proposal.take_profit,
            comment,
        )
        retcode = int(getattr(broker, "retcode", 0) or 0)
        deal_ticket = int(getattr(broker, "deal", 0) or 0)
        return {
            "leg": leg_index,
            "symbol": resolved_symbol,
            "volume": float(volume),
            "retcode": retcode,
            "order_ticket": int(getattr(broker, "order", 0) or 0),
            "deal_ticket": deal_ticket,
            "position_ticket": int(getattr(broker, "position", 0) or 0),
            "fill_price": float(fill_price or 0.0),
            "status": "FILLED" if deal_ticket > 0 and retcode in {10009, 10010} else "REJECTED",
            "reason": str(getattr(broker, "comment", "") or f"MT5_RETCODE_{retcode}"),
        }

    def submit(self, proposal: TradeProposal) -> ExecutionResult:
        with self._lock:
            if proposal.proposal_id in self._claimed or self.database.proposal_seen(proposal.proposal_id):
                return self._result(
                    proposal,
                    "DUPLICATE_SUPPRESSED",
                    reason="PROPOSAL_ID_ALREADY_CLAIMED",
                )
            self._claimed.add(proposal.proposal_id)

        if self._now() >= proposal.expires_at:
            return self._result(proposal, "EXECUTION_BLOCKED", reason="PROPOSAL_EXPIRED")

        gold_loss_reason = self._gold_daily_loss_reason(proposal)
        if gold_loss_reason:
            return self._result(proposal, "EXECUTION_BLOCKED", reason=gold_loss_reason)

        for control_reason in (
            self._symbol_reentry_cooldown_reason(proposal),
            self._setup_duplicate_reason(proposal),
        ):
            if control_reason:
                return self._result(
                    proposal,
                    "EXECUTION_BLOCKED",
                    reason=control_reason,
                )

        try:
            account = self.gateway.account_info()
            if not bool(getattr(account, "terminal_connected", True)):
                return self._result(proposal, "EXECUTION_BLOCKED", reason="BROKER_DISCONNECTED")
            if not bool(getattr(account, "trade_allowed", True)) or not bool(
                getattr(account, "account_trade_allowed", True)
            ):
                return self._result(proposal, "EXECUTION_BLOCKED", reason="TRADING_NOT_ALLOWED")

            positions = list(self.gateway.positions() or [])
            symbol_key = self._symbol_key(proposal.symbol)
            same_symbol = [
                pos for pos in positions
                if self._symbol_key(getattr(pos, "symbol", "")) == symbol_key
            ]
            if same_symbol:
                tickets = ",".join(str(getattr(pos, "ticket", "")) for pos in same_symbol[:5])
                return self._result(
                    proposal,
                    "EXECUTION_BLOCKED",
                    reason=f"SYMBOL_ALREADY_HAS_OPEN_POSITION:{proposal.symbol}:{tickets}",
                )
            max_open = max(1, int(getattr(self.config, "max_open_trades", 30)))
            requested_levels = self._leg_count(self.config)
            levels = requested_levels
            if len(positions) + levels > max_open:
                return self._result(proposal, "EXECUTION_BLOCKED", reason="MAX_OPEN_TRADES")

            resolved, tick = self.gateway.symbol_tick(proposal.symbol)
            entry = float(getattr(tick, "ask" if proposal.side == "BUY" else "bid", 0.0) or 0.0)
            if entry <= 0.0:
                return self._result(proposal, "EXECUTION_BLOCKED", reason="INVALID_LIVE_TICK")
            drift_reason = self._entry_drift_reason(proposal, entry)
            if drift_reason:
                return self._result(proposal, "EXECUTION_BLOCKED", reason=drift_reason)

            spec = self.gateway.symbol_info(resolved)
            total_volume = self._volume(proposal, entry, spec, account)
            levels = self._effective_leg_count(total_volume, requested_levels, spec)
            if levels <= 0:
                return self._result(
                    proposal,
                    "EXECUTION_BLOCKED",
                    volume=total_volume,
                    reason="BROKER_MIN_VOLUME_FOR_LEG_PLAN",
                )
            if total_volume < float(getattr(spec, "volume_min", 0.0) or 0.0):
                return self._result(
                    proposal,
                    "EXECUTION_BLOCKED",
                    volume=total_volume,
                    reason="BROKER_MIN_VOLUME",
                )

            leg_volumes = self._leg_volumes(total_volume, levels, spec)
            if bool(getattr(self.config, "dry_run", False)) or str(
                getattr(self.config, "trade_mode", "")
            ).lower() == "paper":
                legs = [
                    {
                        "leg": index,
                        "volume": volume,
                        "status": "DRY_RUN",
                        "reason": "PAPER_OR_DRY_RUN",
                    }
                    for index, volume in enumerate(leg_volumes, start=1)
                ]
                return self._result(
                    proposal,
                    "DRY_RUN",
                    volume=sum(leg_volumes),
                    reason="PAPER_OR_DRY_RUN",
                    response={"legs": legs, "leg_count": levels},
                )

            legs: list[dict] = []
            for leg_index, volume in enumerate(leg_volumes, start=1):
                _, leg_tick = self.gateway.symbol_tick(resolved)
                leg_entry = float(
                    getattr(leg_tick, "ask" if proposal.side == "BUY" else "bid", 0.0) or 0.0
                )
                leg_drift_reason = self._entry_drift_reason(proposal, leg_entry)
                if leg_drift_reason:
                    legs.append(
                        {
                            "leg": leg_index,
                            "symbol": resolved,
                            "volume": float(volume),
                            "status": "REJECTED",
                            "reason": leg_drift_reason,
                        }
                    )
                    break
                leg = self._send_leg(proposal, resolved, volume, leg_index)
                legs.append(leg)
                if leg["status"] != "FILLED":
                    break

            filled = [leg for leg in legs if leg["status"] == "FILLED"]
            first = filled[0] if filled else (legs[0] if legs else {})
            if not filled:
                return self._result(
                    proposal,
                    "REJECTED",
                    volume=sum(float(leg["volume"]) for leg in legs),
                    reason=first.get("reason", "MT5_ORDER_REJECTED"),
                    response={"legs": legs, "leg_count": levels},
                    order_ticket=first.get("order_ticket", 0),
                    deal_ticket=first.get("deal_ticket", 0),
                    position_ticket=first.get("position_ticket", 0),
                    fill_price=first.get("fill_price", 0.0),
                )

            status_reason = (
                "ALL_LEGS_FILLED"
                if len(filled) == levels
                else f"PARTIAL_BURST_{len(filled)}_OF_{levels}"
            )
            return self._result(
                proposal,
                "FILLED",
                volume=sum(float(leg["volume"]) for leg in filled),
                reason=status_reason,
                response={"legs": legs, "leg_count": levels},
                order_ticket=first.get("order_ticket", 0),
                deal_ticket=first.get("deal_ticket", 0),
                position_ticket=first.get("position_ticket", 0),
                fill_price=first.get("fill_price", 0.0),
            )
        except Exception as exc:
            return self._result(
                proposal,
                "EXECUTION_BLOCKED",
                reason=f"BROKER_ERROR:{type(exc).__name__}:{str(exc)[:180]}",
            )
