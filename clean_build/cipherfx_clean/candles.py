"""Complete fresh candle vocabulary for the clean Friday intelligence build."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .contracts import Candle


@dataclass(frozen=True)
class CandlePattern:
    name: str
    direction: Literal["BULLISH", "BEARISH", "NEUTRAL"]
    index: int
    context: Literal["REJECTION", "CONTROL", "INDECISION", "REVERSAL", "CONTINUATION", "COMPRESSION"]
    strength: float


def _geometry(item: Candle) -> tuple[float, float, float, float]:
    body = abs(item.close - item.open)
    span = max(item.high - item.low, 1e-12)
    upper = item.high - max(item.open, item.close)
    lower = min(item.open, item.close) - item.low
    return body, upper, lower, span


def _single(item: Candle, index: int) -> list[CandlePattern]:
    body, upper, lower, span = _geometry(item)
    floor = max(body, span * 0.05)
    out: list[CandlePattern] = []
    if body <= span * 0.1:
        out.append(CandlePattern("DOJI", "NEUTRAL", index, "INDECISION", 1 - body / span))
        if lower >= span * 0.6 and upper <= span * 0.1:
            out.append(CandlePattern("DRAGONFLY_DOJI", "BULLISH", index, "REJECTION", lower / span))
        if upper >= span * 0.6 and lower <= span * 0.1:
            out.append(CandlePattern("GRAVESTONE_DOJI", "BEARISH", index, "REJECTION", upper / span))
    direction = "BULLISH" if item.close > item.open else "BEARISH" if item.close < item.open else "NEUTRAL"
    if body >= span * 0.8:
        out.append(CandlePattern("MARUBOZU", direction, index, "CONTROL", body / span))
    if body <= span * 0.35 and upper >= floor and lower >= floor:
        out.append(CandlePattern("SPINNING_TOP", "NEUTRAL", index, "INDECISION", 1 - body / span))
    if lower >= 2 * floor and upper <= floor:
        out.append(CandlePattern("HAMMER", "BULLISH", index, "REJECTION", lower / span))
        out.append(CandlePattern("HANGING_MAN", "BEARISH", index, "REJECTION", lower / span))
    if upper >= 2 * floor and lower <= floor:
        out.append(CandlePattern("INVERTED_HAMMER", "BULLISH", index, "REJECTION", upper / span))
        out.append(CandlePattern("SHOOTING_STAR", "BEARISH", index, "REJECTION", upper / span))
    if upper <= floor and lower <= floor and body >= span * 0.5:
        out.append(CandlePattern("BELT_HOLD", direction, index, "CONTROL", body / span))
    return out


def detect_patterns(candles: tuple[Candle, ...]) -> tuple[CandlePattern, ...]:
    out: list[CandlePattern] = []
    for index, current in enumerate(candles):
        out.extend(_single(current, index))
        if index == 0:
            continue
        previous = candles[index - 1]
        current_body, _, _, current_span = _geometry(current)
        previous_body, _, _, _ = _geometry(previous)
        if previous.close < previous.open < current.close and current.open <= previous.close and current.close >= previous.open:
            out.append(CandlePattern("BULLISH_ENGULFING", "BULLISH", index, "REVERSAL", current_body / current_span))
        if previous.close > previous.open > current.close and current.open >= previous.close and current.close <= previous.open:
            out.append(CandlePattern("BEARISH_ENGULFING", "BEARISH", index, "REVERSAL", current_body / current_span))
        inside_body = current_body < previous_body * 0.7 and min(current.open, current.close) >= min(previous.open, previous.close) and max(current.open, current.close) <= max(previous.open, previous.close)
        if inside_body:
            out.append(CandlePattern("HARAMI", "BULLISH" if previous.close < previous.open else "BEARISH", index, "REVERSAL", 1 - current_body / max(previous_body, 1e-12)))
        if current.high <= previous.high and current.low >= previous.low:
            out.append(CandlePattern("INSIDE_BAR", "NEUTRAL", index, "COMPRESSION", 1.0))
        if current.high > previous.high and current.low < previous.low:
            out.append(CandlePattern("OUTSIDE_BAR", "BULLISH" if current.close > current.open else "BEARISH", index, "CONTINUATION", 1.0))
        if abs(current.high - previous.high) <= current_span * 0.1:
            out.append(CandlePattern("TWEEZER_TOP", "BEARISH", index, "REVERSAL", 1.0))
        if abs(current.low - previous.low) <= current_span * 0.1:
            out.append(CandlePattern("TWEEZER_BOTTOM", "BULLISH", index, "REVERSAL", 1.0))
    return tuple(out)
