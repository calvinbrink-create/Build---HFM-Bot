from __future__ import annotations

import json
import os
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mt5_xm_gateway import MT5Gateway

from .database import DatabaseLayer


def _effective_loss_cap(metrics: dict) -> float:
    """Per-position dollar loss cap that can never sit tighter than the
    trade's own price stop.

    The configured cap (e.g. $150 forex) was force-closing trades at a small
    fraction of their intended 1R risk - a trade sized so the broker stop is
    ~$500 away was being guillotined at -$150 (~0.3R), turning an H4/H1-scoped
    swing entry into a scalp exit and making the 1:1.6 reward geometry
    unreachable. The broker stop at 1R is the real risk control; this cap is
    only a catastrophe backstop for a stop that failed to fill, so it must be
    at least 1.5x the position's own initial risk. If initial risk is unknown
    the configured value still applies as a floor.
    """
    configured = float(metrics.get("max_open_position_loss_usd", 0.0) or 0.0)
    initial_risk = float(metrics.get("initial_risk_amount", 0.0) or 0.0)
    if initial_risk > 0:
        return max(configured, initial_risk * 1.5)
    return configured


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
        missing_loss_cap = sorted(
            symbol
            for symbol, profile in self.profiles.items()
            if isinstance(profile, dict) and float(profile.get("max_open_position_loss_usd", 0.0) or 0.0) <= 0.0
        )
        if missing_loss_cap:
            self.database.event(
                "SYMBOL_PROFILE_MISSING_LOSS_CAP",
                {
                    "symbols": missing_loss_cap,
                    "reason": "max_open_position_loss_usd is absent or zero for these symbols - "
                              "the per-position dollar loss auto-close safety net is disabled for them",
                },
            )
        # Raised from 0.30/0.50/0.60: moving to breakeven/trailing this early
        # was capping winners at ~0.5R average while the entry logic designs
        # for a 1.6-1.8:1 reward:risk target - these defaults now give a
        # trade room to develop before protecting profit, per standard
        # practice (wait for >=1R, ideally more, before breakeven).
        self.default_breakeven_r = float(os.getenv("MT5_MANAGEMENT_BREAKEVEN_R", "1.0"))
        self.default_trail_start_r = float(os.getenv("MT5_MANAGEMENT_TRAIL_START_R", "1.2"))
        self.default_profit_take_r = max(
            0.0, float(os.getenv("MT5_PROFIT_TAKE_R", "1.5"))
        )
        self.trail_distance_r = max(
            0.05, float(os.getenv("MT5_MANAGEMENT_TRAIL_DISTANCE_R", "0.25"))
        )
        self.early_exit_r = float(os.getenv("MT5_MANAGEMENT_EARLY_EXIT_R", "-0.75"))
        # Giveback guard: protects a favorable move before breakeven-lock
        # (1.0R) would otherwise kick in. Was fully configured in env but
        # never wired into any code path - between 0 and 1.0R a trade could
        # run to +0.9R and reverse all the way to a loss with nothing
        # stopping it. Confirmed live 2026-07-20 user request.
        self.giveback_guard_enabled = str(os.getenv("MT5_GIVEBACK_GUARD_ENABLE", "1")).strip().lower() not in {"0", "false", "no"}
        self.giveback_trigger_r = float(os.getenv("MT5_GIVEBACK_TRIGGER_R", "0.70"))
        self.giveback_max_r = max(0.01, float(os.getenv("MT5_GIVEBACK_MAX_R", "0.20")))
        self.modify_retry_seconds = max(
            5.0, float(os.getenv("MT5_MANAGEMENT_MODIFY_RETRY_SECONDS", "30"))
        )
        self.close_retry_seconds = max(
            5.0, float(os.getenv("MT5_MANAGEMENT_CLOSE_RETRY_SECONDS", "30"))
        )
        self.monitor_event_seconds = max(
            1.0, float(os.getenv("MT5_MANAGEMENT_EVENT_INTERVAL_SECONDS", "5"))
        )
        self._last_portfolio_event_at = 0.0

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
            tick_reader = getattr(self.gateway, "position_symbol_tick", None)
            info_reader = getattr(self.gateway, "position_symbol_info", None)
            _, tick = (tick_reader or self.gateway.symbol_tick)(symbol)
            spec = (info_reader or self.gateway.symbol_info)(symbol)
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
        now = datetime.now(timezone.utc)
        failed_at = state.get("last_close_attempt_at")
        if failed_at:
            try:
                age = (now - datetime.fromisoformat(str(failed_at))).total_seconds()
            except ValueError:
                age = self.close_retry_seconds
            if age < self.close_retry_seconds:
                return
        try:
            self.gateway.close_position(position)
            state["close_requested"] = True
            state.pop("last_close_attempt_at", None)
            state.pop("last_close_error", None)
            self._action(position, state, "CLOSE", reason, metrics=metrics)
        except Exception as exc:
            state["last_close_attempt_at"] = now.isoformat()
            state["last_close_error"] = f"{type(exc).__name__}:{str(exc)[:180]}"
            self.database.event(
                "POSITION_MANAGEMENT_ERROR",
                {
                    "ticket": int(position.ticket),
                    "symbol": position.symbol,
                    "action": "CLOSE",
                    "reason": reason,
                    "error": state["last_close_error"],
                    "retry_after_seconds": self.close_retry_seconds,
                },
                symbol=position.symbol,
            )

    def _modify(self, position, state: dict[str, Any], new_sl: float, reason: str, metrics):
        now = datetime.now(timezone.utc)
        failed_target = state.get("last_failed_sl")
        failed_at = state.get("last_modify_attempt_at")
        if failed_target is not None and failed_at:
            try:
                age = (now - datetime.fromisoformat(str(failed_at))).total_seconds()
            except ValueError:
                age = self.modify_retry_seconds
            tolerance = max(abs(float(new_sl)) * 1e-8, 1e-8)
            if age < self.modify_retry_seconds and abs(float(failed_target) - float(new_sl)) <= tolerance:
                return
        try:
            info_reader = getattr(self.gateway, "position_symbol_info", None)
            spec = (info_reader or self.gateway.symbol_info)(position.symbol)
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
            # The broker-minimum-distance clamp above reads a second, later
            # quote than the one the caller used to decide this modify
            # improves the stop. An adverse tick between those two reads can
            # clamp new_sl past the position's current live stop - re-check
            # here so we never submit a modify that widens risk instead of
            # protecting it.
            current_sl = float(getattr(position, "sl", 0.0) or 0.0)
            if current_sl > 0:
                still_improves = (
                    new_sl > current_sl if position.direction == "BUY" else new_sl < current_sl
                )
                if not still_improves:
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
        # Repair a stale/racey baseline: the execution's position_ticket can be
        # 0 at the instant management first snapshots a just-filled position
        # (it is reconciled to the real broker ticket a moment later), so
        # position_baseline() returns nothing and initial_risk_amount gets
        # cached as 0 forever. That zero silently disables the risk-scaled
        # loss cap AND the R-based exits, letting a swing trade get scalp-cut
        # at the tiny fixed dollar cap. Re-fetch from the baseline while the
        # cached risk/stop are still missing.
        if float(state.get("initial_risk_amount", 0.0) or 0.0) <= 0.0 or float(state.get("initial_sl", 0.0) or 0.0) <= 0.0:
            baseline = self.database.position_baseline(int(position.ticket)) or {}
            repaired_risk = float(baseline.get("initial_risk") or 0.0)
            repaired_sl = float(baseline.get("stop_loss") or 0.0)
            if repaired_risk > 0:
                state["initial_risk_amount"] = repaired_risk
            if repaired_sl > 0 and float(state.get("initial_sl", 0.0) or 0.0) <= 0.0:
                state["initial_sl"] = repaired_sl

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
        peak_r = max(float(state.get("mfe_r", 0.0) or 0.0), float(current_r or 0.0))
        prior_mae_r = abs(float(state.get("mae_r", 0.0) or 0.0))
        adverse_r = max(prior_mae_r, max(0.0, -float(current_r or 0.0)))
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
            "mae_r": adverse_r,
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
            "max_open_position_loss_usd": self._profile_float(position.symbol, "max_open_position_loss_usd", 0.0),
            "recovery_status": (
                "RECOVERY_MONITORING" if current_r is not None and current_r < 0
                else "PROFIT_PROTECTION" if current_r is not None and current_r > 0
                else "NEUTRAL"
            ),
            "account": dict(portfolio or {}),
        }
        rank = self._rank(current_r, drawdown_r, spread_jump)
        metrics["rank"] = rank

        if not state.get("close_requested"):
            live_sl = float(getattr(position, "sl", 0.0) or 0.0)
            if initial_sl <= 0 or live_sl <= 0:
                # A live stop of 0 means the broker has no protective stop on
                # this position right now (cleared, never attached, or lost in
                # a modify) regardless of side - comparing price against 0.0
                # would be a no-op for BUY and a false-positive for SELL, so
                # treat it as unmanaged rather than as a directional breach.
                self._close(position, state, "UNMANAGED_POSITION_NO_STOP", metrics)
            elif (
                (position.direction == "BUY" and position.price_current <= live_sl)
                or (position.direction == "SELL" and position.price_current >= live_sl)
            ):
                self._close(position, state, "STOP_BREACHED_POSITION_STILL_OPEN", metrics)
            elif (
                _effective_loss_cap(metrics) > 0
                and float(position.profit) <= -_effective_loss_cap(metrics)
            ):
                self._close(position, state, "MAX_OPEN_POSITION_LOSS", metrics)
            elif (
                self._profile_float(position.symbol, "max_duration_minutes", 0.0) > 0
                and time_in_trade >= self._profile_float(position.symbol, "max_duration_minutes", 0.0) * 60.0
            ):
                self._close(position, state, "MAX_DURATION_EXCEEDED", metrics)
            elif (
                self.giveback_guard_enabled
                and peak_r >= self.giveback_trigger_r
                and current_r is not None
                and (peak_r - current_r) >= self.giveback_max_r
            ):
                self._close(position, state, "GIVEBACK_GUARD_TRIGGERED", metrics)
            else:
                if current_r is not None and current_r <= self.early_exit_r + 1e-9:
                    self._close(position, state, "RECOVERY_DANGER_THRESHOLD", metrics)
                elif (
                    current_r is not None
                    and current_r >= self._profile_float(
                        position.symbol, "profit_take_r", self.default_profit_take_r
                    )
                    and float(position.profit or 0.0) > 0.0
                ):
                    self._close(position, state, "PROFIT_TAKE_R_REACHED", metrics)
                elif current_r is not None and current_r >= self._profile_float(
                    position.symbol, "trail_start_r", self.default_trail_start_r
                ):
                    trail_distance = abs(
                        initial_sl - float(state.get("initial_entry", position.price_open))
                    )
                    desired_sl = (
                        float(position.price_current)
                        - trail_distance * self.trail_distance_r
                        if position.direction == "BUY"
                        else float(position.price_current)
                        + trail_distance * self.trail_distance_r
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
                        buffer = self._breakeven_buffer_price(position.symbol)
                        locked_sl = (
                            float(position.price_open) + buffer
                            if position.direction == "BUY"
                            else float(position.price_open) - buffer
                        )
                        self._modify(position, state, locked_sl, "BREAK_EVEN", metrics)

        last_monitor_event_at = state.get("last_monitor_event_at")
        emit_monitor_event = True
        if last_monitor_event_at:
            try:
                emit_monitor_event = (
                    now - datetime.fromisoformat(str(last_monitor_event_at))
                ).total_seconds() >= self.monitor_event_seconds
            except ValueError:
                emit_monitor_event = True
        if emit_monitor_event:
            state["last_monitor_event_at"] = now.isoformat()

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
        if emit_monitor_event:
            self.database.event("POSITION_MONITOR", metrics, symbol=position.symbol)
        return metrics

    def _breakeven_buffer_price(self, symbol: str) -> float:
        # A stop parked at the exact entry tick gets whipsawed closed by
        # ordinary price noise within minutes (confirmed live 2026-07-20:
        # EU50Cash/EURUSD legs locked to exact breakeven closed for $0.00
        # 1-4 minutes later). Move the stop a few points past entry, in the
        # trade's favor, so normal noise can't touch it.
        points = max(0.0, float(os.getenv("MT5_BREAKEVEN_BUFFER_POINTS", "2")))
        if points <= 0:
            return 0.0
        try:
            spec = self.gateway.symbol_info(symbol)
            return points * float(spec.point)
        except Exception:
            return 0.0

    def _lock_earlier_pyramid_legs_to_breakeven(self, positions):
        # Each pyramid leg is its own independent MT5 position (own SL, own
        # exit cycle) - nothing else ties them together. Without this, a
        # later leg can hit its own stop and lose while an earlier, still-
        # profitable leg keeps running untouched, netting the "pyramid" to
        # roughly breakeven instead of compounding one correct trade idea.
        # Covers positions already open before this protection was added, in
        # addition to the same lock applied at fill time for new legs.
        if str(os.getenv("MT5_PYRAMID_LOCK_PRIOR_LEGS_BREAKEVEN", "1")).strip().lower() in {"0", "false", "no"}:
            return
        groups: dict[tuple[str, str], list] = {}
        for position in positions:
            key = (str(position.symbol).upper(), str(position.direction).upper())
            groups.setdefault(key, []).append(position)
        for (symbol, direction), members in groups.items():
            if len(members) < 2:
                continue
            members_with_time = [p for p in members if getattr(p, "time", None)]
            if len(members_with_time) < 2:
                continue
            newest = max(members_with_time, key=lambda p: p.time)
            for position in members_with_time:
                if position.ticket == newest.ticket:
                    continue
                if float(getattr(position, "profit", 0.0) or 0.0) <= 0.0:
                    continue
                entry = float(position.price_open)
                sl = float(position.sl)
                improves = entry > sl if direction == "BUY" else entry < sl
                if not improves:
                    continue
                buffer = self._breakeven_buffer_price(position.symbol)
                locked_sl = entry + buffer if direction == "BUY" else entry - buffer
                try:
                    self.gateway.modify_position(int(position.ticket), position.symbol, locked_sl, float(position.tp))
                    self.database.event(
                        "PYRAMID_PRIOR_LEG_BREAKEVEN_LOCKED",
                        {"sl": locked_sl, "reason": "existing_leg_sweep"},
                        symbol=position.symbol,
                    )
                except Exception as exc:
                    self.database.event(
                        "PYRAMID_BREAKEVEN_LOCK_FAILED",
                        {"ticket": int(position.ticket), "error": f"{type(exc).__name__}:{str(exc)[:180]}"},
                        symbol=position.symbol,
                    )

    def monitor(self):
        positions = self.gateway.positions()
        try:
            self._lock_earlier_pyramid_legs_to_breakeven(positions)
        except Exception as exc:
            self.database.event(
                "PYRAMID_BREAKEVEN_SWEEP_FAILED",
                {"error": f"{type(exc).__name__}:{str(exc)[:180]}"},
            )
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
        metrics = []
        for position in positions:
            try:
                metrics.append(self._monitor_position(position, len(positions), portfolio))
            except Exception as exc:
                # One bad position/state row must not stop trailing, breakeven,
                # stop-breach detection, and the $-loss cap for every other
                # currently open position this cycle.
                self.database.event(
                    "POSITION_MONITOR_FAILED",
                    {
                        "ticket": int(getattr(position, "ticket", 0) or 0),
                        "error": f"{type(exc).__name__}:{str(exc)[:180]}",
                    },
                    symbol=str(getattr(position, "symbol", "") or ""),
                )
        portfolio["open_positions"] = len(positions)
        portfolio["portfolio_heat_usd"] = sum(
            float(item.get("initial_risk_amount", 0.0) or 0.0) for item in metrics
        )
        self.database.status("open_positions", metrics)
        self.database.status("position_intelligence", metrics)
        if time.monotonic() - self._last_portfolio_event_at >= self.monitor_event_seconds:
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
            self._last_portfolio_event_at = time.monotonic()
        for position in positions:
            self.database.save_position(position)
        return positions
