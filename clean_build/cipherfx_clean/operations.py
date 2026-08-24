"""Deterministic broker-operational validation, separate from intelligence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class BrokerContract:
    symbol: str
    tick_size: float
    tick_value: float
    contract_size: float
    min_volume: float
    max_volume: float
    volume_step: float
    margin_per_unit: float


@dataclass(frozen=True)
class OperationalRequest:
    symbol: str
    volume: float
    spread: float
    spread_limit: float | None
    required_margin: float
    free_margin: float
    duplicate: bool
    market_open: bool
    tradable: bool


@dataclass(frozen=True)
class OperationalResult:
    status: Literal["PASS", "EXECUTION_BLOCKED"]
    reasons: tuple[str, ...]


def validate_operational(request: OperationalRequest, contract: BrokerContract) -> OperationalResult:
    reasons: list[str] = []
    if not request.symbol or request.symbol != contract.symbol:
        reasons.append("SYMBOL_MISMATCH")
    if not request.tradable:
        reasons.append("SYMBOL_NOT_TRADABLE")
    if not request.market_open:
        reasons.append("MARKET_CLOSED")
    if request.volume < contract.min_volume or request.volume > contract.max_volume:
        reasons.append("VOLUME_OUT_OF_RANGE")
    if contract.volume_step > 0:
        steps = round(request.volume / contract.volume_step)
        if abs(request.volume - steps * contract.volume_step) > 1e-9:
            reasons.append("VOLUME_STEP_INVALID")
    if request.spread_limit is not None and request.spread > request.spread_limit:
        reasons.append("SPREAD_OUT_OF_RANGE")
    if request.required_margin > request.free_margin:
        reasons.append("INSUFFICIENT_MARGIN")
    if request.duplicate:
        reasons.append("DUPLICATE_DECISION")
    return OperationalResult("EXECUTION_BLOCKED" if reasons else "PASS", tuple(reasons))
