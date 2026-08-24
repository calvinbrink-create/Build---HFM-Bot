"""Swing hierarchy and structural shifts from completed candles."""

from __future__ import annotations

from ..market import Candle
from .model import StructureState, SwingPoint


def analyse_structure(candles: tuple[Candle, ...], lookback: int = 2) -> StructureState:
    if len(candles) < lookback * 2 + 1:
        return StructureState("UNKNOWN", (), False, False)
    swings: list[SwingPoint] = []
    for index in range(lookback, len(candles) - lookback):
        current = candles[index]
        left = candles[index - lookback:index]
        right = candles[index + 1:index + lookback + 1]
        if current.high >= max(item.high for item in left + right):
            strength = current.high - min(item.low for item in left + right)
            swings.append(SwingPoint(index, current.high, "HIGH", strength))
        if current.low <= min(item.low for item in left + right):
            strength = max(item.high for item in left + right) - current.low
            swings.append(SwingPoint(index, current.low, "LOW", strength))

    total_range = max(max(item.high for item in candles) - min(item.low for item in candles), 1e-12)
    major_lookback = max(lookback * 3, lookback + 1)
    labelled_swings: list[SwingPoint] = []
    sequence: list[str] = []
    previous_by_kind: dict[str, SwingPoint] = {}
    for point in swings:
        previous = previous_by_kind.get(point.kind)
        label = _swing_label(point, previous)
        labelled_swings.append(
            SwingPoint(
                point.index,
                point.price,
                point.kind,
                point.strength,
                label,
                _swing_hierarchy(candles, point, major_lookback),
                min(1.0, max(0.0, point.strength / total_range)),
            )
        )
        if label is not None:
            sequence.append(label)
        previous_by_kind[point.kind] = point

    high_points = [point for point in labelled_swings if point.kind == "HIGH"]
    low_points = [point for point in labelled_swings if point.kind == "LOW"]
    highs = [point.price for point in high_points]
    lows = [point.price for point in low_points]
    if len(highs) >= 2 and len(lows) >= 2:
        up = highs[-1] > highs[-2] and lows[-1] > lows[-2]
        down = highs[-1] < highs[-2] and lows[-1] < lows[-2]
    else:
        up = down = False
    direction = "UP" if up else "DOWN" if down else "RANGE" if labelled_swings else "UNKNOWN"
    latest = candles[-1].close
    previous_close = candles[-2].close
    break_up = bool(highs and previous_close <= highs[-1] < latest)
    break_down = bool(lows and previous_close >= lows[-1] > latest)
    bos_direction = "UP" if break_up else "DOWN" if break_down else None
    prior_direction = _prior_direction(highs, lows)
    choch_direction = None
    if prior_direction == "UP" and break_down:
        choch_direction = "DOWN"
    elif prior_direction == "DOWN" and break_up:
        choch_direction = "UP"
    strength = 0.0
    if len(highs) >= 2 and len(lows) >= 2:
        strength = (abs(highs[-1] - highs[-2]) + abs(lows[-1] - lows[-2])) / total_range
    close_moves = tuple(current.close - previous.close for previous, current in zip(candles, candles[1:]))
    persistence = sum(1 if move > 0 else -1 if move < 0 else 0 for move in close_moves) / len(close_moves) if close_moves else 0.0
    return StructureState(
        direction,
        tuple(labelled_swings),
        break_up or break_down,
        choch_direction is not None,
        tuple(sequence),
        bos_direction,
        choch_direction,
        strength,
        persistence,
    )


def _swing_label(current: SwingPoint, previous: SwingPoint | None) -> str | None:
    if previous is None:
        return None
    if current.kind == "HIGH":
        return "HH" if current.price > previous.price else "LH" if current.price < previous.price else "EH"
    return "HL" if current.price > previous.price else "LL" if current.price < previous.price else "EL"


def _swing_hierarchy(candles: tuple[Candle, ...], point: SwingPoint, lookback: int) -> str:
    start = max(0, point.index - lookback)
    stop = min(len(candles), point.index + lookback + 1)
    window = candles[start:stop]
    if point.kind == "HIGH":
        return "MAJOR" if point.price >= max(item.high for item in window) else "MINOR"
    return "MAJOR" if point.price <= min(item.low for item in window) else "MINOR"


def _prior_direction(highs: list[float], lows: list[float]) -> str:
    if len(highs) < 2 or len(lows) < 2:
        return "UNKNOWN"
    if highs[-1] > highs[-2] and lows[-1] > lows[-2]:
        return "UP"
    if highs[-1] < highs[-2] and lows[-1] < lows[-2]:
        return "DOWN"
    return "RANGE"
