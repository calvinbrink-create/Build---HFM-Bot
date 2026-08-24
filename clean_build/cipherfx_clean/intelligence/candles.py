"""Canonical candle pattern adapter used by the intelligence report."""

from __future__ import annotations

from ..candles import detect_patterns
from ..market import Candle
from .model import CandlePattern


def identify_patterns(candles: tuple[Candle, ...]) -> tuple[CandlePattern, ...]:
    return tuple(
        CandlePattern(item.name, item.direction, item.index, item.strength)
        for item in detect_patterns(candles)
    )
