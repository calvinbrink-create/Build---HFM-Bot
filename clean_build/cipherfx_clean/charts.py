"""Auditable chart pack generated from the same snapshot as analysis."""

from __future__ import annotations

from dataclasses import dataclass

from .candles import CandlePattern, detect_patterns
from .contracts import Candle, MarketSnapshot
from .structure import Structure, analyse
from .zones import Zone, discover


@dataclass(frozen=True)
class ChartAnnotation:
    kind: str
    index: int | None
    values: tuple[float, ...]
    label: str


@dataclass(frozen=True)
class ChartFrame:
    symbol: str
    timeframe: str
    candles: tuple[Candle, ...]
    patterns: tuple[CandlePattern, ...]
    structure: Structure
    zones: tuple[Zone, ...]
    annotations: tuple[ChartAnnotation, ...]


def create_chart_frame(symbol: str, timeframe: str, candles: tuple[Candle, ...]) -> ChartFrame:
    patterns = detect_patterns(candles)
    structure = analyse(candles)
    zones = discover(candles)
    annotations: list[ChartAnnotation] = []
    annotations.extend(ChartAnnotation("PATTERN", item.index, (), item.name) for item in patterns)
    annotations.extend(ChartAnnotation("ZONE", item.origin_index, (item.low, item.high), item.kind) for item in zones)
    annotations.append(ChartAnnotation("STRUCTURE", None, (), structure.direction))
    return ChartFrame(symbol, timeframe, candles, patterns, structure, zones, tuple(annotations))


def create_chart_pack(snapshot: MarketSnapshot) -> dict[str, ChartFrame]:
    return {timeframe: create_chart_frame(snapshot.symbol, timeframe, candles) for timeframe, candles in snapshot.candles.items()}
