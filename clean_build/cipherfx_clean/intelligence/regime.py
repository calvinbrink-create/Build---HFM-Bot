"""Descriptive market regime classification from completed price states."""

from __future__ import annotations

from ..market import Candle
from .model import RegimeState


def classify_regime(candles: tuple[Candle, ...]) -> RegimeState:
    if len(candles) < 3:
        return RegimeState("UNKNOWN", 0.0, 0.0)
    ranges = [max(item.high - item.low, 0.0) for item in candles]
    volatility = sum(ranges[-3:]) / max(sum(ranges), 1e-9)
    moves = [1 if item.close > item.open else -1 if item.close < item.open else 0 for item in candles[-3:]]
    persistence = abs(sum(moves)) / len(moves)
    if volatility < 0.15:
        label = "COMPRESSION"
    elif persistence >= 2 / 3 and moves[-1] > 0:
        label = "TREND_UP"
    elif persistence >= 2 / 3 and moves[-1] < 0:
        label = "TREND_DOWN"
    elif volatility > 0.5:
        label = "EXPANSION"
    else:
        label = "RANGE"
    return RegimeState(label, volatility, persistence)
