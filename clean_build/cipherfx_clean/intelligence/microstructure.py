"""Combined, descriptive tick and quote microstructure observations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from statistics import mean, median, pstdev
from typing import Sequence

from ..market import RawTick, exact_tick_change, exact_tick_mid
from .market_features import (
    tick_acceleration_observation,
    tick_imbalance_observation,
    tick_velocity_observation,
)


@dataclass(frozen=True)
class Microstructure:
    """One transparent feature vector for a bounded quote-event window.

    The fields are measurements, not a score or a trading decision.  Keeping
    the components separate lets later research prove which observations add
    predictive value without making this module a hidden execution gate.
    """

    symbol: str
    tick_count: int
    event_count: int
    window_start: datetime | None
    window_end: datetime | None
    duration_seconds: float
    latest_spread: float
    mean_spread: float
    median_spread: float
    minimum_spread: float
    max_spread: float
    spread_stddev: float
    mean_relative_spread_bps: float
    quote_arrival_rate: float
    mean_interarrival_seconds: float
    median_interarrival_seconds: float
    zero_interval_count: int
    net_quote_movement: float
    gross_quote_movement: float
    mean_absolute_quote_change: float
    micro_volatility: float
    relative_micro_volatility_bps: float
    directional_imbalance: float
    distance_imbalance: float
    velocity: float
    path_velocity: float
    acceleration: float
    mean_absolute_acceleration: float
    maximum_absolute_acceleration: float
    acceleration_zscore: float
    rapid_acceleration: bool
    status: str
    source: str = "BOUNDED_BID_ASK_QUOTE_EVENT_WINDOW"
    component_sources: tuple[str, ...] = (
        "EXACT_BID_ASK_SPREAD",
        "EXACT_MIDPOINT_DIRECTION_EVENTS",
        "EXACT_MIDPOINT_CHANGE_PER_ELAPSED_SECOND",
        "CHANGE_IN_EVENT_VELOCITY_PER_ELAPSED_SECOND",
    )


def _exact_spread(tick: RawTick) -> float:
    return float(Decimal(str(tick.ask)) - Decimal(str(tick.bid)))


def _empty_microstructure() -> Microstructure:
    return Microstructure(
        symbol="",
        tick_count=0,
        event_count=0,
        window_start=None,
        window_end=None,
        duration_seconds=0.0,
        latest_spread=0.0,
        mean_spread=0.0,
        median_spread=0.0,
        minimum_spread=0.0,
        max_spread=0.0,
        spread_stddev=0.0,
        mean_relative_spread_bps=0.0,
        quote_arrival_rate=0.0,
        mean_interarrival_seconds=0.0,
        median_interarrival_seconds=0.0,
        zero_interval_count=0,
        net_quote_movement=0.0,
        gross_quote_movement=0.0,
        mean_absolute_quote_change=0.0,
        micro_volatility=0.0,
        relative_micro_volatility_bps=0.0,
        directional_imbalance=0.0,
        distance_imbalance=0.0,
        velocity=0.0,
        path_velocity=0.0,
        acceleration=0.0,
        mean_absolute_acceleration=0.0,
        maximum_absolute_acceleration=0.0,
        acceleration_zscore=0.0,
        rapid_acceleration=False,
        status="INSUFFICIENT_TICKS",
    )


def measure_microstructure(ticks: Sequence[RawTick]) -> Microstructure:
    ordered = tuple(sorted(ticks, key=lambda item: (item.timestamp, item.bid, item.ask)))
    if not ordered:
        return _empty_microstructure()
    symbols = {item.symbol for item in ordered}
    if len(symbols) != 1:
        raise ValueError("microstructure window must contain exactly one symbol")

    spreads = tuple(_exact_spread(item) for item in ordered)
    mids = tuple(exact_tick_mid(item) for item in ordered)
    changes = tuple(
        exact_tick_change(previous, current)
        for previous, current in zip(ordered, ordered[1:])
    )
    absolute_changes = tuple(abs(value) for value in changes)
    relative_spreads = tuple(
        spread / mid * 10_000.0
        for spread, mid in zip(spreads, mids)
        if mid > 0
    )
    midpoint_reference = mean(mids) if mids else 0.0

    imbalance = tick_imbalance_observation(ordered)
    velocity = tick_velocity_observation(ordered)
    acceleration = tick_acceleration_observation(ordered)
    observed = len(ordered) >= 3 and velocity.duration_seconds > 0

    return Microstructure(
        symbol=ordered[0].symbol,
        tick_count=len(ordered),
        event_count=len(changes),
        window_start=ordered[0].timestamp,
        window_end=ordered[-1].timestamp,
        duration_seconds=velocity.duration_seconds,
        latest_spread=spreads[-1],
        mean_spread=mean(spreads),
        median_spread=median(spreads),
        minimum_spread=min(spreads),
        max_spread=max(spreads),
        spread_stddev=pstdev(spreads) if len(spreads) > 1 else 0.0,
        mean_relative_spread_bps=mean(relative_spreads) if relative_spreads else 0.0,
        quote_arrival_rate=velocity.quote_arrival_rate,
        mean_interarrival_seconds=velocity.mean_interarrival_seconds,
        median_interarrival_seconds=velocity.median_interarrival_seconds,
        zero_interval_count=max(velocity.zero_interval_count, acceleration.zero_interval_count),
        net_quote_movement=imbalance.net_change,
        gross_quote_movement=imbalance.gross_distance,
        mean_absolute_quote_change=mean(absolute_changes) if absolute_changes else 0.0,
        micro_volatility=pstdev(changes) if len(changes) > 1 else 0.0,
        relative_micro_volatility_bps=(
            pstdev(changes) / midpoint_reference * 10_000.0
            if len(changes) > 1 and midpoint_reference > 0
            else 0.0
        ),
        directional_imbalance=imbalance.count_imbalance,
        distance_imbalance=imbalance.distance_imbalance,
        velocity=velocity.net_velocity,
        path_velocity=velocity.path_velocity,
        acceleration=acceleration.latest_acceleration,
        mean_absolute_acceleration=acceleration.mean_absolute_acceleration,
        maximum_absolute_acceleration=acceleration.maximum_absolute_acceleration,
        acceleration_zscore=acceleration.latest_acceleration_zscore,
        rapid_acceleration=acceleration.rapid_change,
        status="OBSERVED" if observed else "INSUFFICIENT_TICKS",
    )
