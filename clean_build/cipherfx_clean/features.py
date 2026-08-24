"""Market-state fingerprints built from observations, not strategy rules."""

from __future__ import annotations

from dataclasses import dataclass
from math import copysign

from .market import Candle, RawTick


@dataclass(frozen=True)
class MarketFingerprint:
    symbol: str
    tick_count: int
    spread_mean: float
    spread_max: float
    return_change: float
    range_change: float
    directional_persistence: float
    tick_rate_per_second: float
    candle_direction: int
    candle_range: float


def build_fingerprint(ticks: tuple[RawTick, ...], candles: tuple[Candle, ...]) -> MarketFingerprint:
    """Describe the observed state without classifying it as a trade setup."""

    if not ticks:
        raise ValueError("fingerprint requires observations")
    ordered = sorted(ticks, key=lambda item: item.timestamp)
    spreads = [item.spread for item in ordered]
    mids = [(item.bid + item.ask) / 2 for item in ordered]
    move_signs = [
        1 if current > previous else -1 if current < previous else 0
        for previous, current in zip(mids, mids[1:])
    ]
    persistence = sum(move_signs) / len(move_signs) if move_signs else 0.0
    duration = (ordered[-1].timestamp - ordered[0].timestamp).total_seconds()
    candle = candles[-1] if candles else None
    candle_direction = 0 if candle is None else (1 if candle.close > candle.open else -1 if candle.close < candle.open else 0)
    return MarketFingerprint(
        symbol=ordered[0].symbol,
        tick_count=len(ordered),
        spread_mean=sum(spreads) / len(spreads),
        spread_max=max(spreads),
        return_change=mids[-1] - mids[0],
        range_change=max(mids) - min(mids),
        directional_persistence=persistence,
        tick_rate_per_second=len(ordered) / duration if duration > 0 else 0.0,
        candle_direction=candle_direction,
        candle_range=0.0 if candle is None else candle.high - candle.low,
    )
