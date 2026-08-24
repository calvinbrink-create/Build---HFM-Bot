"""Observable liquidity references and sweep evidence."""

from __future__ import annotations

from typing import Mapping

from ..market import Candle
from .market_features import SessionRange
from .model import LiquidityPool
from .structure import analyse_structure


def identify_liquidity(candles: tuple[Candle, ...], tolerance: float = 0.0005) -> tuple[LiquidityPool, ...]:
    if not candles:
        return ()
    latest = candles[-1].close
    pools: list[LiquidityPool] = []
    swings = analyse_structure(tuple(candles)).swings
    threshold = max(abs(latest) * tolerance, 1e-9)
    for kind, equal_kind, swing_kind in (("HIGH", "EQUAL_HIGH", "SWING_HIGH"), ("LOW", "EQUAL_LOW", "SWING_LOW")):
        points = tuple(item for item in swings if item.kind == kind)
        for point in points:
            pools.append(LiquidityPool(swing_kind, point.price, abs(latest - point.price), (point.index,), 1, "CONFIRMED_SWING"))
        used: set[int] = set()
        for left_index, left in enumerate(points):
            if left_index in used:
                continue
            cluster = tuple((index, point) for index, point in enumerate(points[left_index + 1:], left_index + 1) if abs(point.price - left.price) <= threshold)
            if cluster:
                members = (left,) + tuple(item[1] for item in cluster)
                used.update((left_index, *(item[0] for item in cluster)))
                price = sum(item.price for item in members) / len(members)
                pools.append(LiquidityPool(equal_kind, price, abs(latest - price), tuple(item.index for item in members), len(members), "EQUAL_SWING_CLUSTER"))
    if len(candles) >= 2:
        previous_index = len(candles) - 2
        previous = candles[previous_index]
        pools.append(LiquidityPool("PREVIOUS_HIGH", previous.high, abs(latest - previous.high), (previous_index,), 1, "PREVIOUS_COMPLETED_CANDLE"))
        pools.append(LiquidityPool("PREVIOUS_LOW", previous.low, abs(latest - previous.low), (previous_index,), 1, "PREVIOUS_COMPLETED_CANDLE"))
    return tuple(pools)


def session_liquidity(
    session_ranges: Mapping[str, SessionRange],
    latest_price: float,
) -> tuple[LiquidityPool, ...]:
    pools = []
    for session_id, observed in sorted(session_ranges.items()):
        if observed.high is not None:
            pools.append(
                LiquidityPool(
                    "SESSION_HIGH",
                    observed.high,
                    abs(latest_price - observed.high),
                    observation_count=1,
                    source="SESSION_RANGE",
                    session_id=session_id,
                )
            )
        if observed.low is not None:
            pools.append(
                LiquidityPool(
                    "SESSION_LOW",
                    observed.low,
                    abs(latest_price - observed.low),
                    observation_count=1,
                    source="SESSION_RANGE",
                    session_id=session_id,
                )
            )
    return tuple(pools)
