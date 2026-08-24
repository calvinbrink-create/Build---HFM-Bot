"""Forward outcome and edge metrics over executable bid/ask paths."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from statistics import mean
from typing import Iterable, Literal, Sequence


OUTCOME_HORIZONS_SECONDS = (5, 15, 30, 60, 180, 300, 900, 3600, 14400)


@dataclass(frozen=True)
class ForwardMetric:
    horizon_seconds: int
    return_value: float
    mfe: float
    mae: float
    time_to_mfe_seconds: int
    time_to_mae_seconds: int


@dataclass(frozen=True)
class EdgeOutcomeSummary:
    sample_size: int
    tp_before_sl_probability: float
    r_multiple_mean: float
    expected_value_after_cost: float
    r_multiple_probabilities: dict[str, float]


def forward_metrics(
    direction: Literal["BUY", "SELL"],
    entry: float,
    path: Sequence[tuple[int, float, float]],
    *,
    horizons: Sequence[int] = OUTCOME_HORIZONS_SECONDS,
) -> tuple[ForwardMetric, ...]:
    if entry <= 0:
        raise ValueError("entry must be positive")
    ordered = tuple(sorted(path, key=lambda item: item[0]))
    output: list[ForwardMetric] = []
    for horizon in horizons:
        available = tuple(item for item in ordered if item[0] <= horizon)
        if not available:
            continue
        moves = tuple(
            (bid - entry) if direction == "BUY" else (entry - ask)
            for seconds, bid, ask in available
        )
        mfe = max(moves)
        mae = min(moves)
        output.append(
            ForwardMetric(
                horizon,
                moves[-1] / entry,
                mfe / entry,
                mae / entry,
                next(item[0] for item, move in zip(available, moves) if move == mfe),
                next(item[0] for item, move in zip(available, moves) if move == mae),
            )
        )
    return tuple(output)


def classify_r_multiple(
    direction: Literal["BUY", "SELL"],
    entry: float,
    path: Sequence[tuple[int, float, float]],
    stop_distance: float,
    target_distance: float,
    *,
    total_cost: float = 0.0,
) -> tuple[float, bool | None, int | None, int | None]:
    if stop_distance <= 0 or target_distance <= 0:
        raise ValueError("stop and target distances must be positive")
    target_at = None
    stop_at = None
    final_move = 0.0
    for seconds, bid, ask in sorted(path, key=lambda item: item[0]):
        move = (bid - entry) if direction == "BUY" else (entry - ask)
        final_move = move
        if target_at is None and move >= target_distance:
            target_at = seconds
        if stop_at is None and move <= -stop_distance:
            stop_at = seconds
    tp_before_sl = None
    if target_at is not None or stop_at is not None:
        tp_before_sl = stop_at is None or (target_at is not None and target_at < stop_at)
    return (final_move - total_cost) / stop_distance, tp_before_sl, target_at, stop_at


def summarize_outcomes(
    rows: Iterable[tuple[float, bool | None]],
    *,
    thresholds: Sequence[float] = (0.5, 1.0, 1.5, 2.0),
) -> EdgeOutcomeSummary:
    values = tuple(float(value) for value, _ in rows)
    tp_values = tuple(value for _, value in rows)
    if not values:
        raise ValueError("at least one outcome is required")
    if any(not isfinite(value) for value in values):
        raise ValueError("outcomes must be finite")
    tp_observed = tuple(value for value in tp_values if value is not None)
    probabilities = {
        f"{threshold:g}R": sum(value >= threshold for value in values) / len(values)
        for threshold in thresholds
    }
    return EdgeOutcomeSummary(
        len(values),
        sum(tp_observed) / len(tp_observed) if tp_observed else 0.0,
        mean(values),
        mean(values),
        probabilities,
    )
