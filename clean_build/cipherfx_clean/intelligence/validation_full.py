"""Chronological purged walk-forward validation with untouched holdout data."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from math import log, sqrt
from random import Random
from typing import Iterable, Sequence


@dataclass(frozen=True)
class TimedObservation:
    timestamp: datetime
    value: float
    instrument: str
    session: str
    regime: str
    label_end: datetime | None = None
    timeframe: str = ""
    direction: str = ""
    volatility: str = ""
    spread_bucket: str = ""
    source_id: str = ""
    cost: float = 0.0

    @property
    def net_value(self) -> float:
        return self.value - self.cost


@dataclass(frozen=True)
class PurgedFold:
    number: int
    train: tuple[TimedObservation, ...]
    test: tuple[TimedObservation, ...]
    embargo_start: datetime | None
    embargo_end: datetime | None


@dataclass(frozen=True)
class MetricReport:
    sample_size: int
    mean_value: float
    win_rate: float
    lower_bound: float
    upper_bound: float
    drawdown: float
    standard_deviation: float = 0.0
    mean_lower: float = 0.0
    mean_upper: float = 0.0
    profit_factor: float | None = None
    sharpe: float | None = None


@dataclass(frozen=True)
class ValidationEvidence:
    edge_id: str
    folds: tuple[PurgedFold, ...]
    in_sample: MetricReport
    out_of_sample: MetricReport
    slices: dict[str, MetricReport]
    monte_carlo_lower: float
    multiple_test_count: int
    holdout: MetricReport = MetricReport(0, 0.0, 0.0, 0.0, 0.0, 0.0)
    multiplicity_adjusted_lower: float = 0.0
    status: str = "INSUFFICIENT_EVIDENCE"
    reasons: tuple[str, ...] = ()


def purged_folds(
    observations: Sequence[TimedObservation],
    train_size: int,
    test_size: int,
    embargo: timedelta,
) -> tuple[PurgedFold, ...]:
    rows = tuple(sorted(observations, key=lambda item: (item.timestamp, item.source_id)))
    if train_size <= 0 or test_size <= 0:
        raise ValueError("train_size and test_size must be positive")
    output: list[PurgedFold] = []
    cursor = train_size
    number = 1
    while cursor + test_size <= len(rows):
        raw_test = rows[cursor:cursor + test_size]
        test = _non_overlapping(raw_test)
        if not test:
            cursor += test_size
            continue
        start = test[0].timestamp
        end = max((item.label_end or item.timestamp) for item in test)
        train = tuple(
            item
            for item in rows[:cursor]
            if (item.label_end or item.timestamp) < start - embargo
        )
        output.append(PurgedFold(number, train, test, start - embargo, end + embargo))
        cursor += test_size
        number += 1
    return tuple(output)


def _non_overlapping(rows: Sequence[TimedObservation]) -> tuple[TimedObservation, ...]:
    output: list[TimedObservation] = []
    occupied_until: datetime | None = None
    for item in rows:
        if occupied_until is not None and item.timestamp <= occupied_until:
            continue
        output.append(item)
        occupied_until = item.label_end or item.timestamp
    return tuple(output)


def _metric(values: Iterable[float]) -> MetricReport:
    rows = tuple(values)
    if not rows:
        return MetricReport(0, 0.0, 0.0, 0.0, 0.0, 0.0)
    wins = sum(value > 0 for value in rows)
    rate = wins / len(rows)
    z = 1.959963984540054
    denominator = 1 + z * z / len(rows)
    center = (rate + z * z / (2 * len(rows))) / denominator
    margin = z * sqrt(rate * (1 - rate) / len(rows) + z * z / (4 * len(rows) ** 2)) / denominator
    equity = 0.0
    peak = 0.0
    drawdown = 0.0
    for value in rows:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    average = sum(rows) / len(rows)
    deviation = _population_deviation(rows, average)
    standard_error = deviation / sqrt(len(rows)) if rows else 0.0
    gains = sum(value for value in rows if value > 0)
    losses = abs(sum(value for value in rows if value < 0))
    return MetricReport(
        len(rows),
        average,
        rate,
        max(0.0, center - margin),
        min(1.0, center + margin),
        drawdown,
        deviation,
        average - z * standard_error,
        average + z * standard_error,
        gains / losses if losses else None,
        average / deviation * sqrt(len(rows)) if deviation else None,
    )


def monte_carlo_lower(
    values: Sequence[float],
    iterations: int = 1000,
    seed: int = 7,
    percentile: float = 0.05,
) -> float:
    if not values:
        return 0.0
    rng = Random(seed)
    sample_size = len(values)
    means = sorted(
        sum(rng.choice(values) for _ in values) / sample_size
        for _ in range(iterations)
    )
    index = min(len(means) - 1, max(0, int(iterations * percentile)))
    return means[index]


def validate_candidate(
    edge_id: str,
    observations: Sequence[TimedObservation],
    *,
    train_size: int,
    test_size: int,
    embargo: timedelta,
    multiple_test_count: int,
    holdout_size: int | None = None,
    minimum_oos_samples: int = 1,
    minimum_holdout_samples: int = 1,
) -> ValidationEvidence:
    rows = tuple(sorted(observations, key=lambda item: (item.timestamp, item.source_id)))
    if holdout_size is None:
        holdout_size = min(test_size, max(0, len(rows) - train_size - test_size))
    holdout_rows = _non_overlapping(rows[-holdout_size:]) if holdout_size else ()
    discovery = rows[:-holdout_size] if holdout_size else rows
    folds = purged_folds(discovery, train_size, test_size, embargo)
    latest_train = folds[-1].train if folds else ()
    test_rows = _unique_observations(item for fold in folds for item in fold.test)
    in_sample = _metric(item.net_value for item in latest_train)
    out_of_sample = _metric(item.net_value for item in test_rows)
    holdout = _metric(item.net_value for item in holdout_rows)
    evaluated = test_rows + holdout_rows
    multiplicity_penalty = (
        out_of_sample.standard_deviation
        * sqrt(2 * log(max(multiple_test_count, 1)) / out_of_sample.sample_size)
        if out_of_sample.sample_size
        else 0.0
    )
    adjusted = out_of_sample.mean_lower - multiplicity_penalty
    reasons = []
    if not folds:
        reasons.append("NO_WALK_FORWARD_FOLDS")
    if out_of_sample.sample_size < minimum_oos_samples:
        reasons.append("INSUFFICIENT_OOS_SAMPLE")
    if holdout.sample_size < minimum_holdout_samples:
        reasons.append("INSUFFICIENT_UNTOUCHED_HOLDOUT")
    if adjusted <= 0:
        reasons.append("MULTIPLICITY_ADJUSTED_EDGE_NOT_POSITIVE")
    if holdout.sample_size and holdout.mean_lower <= 0:
        reasons.append("HOLDOUT_EDGE_NOT_POSITIVE")
    status = "PASS" if not reasons else "INSUFFICIENT_EVIDENCE" if any(item.startswith("INSUFFICIENT") or item.startswith("NO_") for item in reasons) else "NO_EDGE"
    slices = {} if status == "INSUFFICIENT_EVIDENCE" else _slice_metrics(evaluated)
    simulation_lower = (
        monte_carlo_lower(tuple(item.net_value for item in test_rows))
        if not reasons
        else 0.0
    )
    return ValidationEvidence(
        edge_id,
        folds,
        in_sample,
        out_of_sample,
        slices,
        simulation_lower,
        multiple_test_count,
        holdout,
        adjusted,
        status,
        tuple(reasons),
    )


def _unique_observations(rows: Iterable[TimedObservation]) -> tuple[TimedObservation, ...]:
    output = {}
    for item in rows:
        key = item.source_id or f"{item.instrument}|{item.timestamp.isoformat()}|{item.label_end}"
        output[key] = item
    return tuple(sorted(output.values(), key=lambda item: (item.timestamp, item.source_id)))


def _slice_metrics(rows: Sequence[TimedObservation]) -> dict[str, MetricReport]:
    grouped: dict[str, list[float]] = {}
    for item in rows:
        dimensions = {
            "instrument": item.instrument,
            "session": item.session,
            "regime": item.regime,
            "timeframe": item.timeframe,
            "direction": item.direction,
            "volatility": item.volatility,
            "spread": item.spread_bucket,
        }
        for name, value in dimensions.items():
            if value:
                grouped.setdefault(f"{name}={value}", []).append(item.net_value)
        grouped.setdefault(
            f"instrument={item.instrument}|session={item.session}|regime={item.regime}", []
        ).append(item.net_value)
    return {key: _metric(values) for key, values in grouped.items()}


def feature_ablation(baseline: Sequence[float], variants: dict[str, Sequence[float]]) -> dict[str, float]:
    base = _metric(baseline).mean_value
    return {name: _metric(values).mean_value - base for name, values in variants.items()}


def _population_deviation(values: Sequence[float], average: float) -> float:
    if len(values) <= 1:
        return 0.0
    return sqrt(sum((value - average) ** 2 for value in values) / len(values))
