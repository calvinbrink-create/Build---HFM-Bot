"""Executable-quote shadow positions that can never submit broker orders."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import sha256
import json
from typing import Literal

from .contracts import RawTick, TradeDecision
from .validation import validate_decision


ShadowStatus = Literal["WAITING", "OPEN", "TARGET", "STOP", "EXPIRED"]


@dataclass(frozen=True)
class ShadowPosition:
    shadow_id: str
    decision_id: str
    edge_id: str
    edge_version: int
    state_id: str
    symbol: str
    side: Literal["BUY", "SELL"]
    created_at: datetime
    expires_at: datetime
    entry_area: tuple[float, float] | None
    planned_entry: float
    stop: float
    target: float
    status: ShadowStatus = "WAITING"
    entry_at: datetime | None = None
    entry_price: float | None = None
    closed_at: datetime | None = None
    exit_price: float | None = None
    mfe_price: float = 0.0
    mae_price: float = 0.0
    observed_quotes: int = 0
    extra_cost_price: float = 0.0


@dataclass(frozen=True)
class ShadowOutcome:
    shadow_id: str
    decision_id: str
    edge_id: str
    status: Literal["TARGET", "STOP", "EXPIRED"]
    entry_at: datetime | None
    closed_at: datetime
    entry_price: float | None
    exit_price: float | None
    r_multiple: float | None
    mfe_r: float | None
    mae_r: float | None
    holding_seconds: float | None
    observed_quotes: int
    source_tick_id: str


def create_shadow_position(
    decision: TradeDecision,
    *,
    state_id: str,
    expires_at: datetime,
    extra_cost_return: float = 0.0,
) -> ShadowPosition:
    validation = validate_decision(decision)
    if not validation.valid or decision.action == "NO_TRADE":
        raise ValueError(f"shadow requires executable valid decision: {validation.reason}")
    if expires_at <= decision.created_at:
        raise ValueError("shadow expiry must follow decision creation")
    if extra_cost_return < 0:
        raise ValueError("extra shadow cost cannot be negative")
    payload = (
        decision.decision_id, decision.edge_id, decision.edge_version,
        state_id, expires_at.isoformat(), extra_cost_return,
    )
    return ShadowPosition(
        _digest(payload),
        decision.decision_id,
        decision.edge_id,
        int(decision.edge_version),
        state_id,
        decision.symbol,
        decision.action,
        decision.created_at,
        expires_at,
        decision.entry_area,
        float(decision.entry),
        float(decision.stop),
        float(decision.target),
        extra_cost_price=float(decision.entry) * extra_cost_return,
    )


def advance_shadow(
    position: ShadowPosition,
    tick: RawTick,
) -> tuple[ShadowPosition, ShadowOutcome | None]:
    if tick.symbol != position.symbol:
        raise ValueError("shadow tick symbol mismatch")
    if tick.timestamp < position.created_at:
        raise ValueError("shadow tick predates decision")
    if position.status in ("TARGET", "STOP", "EXPIRED"):
        return position, None
    source_tick_id = _digest((tick.symbol, tick.timestamp.isoformat(), tick.bid, tick.ask))
    quotes = position.observed_quotes + 1
    if position.status == "WAITING":
        if tick.timestamp >= position.expires_at:
            closed = replace(position, status="EXPIRED", closed_at=tick.timestamp, observed_quotes=quotes)
            return closed, _outcome(closed, source_tick_id)
        executable = tick.ask if position.side == "BUY" else tick.bid
        if position.entry_area is not None and not position.entry_area[0] <= executable <= position.entry_area[1]:
            return replace(position, observed_quotes=quotes), None
        position = replace(
            position,
            status="OPEN",
            entry_at=tick.timestamp,
            entry_price=executable,
            observed_quotes=quotes,
        )
    else:
        position = replace(position, observed_quotes=quotes)

    executable_exit = tick.bid if position.side == "BUY" else tick.ask
    raw_move = executable_exit - position.entry_price if position.side == "BUY" else position.entry_price - executable_exit
    updated = replace(
        position,
        mfe_price=max(position.mfe_price, raw_move),
        mae_price=min(position.mae_price, raw_move),
    )
    target_hit = executable_exit >= position.target if position.side == "BUY" else executable_exit <= position.target
    stop_hit = executable_exit <= position.stop if position.side == "BUY" else executable_exit >= position.stop
    status: ShadowStatus | None = "TARGET" if target_hit else "STOP" if stop_hit else None
    if status is None and tick.timestamp >= position.expires_at:
        status = "EXPIRED"
    if status is None:
        return updated, None
    closed = replace(updated, status=status, closed_at=tick.timestamp, exit_price=executable_exit)
    return closed, _outcome(closed, source_tick_id)


def _outcome(position: ShadowPosition, source_tick_id: str) -> ShadowOutcome:
    if position.closed_at is None:
        raise ValueError("closed shadow position requires close time")
    if position.entry_price is None or position.exit_price is None:
        return ShadowOutcome(
            position.shadow_id, position.decision_id, position.edge_id,
            position.status, position.entry_at, position.closed_at,
            position.entry_price, position.exit_price, None, None, None, None,
            position.observed_quotes, source_tick_id,
        )
    risk = abs(position.entry_price - position.stop)
    raw_move = position.exit_price - position.entry_price if position.side == "BUY" else position.entry_price - position.exit_price
    net_move = raw_move - position.extra_cost_price
    return ShadowOutcome(
        position.shadow_id,
        position.decision_id,
        position.edge_id,
        position.status,
        position.entry_at,
        position.closed_at,
        position.entry_price,
        position.exit_price,
        net_move / risk,
        (position.mfe_price - position.extra_cost_price) / risk,
        (position.mae_price - position.extra_cost_price) / risk,
        (position.closed_at - position.entry_at).total_seconds() if position.entry_at else None,
        position.observed_quotes,
        source_tick_id,
    )


def _digest(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()
