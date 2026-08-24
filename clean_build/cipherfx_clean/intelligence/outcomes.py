"""Forward outcomes for trades, rejected setups and ordinary market states."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Sequence


@dataclass(frozen=True)
class ForwardHorizon:
    seconds: int
    bid_return: float | None
    ask_return: float | None
    max_favourable: float | None
    max_adverse: float | None


@dataclass(frozen=True)
class StateOutcome:
    state_id: str
    outcome_kind: Literal["WINNER", "LOSER", "BREAKEVEN", "MISSED", "REJECTED", "EXPIRED", "EXECUTION_FAILED", "FAILED_PATTERN", "NO_TRADE", "TRADED"]
    direction: Literal["BUY", "SELL", "NONE"]
    horizons: tuple[ForwardHorizon, ...]
    tp_before_sl: bool | None
    time_to_mfe_seconds: int | None
    time_to_mae_seconds: int | None
    rejection_reason: str | None = None
    mfe_value: float | None = None
    mae_value: float | None = None
    r_multiple: float | None = None
    holding_time_seconds: int | None = None
    total_cost: float = 0.0
    target_at_seconds: int | None = None
    stop_at_seconds: int | None = None


def future_path_outcome(
    state_id: str,
    direction: Literal["BUY", "SELL"],
    entry: float,
    future_bid_ask: Sequence[tuple[int, float, float]],
    stop_distance: float,
    target_distance: float,
    *,
    outcome_kind: str = "TRADED",
    total_cost: float = 0.0,
) -> StateOutcome:
    if entry <= 0 or stop_distance <= 0 or target_distance <= 0:
        raise ValueError("entry and distances must be positive")
    horizons: list[ForwardHorizon] = []
    mfe = 0.0
    mae = 0.0
    path: list[tuple[int, float]] = []
    target_at = None
    stop_at = None
    for seconds, bid, ask in future_bid_ask:
        move = (bid - entry) if direction == "BUY" else (entry - ask)
        mfe = max(mfe, move)
        mae = min(mae, move)
        path.append((seconds, move))
        if target_at is None and move >= target_distance:
            target_at = seconds
        if stop_at is None and move <= -stop_distance:
            stop_at = seconds
        horizons.append(ForwardHorizon(seconds, move / entry, move / entry, mfe / entry, mae / entry))
    tp_before_sl = None
    if target_at is not None or stop_at is not None:
        tp_before_sl = stop_at is None or (target_at is not None and target_at < stop_at)
    mfe_at = next((seconds for seconds, move in path if move == mfe), None)
    mae_at = next((seconds for seconds, move in path if move == mae), None)
    final_move = path[-1][1] if path else 0.0
    r_multiple = (final_move - total_cost) / stop_distance
    return StateOutcome(
        state_id, outcome_kind, direction, tuple(horizons), tp_before_sl, mfe_at, mae_at, None,
        mfe, mae, r_multiple, path[-1][0] if path else None, total_cost, target_at, stop_at,
    )
