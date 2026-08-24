"""Supply and demand zones with explicit lifecycle state."""

from __future__ import annotations

from ..market import Candle
from .model import Zone
from statistics import median


def identify_zones(candles: tuple[Candle, ...]) -> tuple[Zone, ...]:
    zones: list[Zone] = []
    for index in range(1, len(candles)):
        previous = candles[index - 1]
        current = candles[index]
        previous_range = previous.high - previous.low
        current_range = current.high - current.low
        reference_rows = candles[max(0, index - 20):index]
        reference = median(tuple(row.high - row.low for row in reference_rows)) if reference_rows else previous_range
        if previous_range <= 0 or current_range <= max(reference, 1e-12) * 1.5:
            continue
        if current.close > previous.high:
            lifecycle, touches, penetration, invalidated = _lifecycle(candles[index + 1:], previous.low, previous.high, "DEMAND", index + 1)
            zones.append(Zone("DEMAND", previous.low, previous.high, index - 1, lifecycle, current_range / max(reference, 1e-12), touches, penetration, invalidated))
        elif current.close < previous.low:
            lifecycle, touches, penetration, invalidated = _lifecycle(candles[index + 1:], previous.low, previous.high, "SUPPLY", index + 1)
            zones.append(Zone("SUPPLY", previous.low, previous.high, index - 1, lifecycle, current_range / max(reference, 1e-12), touches, penetration, invalidated))
    return tuple(zones)


def _lifecycle(future: tuple[Candle, ...], low: float, high: float, kind: str, offset: int) -> tuple[str, int, float, int | None]:
    touches = 0
    penetration = 0.0
    state = "FRESH"
    width = max(high - low, 1e-12)
    for relative, candle in enumerate(future):
        if candle.low <= high and candle.high >= low:
            touches += 1
            overlap = max(0.0, min(high, candle.high) - max(low, candle.low))
            penetration = max(penetration, overlap / width)
            if kind == "DEMAND" and candle.close < low:
                return "FAILED", touches, penetration, offset + relative
            if kind == "SUPPLY" and candle.close > high:
                return "FAILED", touches, penetration, offset + relative
            if candle.low <= low and candle.high >= high:
                state = "MITIGATED"
            elif state == "FRESH":
                state = "TESTED"
    return state, touches, penetration, None
