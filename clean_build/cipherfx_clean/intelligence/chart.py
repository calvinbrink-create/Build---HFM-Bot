"""Serializable chart pack generated from the same snapshot used for analysis."""

from __future__ import annotations

from typing import Mapping

from ..contracts import TradeDecision
from ..market import Candle, MarketSnapshot
from ..snapshot import snapshot_state_id
from .market_features import FairValueGap, SessionRange, VWAPObservation
from .model import ChartPack, LiquidityPool, StructureState, Zone


def build_chart_pack(
    snapshot: MarketSnapshot,
    structures: dict[str, StructureState],
    zones: dict[str, tuple[Zone, ...]],
    liquidity: dict[str, tuple[LiquidityPool, ...]],
    fair_value_gaps: Mapping[str, tuple[FairValueGap, ...]] | None = None,
    vwap: VWAPObservation | None = None,
    session_ranges: Mapping[str, SessionRange] | None = None,
) -> ChartPack:
    fair_value_gaps = dict(fair_value_gaps or {})
    session_ranges = dict(session_ranges or {})
    annotations: dict[str, tuple[dict[str, object], ...]] = {}
    for timeframe, candles in snapshot.candles.items():
        points: list[dict[str, object]] = []
        current = candles[-1].close if candles else 0.0
        active_zones = tuple(
            item for item in zones.get(timeframe, ()) if item.freshness != "FAILED"
        )
        for zone in _nearest_ranges(active_zones, current, 4):
            points.append({"type": zone.kind, "low": zone.low, "high": zone.high, "state": zone.freshness})
        for gap in _nearest_ranges(
            tuple(item for item in fair_value_gaps.get(timeframe, ()) if item.state in {"UNTOUCHED", "PARTIAL"}),
            current,
            4,
        ):
            points.append(
                {
                    "type": f"FVG_{gap.direction}",
                    "low": gap.low,
                    "high": gap.high,
                    "state": gap.state,
                }
            )
        for pool in sorted(
            liquidity.get(timeframe, ()),
            key=lambda item: abs(item.price - current),
        )[:4]:
            points.append({"type": pool.kind, "price": pool.price, "distance": pool.distance})
        state = structures.get(timeframe)
        if state:
            points.append(
                {
                    "type": "STRUCTURE",
                    "price": current,
                    "direction": state.direction,
                    "bos": state.break_of_structure,
                    "choch": state.change_of_character,
                }
            )
        if vwap is not None and vwap.value is not None:
            points.append({"type": "VWAP", "price": vwap.value, "source": vwap.source})
        for name in ("ASIA", "LONDON", "NEW_YORK"):
            session = session_ranges.get(name)
            if session is None:
                continue
            for label, price in (("HIGH", session.high), ("LOW", session.low)):
                if price is not None:
                    points.append(
                        {
                            "type": f"SESSION_{name}_{label}",
                            "price": price,
                            "session_start": session.start.isoformat(),
                            "session_end": session.end.isoformat(),
                        }
                    )
        annotations[timeframe] = tuple(points)
    return ChartPack(
        snapshot.symbol,
        snapshot.observed_at.isoformat(),
        snapshot.candles,
        annotations,
        snapshot_state_id(snapshot),
        tuple(snapshot.source_ids),
    )


def with_trade_annotations(chart_pack: ChartPack, decision: TradeDecision) -> ChartPack:
    if decision.symbol != chart_pack.symbol:
        raise ValueError("trade annotation symbol does not match chart pack")
    trade_levels = tuple(
        {"type": kind, "price": value, "decision_id": decision.decision_id}
        for kind, value in (
            ("ENTRY", decision.entry),
            ("STOP_LOSS", decision.stop),
            ("TAKE_PROFIT", decision.target),
        )
        if value is not None
    )
    return ChartPack(
        symbol=chart_pack.symbol,
        observed_at=chart_pack.observed_at,
        timeframes=chart_pack.timeframes,
        annotations={
            timeframe: tuple((*chart_pack.annotations.get(timeframe, ()), *trade_levels))
            for timeframe in chart_pack.timeframes
        },
        pack_id=chart_pack.pack_id,
        source_ids=chart_pack.source_ids,
    )


def _nearest_ranges(rows: tuple[object, ...], current: float, limit: int) -> tuple[object, ...]:
    return tuple(
        sorted(
            rows,
            key=lambda item: abs(((item.low + item.high) / 2.0) - current),
        )[:limit]
    )
