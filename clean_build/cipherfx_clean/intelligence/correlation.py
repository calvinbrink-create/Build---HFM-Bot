"""Monitor-only cross-asset relationship calculations."""

from __future__ import annotations

from math import sqrt


def pearson(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    if len(left) != len(right) or len(left) < 2:
        raise ValueError("equal series with at least two observations required")
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    numerator = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right))
    left_norm = sqrt(sum((a - left_mean) ** 2 for a in left))
    right_norm = sqrt(sum((b - right_mean) ** 2 for b in right))
    return numerator / (left_norm * right_norm) if left_norm and right_norm else 0.0


def returns(values: tuple[float, ...]) -> tuple[float, ...]:
    return tuple((current - previous) / max(abs(previous), 1e-12) for previous, current in zip(values, values[1:]))


def rolling_pearson(left: tuple[float, ...], right: tuple[float, ...], window: int) -> tuple[float, ...]:
    if window < 2 or len(left) != len(right):
        raise ValueError("equal series and window >= 2 required")
    return tuple(pearson(left[index - window:index], right[index - window:index]) for index in range(window, len(left) + 1))
