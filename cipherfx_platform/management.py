from __future__ import annotations

import json
import os
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mt5_xm_gateway import MT5Gateway

from .database import DatabaseLayer


class TradeManagementEngine:
    """Monitors and manages open broker positions only."""

    def __init__(self, gateway: MT5Gateway, database: DatabaseLayer):
        self.gateway = gateway
        self.database = database
        profile_path = Path(
            os.getenv(
                "CIPHERFX_PROFILE_PATH",
                str(Path(__file__).resolve().parent.parent / "config" / "symbol_profiles.json"),
            )
        )
        try:
            payload = json.loads(profile_path.read_text())
            self.profiles = payload.get("symbols", {})
        except (OSError, json.JSONDecodeError):
            self.profiles = {}
        self.default_breakeven_r = float(os.getenv("MT5_MANAGEMENT_BREAKEVEN_R", "0.30"))
        self.default_trail_start_r = float(os.getenv("MT5_MANAGEMENT_TRAIL_START_R", "0.50"))
        self.trail_distance_r = max(
            0.05, float(os.getenv("MT5_MANAGEMENT_TRAIL_DISTANCE_R", "0.25"))
        )
        self.early_exit_r = float(os.getenv("MT5_MANAGEMENT_EARLY_EXIT_R", "-0.75"))
        self.modify_retry_seconds = max(
            5.0, float(os.getenv("MT5_MANAGEMENT_MODIFY_RETRY_SECONDS", "30"))
        )

    def _profile(self, symbol: str) -> dict[str, Any]:
        key = str(symbol).upper()
        candidates = [key]
        if key.endswith("CASH"):
            candidates.append(key[:-4])
        if key.endswith(".CASH"):
            candidates.append(key[:-5])
        for candidate in candidates:
            profile = self.profiles.get(candidate)
            if isinstance(profile, dict):
                return profile
        return {}

    def _profile_float(self, symbol: str, field: str, default: float) -> float:
        value = self._profile(symbol).get(field, default)
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(default)

    @staticmethod
    def _session(now: datetime) -> str:
        hour = now.hour
        if 0 <= hour < 7:
            return "Asia"
        if 7 <= hour < 12:
            return "London"
        if 12 <= hour < 16:
            return "overlap"
        if 16 <= hour < 22:
            return "New York"
        return "unknown"

    @staticmethod
    def _r_multiple(position, initial_sl: float) -> float | None:
        risk = abs(float(position.price_open) - float(initial_sl))
        if risk <= 0:
            return None
        if str(position.direction).upper() == "BUY":
            return (float(position.price_current) - float(position.price_open)) / risk
        return (float(position.price_open) - float(position.price_current)) / risk

    @staticmethod
    def _price_volatility(history: list[float]) -> float | None:
        if len(history) < 3:
            return None
        changes = [
            float(history[index]) - float(history[index - 1])
            for index in range(1, len(history))
        ]
        return float(statistics.pstdev(changes)) if changes else None

    @staticmethod
    def _rank(current_r: float | None, drawdown_r: float, spread_jump: bool) -> str:
        if current_r is None:
            return "CRITICAL"
        if current_r <= -1.0 or (spread_jump and current_r < 0):
            return "CRITICAL"
        if current_r <= -0.75:
            return "DANGER"
        if current_r < -0.20:
            return "WEAK"
        if current_r < 0.20:
            return "NEUTRAL"
        if current_r < 0.50:
            return "HEALTHY"
        if drawdown_r > 0.35:
            return "HEALTHY"
        if current_r < 1.0:
            return "STRONG"
        return "EXCELLENT"

    def _quote(self, symbol: str):
        try:
            _, tick = self.gateway.symbol_tick(symbol)
            spec = self.gateway.symbol_info(symbol)
            points = (float(tick.ask) - float(tick.bid)) / max(float(spec.point), 1e-12)
            return points, float(tick.bid), float(tick.ask), spec
        except Exception:
            return None, None, None, None

    def _action(self, position, state: dict[str, Any], action: str, reason: str, **extra):
        payload = {
            "ticket": int(position.ticket),
            "symbol": position.symbol,
            "side": position.direction,
            "action": action,
            "reason": reason,
            **extra,
        }
        self.database.event(
            "POSITION_MANAGEMENT_ACTION",
            payload,
            symbol=position.symbol,
        )
        state["last_action"] = action
        state["last_action_reason"] = reason
        state["last_action_at"] = datetime.now(timezone.utc).isoformat()
        action_log = list(state.get("action_log", []))
        action_log.append(dict(payload, action_at=state["last_action_at"]))
        state["action_log"] = action_log[-100:]

    def _close(self, position, state: dict[str, Any], reason: str, metrics: dict[str, Any]) -> None:
        try:
            self.gateway.close_position(position)
            state["close_requested"] = True
            self._action(position, state, "CLOSE", reason, metrics=metrics)
        except Exception as exc:
            self.database.event(
                "POSITION_MANAGEMENT_ERROR",
                {
                    "ticket": int(position.ticket),
                    "symbol": position.symbol,
                    "action": "CLOSE",
                    "reason": reason,
                    "error": f"{type(exc).__name__}:{str(exc)[:180]}",
                },
                symbol=position.symbol,
            )

    def _modify(self, position, state: dict[str, Any], new_sl: float, reason: str, metrics):
        now = datetime.now(timezone.utc)
        try:
            spec = self.gateway.symbol_info(position.symbol)
            normalizer = getattr(self.gateway, "normalize_price", None)
            if normalizer is None:
                digits = max(0, int(getattr(spec, "digits", 8) or 8))
                normalizer = lambda value, price: round(float(price), digits)
            new_sl = float(normalizer(spec, new_sl))
            minimum_distance = float(getattr(spec, "stops_level", 0) or 0) * float(
                getattr(spec, "point", 0.0) or 0.0
            )
            _, bid, ask, _ = self._quote(position.symbol)
            if minimum_distance > 0 and bid is not None and ask is not None:
                if position.direction == "BUY":
                    new_sl = min(new_sl, float(bid) - minimum_distance)
                else:
                    new_sl = max(new_sl, float(ask) + minimum_distance)
                new_sl = float(normalizer(spec, new_sl))
            failed_target = state.get("last_failed_sl")
            failed_at = state.get("last_modify_attempt_at")
            if failed_target is not None and failed_at:
                try:
                    age = (now - datetime.fromisoformat(str(failed_at))).total_seconds()
                except ValueError:
                    age = self.modify_retry_seconds
                if age < self.modify_retry_seconds and abs(float(failed_target) - new_sl) <= max(
                    float(getattr(spec, "point", 0.0) or 0.0), 1e-12
                ):
                    return
            self.gateway.modify_position(
                int(position.ticket), position.symbol, float(new_sl), float(position.tp)
            )
            state["last_managed_sl"] = float(new_sl)
            state.pop("last_failed_sl", None)
            state.pop("last_modify_error", None)
            self._action(
                position,
                state,
                "MODIFY_SL",
                reason,
                new_sl=float(new_sl),
                metrics=metrics,
            )
        except Exception as exc:
            state["last_failed_sl"] = float(new_sl)
            state["last_modify_attempt_at"] = now.isoformat()
            state["last_modify_error"] = f"{type(exc).__name__}:{str(exc)[:180]}"
            self.database.event(
                "POSITION_MANAGEMENT_ERROR",
                {
                    "ticket": int(position.ticket),
                    "symbol": position.symbol,
                    "action": "MODIFY_SL",
                    "reason": reason,
                    "error": state["last_modify_error"],
                    "retry_after_seconds": self.modify_retry_seconds,
                },
                symbol=position.symbol,
            )

    def _initial_state(self, position, now: datetime) -> dict[str, Any]:
        baseline = self.database.position_baseline(int(position.ticket)) or {}
        entry = float(baseline.get("entry_price") or position.price_open)
        initial_sl = float(baseline.get("stop_loss") or position.sl)
        initial_tp = float(baseline.get("take_profit") or position.tp)
        return {
            "ticket": int(position.ticket),
            "symbol": position.symbol,
            "side": position.direction,
            "initial_entry": entry,
            "initial_sl": initial_sl,
            "initial_tp": initial_tp,
            "initial_risk_amount": float(baseline.get("initial_risk") or 0.0),
            "first_seen_at": now.isoformat(),
            "last_seen_at": now.isoformat(),
            "last_price": float(position.price_current),
            "price_history": [float(position.price_current)],
            "last_spread_points": None,
            "mfe_r": 0.0,
            "mae_r": 0.0,
            "last_managed_sl": float(position.sl),
            "close_requested": False,
            "last_action": "NONE",
            "action_log": [],
        }

    def _monitor_position(
        self,
        position,
        total_positions: int,
        portfolio: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        state = self.database.load_management_state(int(position.ticket))
        if not state:
            state = self._initial_state(position, now)

        initial_sl = float(state.get("initial_sl", 0.0) or 0.0)
        current_r = self._r_multiple(position, initial_sl)
        previous_price = float(state.get("last_price", position.price_current) or position.price_current)
        spread_points, bid, ask, spec = self._quote(position.symbol)
        previous_spread = state.get("last_spread_points")
        spread_jump = (
            spread_points is not None
            and previous_spread is not None
            and spread_points > float(previous_spread) * 1.5
        )
        quote_delta = float(position.price_current) - previous_price
        favorable_delta = (
            quote_delta if str(position.direction).upper() == "BUY" else -quote_delta
        )
        peak_r = max(float(state.get("mfe_r", 0.0)), float(current_r or 0.0))
        adverse_r = min(float(state.get("mae_r", 0.0)), float(current_r or 0.0))
        drawdown_r = max(0.0, peak_r - float(current_r or 0.0))
        price_history = [
            float(value) for value in state.get("price_history", [])
            if isinstance(value, (int, float))
        ]
        price_history.append(float(position.price_current))
        price_history = price_history[-120:]
        volatility = self._price_volatility(price_history)
        previous_r = state.get("last_r")
        velocity_r = (
            float(current_r) - float(previous_r)
            if current_r is not None and previous_r is not None
            else 0.0
        )
        previous_velocity = float(state.get("last_velocity_r", 0.0) or 0.0)
        profit_acceleration_r = velocity_r - previous_velocity
        recent_changes = [
            price_history[index] - price_history[index - 1]
            for index in range(max(1, len(price_history) - 4), len(price_history))
        ]
        favorable_changes = [
            change if str(position.direction).upper() == "BUY" else -change
            for change in recent_changes
        ]
        favorable_change_count = sum(change > 0 for change in favorable_changes)
        trend_continuation = (
            "FAVORABLE"
            if favorable_changes and favorable_change_count >= len(favorable_changes) / 2
            else "ADVERSE"
            if favorable_changes
            else "UNKNOWN"
        )
        try:
            opened_at = position.time
            time_in_trade = max(0.0, (now - opened_at).total_seconds())
        except Exception:
            time_in_trade = 0.0
        time_decay = (
            "NEGATIVE"
            if time_in_trade >= 300 and (current_r or 0.0) <= 0.0
            else "POSITIVE"
            if (current_r or 0.0) > 0.0
            else "NEUTRAL"
        )

        metrics = {
            "ticket": int(position.ticket),
            "symbol": position.symbol,
            "side": position.direction,
            "current_profit": float(position.profit),
            "r_multiple": current_r,
            "drawdown_r": drawdown_r,
            "mfe_r": peak_r,
            "mae_r": abs(adverse_r),
            "time_in_trade_seconds": time_in_trade,
            "spread_points": spread_points,
            "spread_changed": spread_jump,
            "quote_delta": quote_delta,
            "price_change_direction": (
                "FAVORABLE" if favorable_delta > 0 else "ADVERSE" if favorable_delta < 0 else "FLAT"
            ),
            "distance_to_tp": abs(float(position.tp) - float(position.price_current)),
            "distance_to_sl": abs(float(position.price_current) - float(position.sl)),
            "price_risk_exposure": abs(float(position.price_open) - initial_sl) * float(position.volume),
            "portfolio_open_positions": total_positions,
            "session": self._session(now),
            "volatility": volatility,
            "liquidity_event": "SPREAD_EXPANSION" if spread_jump else None,
            "momentum": favorable_delta,
            "market_momentum": trend_continuation,
            "trend_continuation": trend_continuation,
            "profit_velocity_r": velocity_r,
            "profit_acceleration_r": profit_acceleration_r,
            "time_decay": time_decay,
            "market_behavior": "SPREAD_EXPANSION" if spread_jump else trend_continuation,
            "initial_risk_amount": float(state.get("initial_risk_amount", 0.0) or 0.0),
            "account": dict(portfolio or {}),
        }
        rank = self._rank(current_r, drawdown_r, spread_jump)
        metrics["rank"] = rank

        if not state.get("close_requested"):
            if initial_sl <= 0:
                self._close(position, state, "UNMANAGED_POSITION_NO_STOP", metrics)
            elif (
                (position.direction == "BUY" and position.price_current <= position.sl)
                or (position.direction == "SELL" and position.price_current >= position.sl)
            ):
                self._close(position, state, "STOP_BREACHED_POSITION_STILL_OPEN", metrics)
            else:
                max_minutes = self._profile_float(position.symbol, "max_duration_minutes", 0.0)
                if (
                    max_minutes > 0
                    and time_in_trade >= max_minutes * 60.0
                    and current_r is not None
                    and current_r <= self.early_exit_r
                ):
                    self._close(position, state, "TIME_DECAY_RECOVERY_DETERIORATED", metrics)
                elif current_r is not None and current_r >= self._profile_float(
                    position.symbol, "trail_start_r", self.default_trail_start_r
                ):
                    trail_distance = abs(initial_sl - float(state.get("initial_entry", position.price_open)))
                    desired_sl = (
                        float(position.price_current) - trail_distance * self.trail_distance_r
                        if position.direction == "BUY"
                        else float(position.price_current) + trail_distance * self.trail_distance_r
                    )
                    improves = (
                        desired_sl > float(position.sl)
                        if position.direction == "BUY"
                        else desired_sl < float(position.sl)
                    )
                    if improves:
                        self._modify(position, state, desired_sl, "PROFIT_TRAIL", metrics)
                elif current_r is not None and current_r >= self._profile_float(
                    position.symbol, "breakeven_trigger_r", self.default_breakeven_r
                ):
                    improves = (
                        float(position.price_open) > float(position.sl)
                        if position.direction == "BUY"
                        else float(position.price_open) < float(position.sl)
                    )
                    if improves:
                        self._modify(position, state, float(position.price_open), "BREAK_EVEN", metrics)

        state.update(
            {
                "last_seen_at": now.isoformat(),
                "last_price": float(position.price_current),
                "last_spread_points": spread_points,
                "mfe_r": peak_r,
                "mae_r": adverse_r,
                "last_profit": float(position.profit),
                "price_history": price_history,
                "last_r": current_r,
                "last_velocity_r": velocity_r,
                "last_profit_acceleration_r": profit_acceleration_r,
                "last_metrics": metrics,
                "rank": rank,
                "time_in_trade_seconds": time_in_trade,
            }
        )
        self.database.save_management_state(int(position.ticket), state)
        self.database.event("POSITION_MONITOR", metrics, symbol=position.symbol)
        return metrics

    def monitor(self):
        positions = self.gateway.positions()
        try:
            account = self.gateway.account_info()
            portfolio = {
                "equity": float(getattr(account, "equity", 0.0) or 0.0),
                "balance": float(getattr(account, "balance", 0.0) or 0.0),
                "margin": float(getattr(account, "margin", 0.0) or 0.0),
                "free_margin": float(getattr(account, "free_margin", 0.0) or 0.0),
                "margin_level": float(getattr(account, "margin_level", 0.0) or 0.0),
            }
        except Exception:
            portfolio = {}
        metrics = [
            self._monitor_position(position, len(positions), portfolio)
            for position in positions
        ]
        portfolio["open_positions"] = len(positions)
        portfolio["portfolio_heat_usd"] = sum(
            float(item.get("initial_risk_amount", 0.0) or 0.0) for item in metrics
        )
        self.database.status("open_positions", metrics)
        self.database.status("position_intelligence", metrics)
        self.database.event(
            "POSITION_PORTFOLIO_MONITOR",
            {
                "open_positions": len(positions),
                "risk_exposure_price": sum(
                    float(item.get("price_risk_exposure", 0.0) or 0.0) for item in metrics
                ),
                "portfolio_heat_usd": portfolio["portfolio_heat_usd"],
                "portfolio": portfolio,
                "critical": sum(item.get("rank") == "CRITICAL" for item in metrics),
                "danger": sum(item.get("rank") == "DANGER" for item in metrics),
            },
        )
        for position in positions:
            self.database.save_position(position)
        return positions

    def snapshot(self):
        return self.monitor()

    def close(self, ticket: int):
        for position in self.gateway.positions():
            if int(position.ticket) == int(ticket):
                return self.gateway.close_position(position)
        raise LookupError(f"open position not found: {ticket}")

    def reconcile(self):
        self.database.event(
            "POSITION_RECONCILED",
            {"count": len(self.gateway.positions())},
        )
