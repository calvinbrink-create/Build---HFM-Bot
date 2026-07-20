from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from mt5_xm_config import MT5RuntimeConfig
from mt5_xm_gateway import MT5Gateway

from .contracts import ExecutionResult, TradeProposal, SIDES
from .database import DatabaseLayer
from .market_sessions import market_window

LOG = logging.getLogger("cipherfx.platform.execution")


@dataclass(frozen=True)
class ExecutionLimits:
    risk_pct: float = 0.25
    max_risk_pct: float = 1.0
    max_spread_points: float = 0.0


# proposal.asset_class uses the singular runtime.ASSET_GROUPS values
# ("forex"/"index"/"metal"); the env vars configured in scalpbot-mt5.env use
# the plural suffixes below. Symbol-specific overrides win over asset-class
# overrides, which win over the flat default.
_ASSET_CLASS_ENV_SUFFIX = {"forex": "FOREX", "index": "INDICES", "metal": "METALS"}

# Instruments whose price action moves together closely enough that scoring
# them independently every scan cycle produces simultaneous, correlated
# entries rather than independent evidence. Canonical (pre-broker-suffix)
# symbol names, matching MT5RuntimeConfig.canonical_symbol()/mt5_symbols.json.
CORRELATION_GROUPS = (
    frozenset({"EU50", "FRA40", "GER40", "UK100"}),
    frozenset({"NAS100", "US30", "SPX500"}),
    frozenset({"XAUUSD", "XAGUSD"}),
)


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

    def _canonical_symbol(self, symbol: str) -> str:
        resolver = getattr(self.config, "canonical_symbol", None)
        try:
            value = resolver(symbol) if callable(resolver) else symbol
        except Exception:
            value = symbol
        return str(value or "").strip().upper()

    def _env_override(self, prefix: str, proposal: TradeProposal, default: float) -> float:
        symbol = self._canonical_symbol(proposal.symbol)
        asset_suffix = _ASSET_CLASS_ENV_SUFFIX.get(str(proposal.asset_class or "").lower(), "")
        for key in (f"{prefix}_{symbol}", f"{prefix}_{asset_suffix}" if asset_suffix else ""):
            if not key:
                continue
            raw = os.getenv(key)
            if raw is None:
                continue
            try:
                return float(raw)
            except (TypeError, ValueError):
                continue
        return default

    def _volume(self, proposal: TradeProposal, entry, spec, account, pyramid_leg_index: int = 0):
        budget = max(0.0, float(getattr(account, "equity", 0.0) or 0.0))
        risk_pct = self._env_override("MT5_RISK_PCT", proposal, self.limits.risk_pct)
        max_risk_pct = self._env_override("MT5_MAX_RISK_PCT", proposal, self.limits.max_risk_pct)
        budget *= min(risk_pct, max_risk_pct) / 100.0
        # Pyramid legs scale down geometrically: leg 1 (index 0) gets full
        # size, leg 2 gets size*decay, leg 3 gets size*decay^2, etc. Without
        # this every add-on leg risked the same dollars as the first entry,
        # so an 8-leg pyramid could compound to 8x the intended risk on one
        # symbol. decay=1.0 (env override) restores the old flat behavior.
        if pyramid_leg_index > 0:
            decay = max(0.05, min(1.0, self._env_override("MT5_PYRAMID_SIZE_DECAY", proposal, 0.70)))
            budget *= decay ** pyramid_leg_index
        # capital_cap_usd/max_position_pct are an absolute ceiling on a single
        # position's risk budget, independent of the equity-based sizing
        # above - they don't scale up just because equity grows.
        capital_cap = max(0.0, float(getattr(self.config, "capital_cap_usd", 0.0) or 0.0))
        default_position_pct = float(getattr(self.config, "max_position_pct", 1.0) or 1.0)
        position_pct = max(0.0, self._env_override("MT5_MAX_POSITION_PCT", proposal, default_position_pct))
        if capital_cap > 0:
            budget = min(budget, capital_cap * position_pct)
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

        # Fit the already risk-sized request to current broker free margin.
        # This changes only volume; it never bypasses the broker margin check
        # and never changes the immutable proposal geometry.
        free_margin = max(0.0, float(getattr(account, "free_margin", 0.0) or 0.0))
        if volume > 0 and free_margin > 0.0 and margin > free_margin:
            low = 0.0
            high = float(volume)
            best_volume = 0.0
            best_margin = 0.0
            for _ in range(20):
                candidate = self.gateway.normalize_volume(spec, (low + high) / 2.0)
                if candidate <= low or candidate <= 0.0:
                    break
                candidate_margin = float(self.gateway.order_calc_margin(
                    proposal.symbol, proposal.side, candidate, entry
                ))
                if candidate_margin <= free_margin:
                    best_volume = candidate
                    best_margin = candidate_margin
                    low = candidate
                else:
                    high = candidate
            if best_volume > 0.0:
                volume = best_volume
                margin = best_margin
            else:
                volume = 0.0
            initial_risk = abs(float(self.gateway.order_calc_profit(
                proposal.symbol, proposal.side, volume, entry, proposal.stop_loss
            ))) if volume > 0 else 0.0
        return volume, margin, initial_risk

    def _loss_cooldown_reason(self, proposal: TradeProposal) -> str:
        seconds = max(0, int(getattr(self.config, "loss_cooldown_seconds", 0) or 0))
        if seconds <= 0:
            return ""
        engine = str(proposal.context.get("engine", proposal.strategy_name) or "").upper()
        latest = self.database.latest_loss_for(self._canonical_symbol(proposal.symbol), engine)
        if not latest:
            return ""
        try:
            closed_at = datetime.fromisoformat(str(latest["closed_at"]))
            if closed_at.tzinfo is None:
                closed_at = closed_at.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return ""
        remaining = seconds - int((datetime.now(timezone.utc) - closed_at).total_seconds())
        return f"LOSS_COOLDOWN_ACTIVE:{max(1, remaining)}" if remaining > 0 else ""

    def _win_cooldown_reason(self, proposal: TradeProposal) -> str:
        # Patience gate: don't let a fresh proposal re-enter the same symbol
        # immediately after it just took profit there. A win above the "big
        # win" threshold gets a longer cooldown - chasing a move that already
        # paid out is exactly the pattern that gives the profit back.
        engine = str(proposal.context.get("engine", proposal.strategy_name) or "").upper()
        latest = self.database.latest_win_for(self._canonical_symbol(proposal.symbol), engine)
        if not latest:
            return ""
        try:
            closed_at = datetime.fromisoformat(str(latest["closed_at"]))
            if closed_at.tzinfo is None:
                closed_at = closed_at.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return ""
        base_seconds = max(0, int(os.getenv("MT5_WIN_COOLDOWN_SECONDS", "300")))
        big_win_usd = max(0.0, float(os.getenv("MT5_BIG_WIN_COOLDOWN_USD", "50")))
        big_win_seconds = max(0, int(os.getenv("MT5_BIG_WIN_COOLDOWN_SECONDS", "900")))
        realized = float(latest.get("realized_pnl") or 0.0)
        is_big_win = realized >= big_win_usd
        seconds = max(base_seconds, big_win_seconds) if is_big_win else base_seconds
        if seconds <= 0:
            return ""
        remaining = seconds - int((datetime.now(timezone.utc) - closed_at).total_seconds())
        if remaining <= 0:
            return ""
        label = "BIG_WIN_COOLDOWN_ACTIVE" if is_big_win else "WIN_COOLDOWN_ACTIVE"
        return f"{label}:{max(1, remaining)}"

    def _correlated_exposure_reason(self, proposal: TradeProposal, positions) -> str:
        # The whole watchlist is scored against the same market snapshot
        # every scan cycle (see runtime.py run_once), so correlated
        # instruments (e.g. EU50/FRA40/GER40/UK100) routinely all pass
        # threshold together and open within seconds of each other - that's
        # one position's worth of real market exposure sized and risked
        # three or four times over. Block a new entry if a correlated symbol
        # already has an open position, unless explicitly disabled.
        if str(os.getenv("MT5_CORRELATION_GATE_ENABLED", "1")).strip().lower() in {"0", "false", "no"}:
            return ""
        proposal_symbol = self._canonical_symbol(proposal.symbol)
        group = next((g for g in CORRELATION_GROUPS if proposal_symbol in g), None)
        if not group:
            return ""
        for position in positions:
            other_symbol = self._canonical_symbol(position.symbol)
            if other_symbol != proposal_symbol and other_symbol in group:
                return f"CORRELATED_EXPOSURE_BLOCKED:{other_symbol}"
        return ""

    def _lock_prior_legs_to_breakeven(self, symbol: str, side: str, new_ticket: int) -> None:
        # A pyramid add is only safe to treat as "scaling the same trade" if
        # adding it doesn't leave the earlier leg(s) still exposed to their
        # full original stop. Without this, each leg is a fully independent
        # MT5 position (own SL, own management cycle) - one can hit its stop
        # and lose while another wins, netting the "pyramid" to roughly
        # breakeven instead of compounding one correct trade idea.
        if str(os.getenv("MT5_PYRAMID_LOCK_PRIOR_LEGS_BREAKEVEN", "1")).strip().lower() in {"0", "false", "no"}:
            return
        canonical = self._canonical_symbol(symbol)
        try:
            positions = self.gateway.positions()
        except Exception:
            return
        for position in positions:
            if int(getattr(position, "ticket", 0) or 0) == int(new_ticket):
                continue
            if self._canonical_symbol(position.symbol) != canonical:
                continue
            if str(position.direction).upper() != side:
                continue
            if float(getattr(position, "profit", 0.0) or 0.0) <= 0.0:
                continue
            entry = float(position.price_open)
            sl = float(position.sl)
            improves = entry > sl if side == "BUY" else entry < sl
            if not improves:
                continue
            # Exact-entry stops get whipsawed closed by ordinary price noise
            # within minutes (confirmed live 2026-07-20). Add a small buffer.
            buffer_points = max(0.0, float(os.getenv("MT5_BREAKEVEN_BUFFER_POINTS", "2")))
            try:
                buffer_price = buffer_points * float(self.gateway.symbol_info(position.symbol).point)
            except Exception:
                buffer_price = 0.0
            entry = entry + buffer_price if side == "BUY" else entry - buffer_price
            try:
                self.gateway.modify_position(int(position.ticket), position.symbol, entry, float(position.tp))
                self.database.event(
                    "PYRAMID_PRIOR_LEG_BREAKEVEN_LOCKED",
                    {"new_leg_ticket": int(new_ticket), "sl": entry},
                    symbol=position.symbol,
                )
            except Exception as exc:
                LOG.warning("breakeven lock failed for prior leg %s: %s", position.ticket, exc)

    def _pyramid_cooldown_reason(self, same_direction) -> str:
        # Profitable + under the leg cap isn't enough on its own to add
        # another leg - with trades now able to run for hours (not minutes),
        # nothing else stops a new leg firing every scan cycle (900s) for as
        # long as the position stays green, stacking up to max_pyramid_trades
        # full-size legs in a few hours. Require the most recent leg to have
        # had real time to develop first.
        if not same_direction:
            return ""
        pyramid_cooldown_seconds = max(0, int(os.getenv("MT5_PYRAMID_ADD_COOLDOWN_SECONDS", "1800")))
        if pyramid_cooldown_seconds <= 0:
            return ""
        most_recent_open = max(
            (getattr(position, "time", None) for position in same_direction if getattr(position, "time", None)),
            default=None,
        )
        if most_recent_open is None:
            return ""
        open_time = most_recent_open
        if open_time.tzinfo is None:
            open_time = open_time.replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - open_time).total_seconds()
        if elapsed >= pyramid_cooldown_seconds:
            return ""
        remaining = int(pyramid_cooldown_seconds - elapsed)
        return f"PYRAMID_COOLDOWN_ACTIVE:{remaining}"

    def _preflight(self, proposal: TradeProposal):
        if proposal.side not in SIDES:
            return False, "INVALID_SIDE", 0.0, 0.0
        if datetime.now(timezone.utc) >= proposal.expires_at:
            return False, "PROPOSAL_EXPIRED", 0.0, 0.0

        window = market_window(proposal.symbol, proposal.asset_class)
        if not window["open"]:
            return False, f"MARKET_WINDOW_CLOSED:{window['market']}:{window['reason']}", 0.0, 0.0

        cooldown_reason = self._loss_cooldown_reason(proposal)
        if cooldown_reason:
            return False, cooldown_reason, 0.0, 0.0
        win_cooldown_reason = self._win_cooldown_reason(proposal)
        if win_cooldown_reason:
            return False, win_cooldown_reason, 0.0, 0.0

        account = self.gateway.account_info()
        if not getattr(account, "terminal_connected", True):
            return False, "BROKER_DISCONNECTED", 0.0, 0.0
        if not getattr(account, "trade_allowed", True) or not getattr(account, "account_trade_allowed", True):
            return False, "TRADING_NOT_ALLOWED", 0.0, 0.0
        if self.database.count_today() >= int(self.config.max_daily_trades):
            return False, "MAX_DAILY_TRADES", 0.0, 0.0

        daily_loss_limit = float(getattr(self.config, "daily_loss_limit_usd", 0.0) or 0.0)
        if daily_loss_limit > 0 and self.database.realized_pnl_today() <= -daily_loss_limit:
            return False, "DAILY_LOSS_LIMIT", 0.0, 0.0

        positions = self.gateway.positions()
        if len(positions) >= int(self.config.max_open_trades):
            return False, "MAX_OPEN_TRADES", 0.0, 0.0
        correlation_reason = self._correlated_exposure_reason(proposal, positions)
        if correlation_reason:
            return False, correlation_reason, 0.0, 0.0
        proposal_symbol = self._canonical_symbol(proposal.symbol)

        max_per_symbol = int(getattr(self.config, "max_trades_per_symbol", 0) or 0)
        if max_per_symbol > 0 and self.database.count_today_for_symbol(proposal_symbol) >= max_per_symbol:
            return False, "MAX_TRADES_PER_SYMBOL", 0.0, 0.0

        same_direction = [
            position
            for position in positions
            if self._canonical_symbol(position.symbol) == proposal_symbol
            and str(position.direction).upper() == proposal.side
        ]
        if any(float(getattr(position, "profit", 0.0) or 0.0) <= 0.0 for position in same_direction):
            return False, "PYRAMID_LOSS_BLOCKED", 0.0, 0.0
        max_pyramid = int(getattr(self.config, "max_pyramid_trades", 0) or 0)
        if max_pyramid > 0 and len(same_direction) >= max_pyramid:
            return False, "MAX_PYRAMID_TRADES", 0.0, 0.0
        pyramid_cooldown_reason = self._pyramid_cooldown_reason(same_direction)
        if pyramid_cooldown_reason:
            return False, pyramid_cooldown_reason, 0.0, 0.0

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

        volume, margin, initial_risk = self._volume(proposal, entry, spec, account, len(same_direction))
        free = float(getattr(account, "free_margin", 0.0) or 0.0)
        if volume < float(spec.volume_min):
            if free > 0.0 and margin > free:
                return False, "INSUFFICIENT_MARGIN", 0.0, 0.0
            return False, "MIN_VOLUME", 0.0, 0.0
        # A non-positive free margin is already an operational broker stop.
        # Do not send an impossible request and wait for MT5 to reject it.
        if free <= 0.0 or margin > free:
            return False, "INSUFFICIENT_MARGIN", 0.0, 0.0
        return True, "PASS", volume, initial_risk

    def _persist_result(self, proposal: TradeProposal, result: ExecutionResult, payload=None):
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
            # Held for the whole preflight-through-submission critical section
            # (not just the idempotency claim above) so a concurrent submit()
            # can't read stale position/margin state before this one commits.
            with self._lock:
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
                comment = str(getattr(broker, "comment", "") or "").strip()
                if retcode in (10009, 10010) and deal > 0:
                    status = "FILLED"
                    reason = comment or ("MT5_DONE" if retcode == 10009 else "MT5_DONE_PARTIAL")
                elif retcode == 10008 and order > 0 and deal == 0:
                    status = "REJECTED"
                    reason = "BROKER_ORDER_PLACED_NOT_FILLED"
                elif deal == 0:
                    status = "REJECTED"
                    reason = comment or f"BROKER_NO_DEAL_RETCODE_{retcode}"
                else:
                    status = "REJECTED"
                    reason = comment or f"MT5_RETCODE_{retcode}"
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
                    reason=reason,
                    submitted_at=submitted,
                    filled_at=datetime.now(timezone.utc) if status == "FILLED" else None,
                    initial_risk=initial_risk,
                )
                # The broker outcome above is already final. A database
                # write failure from here on must never relabel a real fill
                # as REJECTED - that would permanently hide a live, broker-
                # managed position from reconciliation and learning while
                # leaving the position itself open and untouched. Retry once
                # (most failures here are a transient lock timeout), and if
                # it still fails, log loudly for manual reconciliation but
                # keep returning the true broker result, not a fabricated one.
                persist_payload = {"retcode": retcode, "order": order, "deal": deal, "position": position, "comment": comment, "reason": reason}
                try:
                    self._persist_result(proposal, result, persist_payload)
                    self.database.event(
                        "ORDER_FILLED" if status == "FILLED" else "ORDER_REJECTED",
                        persist_payload,
                        symbol=resolved,
                        proposal_id=proposal.proposal_id,
                    )
                except Exception as persist_exc:
                    try:
                        time.sleep(0.5)
                        self._persist_result(proposal, result, persist_payload)
                        self.database.event(
                            "ORDER_FILLED" if status == "FILLED" else "ORDER_REJECTED",
                            persist_payload,
                            symbol=resolved,
                            proposal_id=proposal.proposal_id,
                        )
                    except Exception as retry_exc:
                        LOG.critical(
                            "PERSIST_FAILED_AFTER_BROKER_RESULT proposal_id=%s symbol=%s status=%s "
                            "order_ticket=%s deal_ticket=%s position_ticket=%s first_error=%s retry_error=%s",
                            proposal.proposal_id, resolved, status, order, deal, position, persist_exc, retry_exc,
                        )
                if status == "FILLED" and position > 0:
                    self._lock_prior_legs_to_breakeven(resolved, proposal.side, position)
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
