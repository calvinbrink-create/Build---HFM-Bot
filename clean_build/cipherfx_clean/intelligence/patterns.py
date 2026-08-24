"""Geometry-first chart pattern observations.

Pattern detection is intentionally descriptive.  A detected pattern is stored
with its geometry and later receives an outcome; its name is never an approval
signal by itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Sequence

from ..market import Candle


@dataclass(frozen=True)
class GeometricPattern:
    name: str
    direction: Literal["BULLISH", "BEARISH", "NEUTRAL"]
    start_index: int
    end_index: int
    neckline: float | None
    confidence: float
    failure_condition: str
    confirmed: bool = False
    failed: bool = False


def _turns(candles: Sequence[Candle], window: int = 2) -> tuple[tuple[int, str, float], ...]:
    points: list[tuple[int, str, float]] = []
    for index in range(window, len(candles) - window):
        item = candles[index]
        left = candles[index - window:index]
        right = candles[index + 1:index + window + 1]
        if item.high >= max(row.high for row in left + right):
            points.append((index, "HIGH", item.high))
        if item.low <= min(row.low for row in left + right):
            points.append((index, "LOW", item.low))
    return tuple(points)


def detect_geometric_patterns(candles: Sequence[Candle], tolerance: float = 0.003) -> tuple[GeometricPattern, ...]:
    turns = _turns(candles)
    patterns: list[GeometricPattern] = []
    highs = tuple(item for item in turns if item[1] == "HIGH")
    lows = tuple(item for item in turns if item[1] == "LOW")
    for left, right in zip(highs, highs[1:]):
        scale = max(abs(left[2]), abs(right[2]), 1e-12)
        if abs(left[2] - right[2]) / scale <= tolerance:
            between = candles[left[0]:right[0] + 1]
            neckline = min(item.low for item in between)
            last = candles[-1].close
            patterns.append(GeometricPattern("DOUBLE_TOP", "BEARISH", left[0], right[0], neckline, 1 - abs(left[2] - right[2]) / scale, "close_above_second_peak", last < neckline, last > max(left[2], right[2])))
    for left, right in zip(lows, lows[1:]):
        scale = max(abs(left[2]), abs(right[2]), 1e-12)
        if abs(left[2] - right[2]) / scale <= tolerance:
            between = candles[left[0]:right[0] + 1]
            neckline = max(item.high for item in between)
            last = candles[-1].close
            patterns.append(GeometricPattern("DOUBLE_BOTTOM", "BULLISH", left[0], right[0], neckline, 1 - abs(left[2] - right[2]) / scale, "close_below_second_trough", last > neckline, last < min(left[2], right[2])))
    if len(highs) >= 3:
        a, b, c = highs[-3:]
        if b[2] > a[2] and b[2] > c[2] and abs(a[2] - c[2]) / max(abs(b[2]), 1e-12) < tolerance * 2:
            neckline = min(item.low for item in candles[a[0]:c[0] + 1])
            patterns.append(GeometricPattern("HEAD_SHOULDERS", "BEARISH", a[0], c[0], neckline, 0.75, "close_above_head", candles[-1].close < neckline, candles[-1].close > b[2]))
    if len(lows) >= 3:
        a, b, c = lows[-3:]
        if b[2] < a[2] and b[2] < c[2] and abs(a[2] - c[2]) / max(abs(b[2]), 1e-12) < tolerance * 2:
            neckline = max(item.high for item in candles[a[0]:c[0] + 1])
            patterns.append(GeometricPattern("INVERSE_HEAD_SHOULDERS", "BULLISH", a[0], c[0], neckline, 0.75, "close_below_head", candles[-1].close > neckline, candles[-1].close < b[2]))
    patterns.extend(_linear_patterns(candles))
    return tuple(patterns)


def _linear_patterns(candles: Sequence[Candle], window: int = 12) -> tuple[GeometricPattern, ...]:
    rows = tuple(candles[-window:])
    if len(rows) < 6:
        return ()
    high_slope, high_fit = _line(tuple(item.high for item in rows))
    low_slope, low_fit = _line(tuple(item.low for item in rows))
    close_slope, close_fit = _line(tuple(item.close for item in rows))
    scale = max(sum(item.high - item.low for item in rows) / len(rows), 1e-12)
    hs, ls, cs = high_slope / scale, low_slope / scale, close_slope / scale
    fit = min(high_fit, low_fit)
    start = len(candles) - len(rows)
    output: list[GeometricPattern] = []
    converging = hs < ls
    diverging = hs > ls
    if hs < -0.05 and ls < -0.05 and close_fit > 0.25:
        output.append(GeometricPattern("DESCENDING_CHANNEL", "NEUTRAL", start, len(candles) - 1, None, fit, "close_above_or_below_channel"))
    if hs > 0.05 and ls > 0.05 and close_fit > 0.25:
        output.append(GeometricPattern("ASCENDING_CHANNEL", "NEUTRAL", start, len(candles) - 1, None, fit, "close_above_or_below_channel"))
    if converging and hs < 0 < ls:
        output.append(GeometricPattern("SYMMETRICAL_TRIANGLE", "NEUTRAL", start, len(candles) - 1, None, fit, "range_expansion_against_break"))
    elif converging and abs(hs) < 0.08 and ls > 0.05:
        output.append(GeometricPattern("ASCENDING_TRIANGLE", "BULLISH", start, len(candles) - 1, None, fit, "close_below_rising_support"))
    elif converging and hs < -0.05 and abs(ls) < 0.08:
        output.append(GeometricPattern("DESCENDING_TRIANGLE", "BEARISH", start, len(candles) - 1, None, fit, "close_above_falling_resistance"))
    if converging and hs > 0 and ls > 0:
        output.append(GeometricPattern("RISING_WEDGE", "BEARISH", start, len(candles) - 1, None, fit, "close_above_upper_boundary"))
    if diverging and hs < 0 and ls < 0:
        output.append(GeometricPattern("FALLING_WEDGE", "BULLISH", start, len(candles) - 1, None, fit, "close_below_lower_boundary"))
    prior = tuple(candles[-window * 2:-window])
    if len(prior) >= 3:
        pole = prior[-1].close - prior[0].open
        if pole > scale * 2 and cs < 0:
            output.append(GeometricPattern("BULL_FLAG", "BULLISH", start, len(candles) - 1, None, min(fit, close_fit), "close_below_flag_low"))
        if pole < -scale * 2 and cs > 0:
            output.append(GeometricPattern("BEAR_FLAG", "BEARISH", start, len(candles) - 1, None, min(fit, close_fit), "close_above_flag_high"))
    return tuple(output)


def _line(values: tuple[float, ...]) -> tuple[float, float]:
    x_mean = (len(values) - 1) / 2
    y_mean = sum(values) / len(values)
    denominator = sum((index - x_mean) ** 2 for index in range(len(values)))
    slope = sum((index - x_mean) * (value - y_mean) for index, value in enumerate(values)) / max(denominator, 1e-12)
    fitted = tuple(y_mean + slope * (index - x_mean) for index in range(len(values)))
    total = sum((value - y_mean) ** 2 for value in values)
    residual = sum((value - estimate) ** 2 for value, estimate in zip(values, fitted))
    return slope, max(0.0, 1 - residual / total) if total else 0.0
