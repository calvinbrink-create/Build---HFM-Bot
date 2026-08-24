"""Open-position management boundary; it cannot create new decisions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol


@dataclass(frozen=True)
class Position:
    position_id: str
    decision_id: str
    symbol: str
    side: Literal["BUY", "SELL"]
    volume: float
    entry: float
    stop: float | None
    target: float | None


@dataclass(frozen=True)
class ManagementAction:
    position_id: str
    action: Literal["HOLD", "MODIFY_STOP", "MODIFY_TARGET", "PARTIAL_CLOSE", "CLOSE"]
    reason: str
    new_stop: float | None = None
    new_target: float | None = None
    volume_fraction: float | None = None


@dataclass(frozen=True)
class ManagementPolicy:
    break_even_r: float | None = None
    trail_start_r: float | None = None
    trail_distance: float | None = None
    partial_levels: tuple[float, ...] = ()
    profit_lock_levels: tuple[tuple[float, float], ...] = ()
    max_adverse_r: float | None = None
    max_holding_seconds: int | None = None


@dataclass(frozen=True)
class PositionMark:
    price: float
    r_multiple: float
    drawdown_r: float
    favourable_r: float
    adverse_r: float


@dataclass(frozen=True)
class PositionTelemetry:
    position_id: str
    observed_at: datetime
    price: float
    current_r: float
    drawdown_r: float
    mfe_r: float
    mae_r: float
    holding_seconds: int
    spread: float
    volatility: float | None
    session: str
    liquidity_event: str | None
    momentum_state: str | None
    distance_to_target: float | None
    distance_to_stop: float | None
    risk_exposure: float
    rank: Literal["EXCELLENT", "STRONG", "HEALTHY", "NEUTRAL", "WEAK", "DANGER", "CRITICAL"]


class PositionMonitor:
    """Track position state only; it has no authority to create an entry."""

    def __init__(self) -> None:
        self._opened: dict[str, datetime] = {}
        self._mfe: dict[str, float] = {}
        self._mae: dict[str, float] = {}
        self._peak: dict[str, float] = {}

    def record(
        self,
        position: Position,
        *,
        observed_at: datetime,
        price: float,
        spread: float,
        risk_exposure: float,
        volatility: float | None = None,
        session: str = "UNKNOWN",
        liquidity_event: str | None = None,
        momentum_state: str | None = None,
    ) -> PositionTelemetry:
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("position observation must be timezone-aware")
        risk_distance = abs(position.entry - position.stop) if position.stop is not None else 0.0
        move = price - position.entry if position.side == "BUY" else position.entry - price
        current_r = move / risk_distance if risk_distance else 0.0
        self._opened.setdefault(position.position_id, observed_at)
        self._mfe[position.position_id] = max(self._mfe.get(position.position_id, current_r), current_r)
        self._mae[position.position_id] = min(self._mae.get(position.position_id, current_r), current_r)
        self._peak[position.position_id] = max(self._peak.get(position.position_id, current_r), current_r)
        drawdown = self._peak[position.position_id] - current_r
        return PositionTelemetry(
            position.position_id,
            observed_at,
            price,
            current_r,
            drawdown,
            self._mfe[position.position_id],
            self._mae[position.position_id],
            int((observed_at - self._opened[position.position_id]).total_seconds()),
            spread,
            volatility,
            session,
            liquidity_event,
            momentum_state,
            abs(position.target - price) if position.target is not None else None,
            abs(position.stop - price) if position.stop is not None else None,
            risk_exposure,
            rank_position(current_r, drawdown),
        )


def rank_position(current_r: float, drawdown_r: float) -> str:
    if current_r >= 2 and drawdown_r <= .5:
        return "EXCELLENT"
    if current_r >= 1:
        return "STRONG"
    if current_r >= .25:
        return "HEALTHY"
    if current_r >= -.25:
        return "NEUTRAL"
    if current_r >= -.6:
        return "WEAK"
    if current_r > -1:
        return "DANGER"
    return "CRITICAL"


class PositionBroker(Protocol):
    def modify_stop(self, position: Position, stop: float) -> None:
        ...

    def close(self, position: Position) -> None:
        ...


class PositionManager:
    def __init__(self, broker: PositionBroker):
        self._broker = broker

    def observe(self, position: Position, market_price: float) -> ManagementAction:
        if position.side == "BUY" and position.target is not None and market_price >= position.target:
            return ManagementAction(position.position_id, "CLOSE", "TARGET_REACHED")
        if position.side == "SELL" and position.target is not None and market_price <= position.target:
            return ManagementAction(position.position_id, "CLOSE", "TARGET_REACHED")
        return ManagementAction(position.position_id, "HOLD", "NO_MANAGEMENT_EVENT")

    def evaluate(
        self,
        position: Position,
        mark: PositionMark,
        policy: ManagementPolicy,
        *,
        holding_seconds: int = 0,
        completed_reasons: tuple[str, ...] = (),
    ) -> ManagementAction:
        """Return a deterministic management instruction for an open position.

        This method only consumes position marks and an explicit management
        policy.  It never analyses a new setup or creates another position.
        """
        if policy.max_adverse_r is not None and mark.r_multiple <= -abs(policy.max_adverse_r):
            return ManagementAction(position.position_id, "CLOSE", "MAX_ADVERSE_R_POLICY")
        if policy.max_holding_seconds is not None and holding_seconds >= policy.max_holding_seconds:
            return ManagementAction(position.position_id, "CLOSE", "MAX_HOLDING_TIME_POLICY")
        for level in sorted(policy.partial_levels):
            reason = f"PARTIAL_CLOSE_{level:g}R"
            if mark.favourable_r >= level and reason not in completed_reasons:
                return ManagementAction(position.position_id, "PARTIAL_CLOSE", reason, volume_fraction=.5)
        risk_distance = abs(position.entry - position.stop) if position.stop is not None else None
        if risk_distance:
            eligible = tuple(item for item in policy.profit_lock_levels if mark.favourable_r >= item[0])
            if eligible:
                _, locked_r = max(eligible)
                candidate = position.entry + risk_distance * locked_r if position.side == "BUY" else position.entry - risk_distance * locked_r
                if position.stop is None or (position.side == "BUY" and candidate > position.stop) or (position.side == "SELL" and candidate < position.stop):
                    return ManagementAction(position.position_id, "MODIFY_STOP", f"PROFIT_LOCK_{locked_r:g}R", candidate)
        if policy.trail_start_r is not None and mark.favourable_r >= policy.trail_start_r and policy.trail_distance is not None:
            candidate = mark.price - policy.trail_distance if position.side == "BUY" else mark.price + policy.trail_distance
            if position.stop is None or (position.side == "BUY" and candidate > position.stop) or (position.side == "SELL" and candidate < position.stop):
                return ManagementAction(position.position_id, "MODIFY_STOP", "TRAIL_POLICY", candidate)
        if policy.break_even_r is not None and mark.favourable_r >= policy.break_even_r and position.stop is not None:
            if (position.side == "BUY" and position.stop < position.entry) or (position.side == "SELL" and position.stop > position.entry):
                return ManagementAction(position.position_id, "MODIFY_STOP", "BREAK_EVEN_POLICY", position.entry)
        return ManagementAction(position.position_id, "HOLD", "NO_MANAGEMENT_EVENT")

    def apply(self, position: Position, action: ManagementAction) -> None:
        if action.action == "MODIFY_STOP" and action.new_stop is not None:
            self._broker.modify_stop(position, action.new_stop)
        elif action.action == "CLOSE":
            self._broker.close(position)
