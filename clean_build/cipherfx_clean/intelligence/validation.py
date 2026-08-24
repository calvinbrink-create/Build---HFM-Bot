"""Time-series validation primitives with purge and embargo separation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Sequence


@dataclass(frozen=True)
class Observation:
    timestamp: datetime
    outcome: float


@dataclass(frozen=True)
class ValidationFold:
    train: tuple[Observation, ...]
    test: tuple[Observation, ...]


def purged_walk_forward(
    observations: Sequence[Observation],
    *,
    train_size: int,
    test_size: int,
    embargo: timedelta,
) -> tuple[ValidationFold, ...]:
    ordered = tuple(sorted(observations, key=lambda item: item.timestamp))
    folds: list[ValidationFold] = []
    cursor = train_size
    while cursor + test_size <= len(ordered):
        train = ordered[:cursor]
        test_start = ordered[cursor]
        test = ordered[cursor:cursor + test_size]
        cutoff = test_start.timestamp - embargo
        train = tuple(item for item in train if item.timestamp < cutoff)
        folds.append(ValidationFold(train, test))
        cursor += test_size
    return tuple(folds)
