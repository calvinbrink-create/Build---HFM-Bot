"""Price, tick, volatility, liquidity and session observations.

Every function returns observed evidence.  None of these functions creates a
trade, applies a threshold, or rejects a proposal.  That distinction keeps
the intelligence layer measurable and keeps operational execution separate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone
from hashlib import sha256
import json
from math import log, sqrt
from statistics import mean, median, pstdev
from typing import Literal, Sequence

from ..market import Candle, RawTick, exact_tick_change, exact_tick_direction, exact_tick_mid


@dataclass(frozen=True)
class FairValueGap:
    direction: Literal["BULLISH", "BEARISH"]
    low: float
    high: float
    origin_index: int
    state: Literal["UNTOUCHED", "PARTIAL", "FILLED", "FAILED"]
    left_index: int = -1
    impulse_index: int = -1
    width: float = 0.0
    midpoint: float = 0.0
    source: str = "THREE_CANDLE_WICK_IMBALANCE"
    first_touched_index: int | None = None
    filled_at: int | None = None
    failed_at: int | None = None
    fill_ratio: float = 0.0
    observation_end_index: int = -1


@dataclass(frozen=True)
class Displacement:
    direction: Literal["UP", "DOWN", "NONE"]
    range_value: float
    reference_range: float
    expansion_ratio: float
    index: int
    body_value: float = 0.0
    reference_body: float = 0.0
    body_ratio: float = 0.0
    body_participation: float = 0.0
    close_location: float = 0.5
    normalized_signed_move: float = 0.0
    range_zscore: float = 0.0
    abnormal_expansion: bool = False
    lookback: int = 0
    normalization_source: str = "PRIOR_TRUE_RANGE"


@dataclass(frozen=True)
class TickObservation:
    direction: Literal["UP", "DOWN", "UNCHANGED"]
    imbalance: float
    velocity: float
    acceleration: float
    tick_rate: float


@dataclass(frozen=True)
class TickDirectionEvent:
    event_id: str
    symbol: str
    index: int
    timestamp: datetime
    previous_timestamp: datetime
    previous_mid: float
    current_mid: float
    bid_change: float
    ask_change: float
    price_change: float
    direction: Literal["UP", "DOWN", "UNCHANGED"]
    interarrival_seconds: float
    source: str = "BID_ASK_MID_CHANGE"


@dataclass(frozen=True)
class TickImbalanceObservation:
    symbol: str
    sample_size: int
    up_count: int
    down_count: int
    unchanged_count: int
    active_move_ratio: float
    count_imbalance: float
    up_distance: float
    down_distance: float
    gross_distance: float
    net_change: float
    distance_imbalance: float
    window_start: datetime | None
    window_end: datetime | None
    source: str = "EXACT_MIDPOINT_DIRECTION_EVENTS"


@dataclass(frozen=True)
class TickVelocityObservation:
    symbol: str
    sample_size: int
    duration_seconds: float
    quote_arrival_rate: float
    mean_interarrival_seconds: float
    median_interarrival_seconds: float
    zero_interval_count: int
    net_velocity: float
    path_velocity: float
    upward_path_velocity: float
    downward_path_velocity: float
    mean_absolute_event_velocity: float
    maximum_absolute_event_velocity: float
    latest_event_velocity: float
    window_start: datetime | None
    window_end: datetime | None
    source: str = "EXACT_MIDPOINT_CHANGE_PER_ELAPSED_SECOND"


@dataclass(frozen=True)
class TickAccelerationObservation:
    symbol: str
    velocity_sample_size: int
    acceleration_sample_size: int
    latest_velocity: float
    previous_velocity: float
    latest_acceleration: float
    mean_absolute_acceleration: float
    maximum_absolute_acceleration: float
    positive_acceleration_count: int
    negative_acceleration_count: int
    unchanged_acceleration_count: int
    latest_acceleration_zscore: float
    rapid_change: bool
    zero_interval_count: int
    window_start: datetime | None
    window_end: datetime | None
    source: str = "CHANGE_IN_EVENT_VELOCITY_PER_ELAPSED_SECOND"


@dataclass(frozen=True)
class VolatilityObservation:
    realized: float
    atr: float
    normalized_atr: float
    vol_of_vol: float
    compression: bool
    expansion: bool


@dataclass(frozen=True)
class RealizedVolatilityObservation:
    realized_variance: float
    realized_volatility: float
    return_standard_deviation: float
    root_mean_square_return: float
    mean_return: float
    upside_semivolatility: float
    downside_semivolatility: float
    elapsed_seconds: float
    sample_size: int
    return_count: int
    source: str = "CLOSE_TO_CLOSE_LOG_RETURNS_NO_ATR"


@dataclass(frozen=True)
class SessionRange:
    name: str
    start: datetime
    end: datetime
    high: float | None
    low: float | None
    midpoint: float | None
    range_value: float | None


@dataclass(frozen=True)
class VWAPObservation:
    value: float | None
    last_price: float | None
    deviation: float | None
    sample_size: int
    source: str = ""


@dataclass(frozen=True)
class LiquiditySweep:
    direction: Literal["UP", "DOWN"]
    level: float
    sweep_price: float
    reclaimed: bool
    continuation: bool
    index: int
    penetration: float = 0.0
    lookback: int = 0
    observation_end_index: int = -1
    reclaim_index: int | None = None
    continuation_index: int | None = None
    outcome: Literal["RECLAIM", "CONTINUATION", "UNRESOLVED", "PENDING"] = "PENDING"
    source: str = "ROLLING_PRIOR_EXTREME"


def _ranges(candles: Sequence[Candle]) -> tuple[float, ...]:
    return tuple(max(0.0, item.high - item.low) for item in candles)


def _true_ranges(candles: Sequence[Candle]) -> tuple[float, ...]:
    values = []
    previous_close = None
    for candle in candles:
        value = candle.high - candle.low
        if previous_close is not None:
            value = max(value, abs(candle.high - previous_close), abs(candle.low - previous_close))
        values.append(max(0.0, value))
        previous_close = candle.close
    return tuple(values)


def detect_fair_value_gaps(candles: Sequence[Candle]) -> tuple[FairValueGap, ...]:
    gaps: list[FairValueGap] = []
    for index in range(2, len(candles)):
        left, middle, right = candles[index - 2:index + 1]
        if right.low > left.high:
            low, high = left.high, right.low
            state, first_touch, filled_at, failed_at, fill_ratio, observation_end = _gap_lifecycle(
                candles,
                start_index=index + 1,
                low=low,
                high=high,
                direction="BULLISH",
            )
            gaps.append(
                FairValueGap(
                    "BULLISH", low, high, index, state,
                    index - 2, index - 1, high - low, (low + high) / 2,
                    "THREE_CANDLE_WICK_IMBALANCE",
                    first_touch, filled_at, failed_at, fill_ratio, observation_end,
                )
            )
        if right.high < left.low:
            low, high = right.high, left.low
            state, first_touch, filled_at, failed_at, fill_ratio, observation_end = _gap_lifecycle(
                candles,
                start_index=index + 1,
                low=low,
                high=high,
                direction="BEARISH",
            )
            gaps.append(
                FairValueGap(
                    "BEARISH", low, high, index, state,
                    index - 2, index - 1, high - low, (low + high) / 2,
                    "THREE_CANDLE_WICK_IMBALANCE",
                    first_touch, filled_at, failed_at, fill_ratio, observation_end,
                )
            )
    return tuple(gaps)


def _gap_lifecycle(
    candles: Sequence[Candle],
    *,
    start_index: int,
    low: float,
    high: float,
    direction: Literal["BULLISH", "BEARISH"],
) -> tuple[str, int | None, int | None, int | None, float, int]:
    first_touch = None
    filled_at = None
    failed_at = None
    fill_ratio = 0.0
    observation_end = len(candles) - 1
    width = high - low
    for index in range(start_index, len(candles)):
        candle = candles[index]
        if direction == "BULLISH" and candle.low >= high:
            continue
        if direction == "BEARISH" and candle.high <= low:
            continue
        if first_touch is None:
            first_touch = index
        if direction == "BULLISH":
            penetration = high - max(candle.low, low)
            failure = candle.close < low
            filled = candle.low <= low
        else:
            penetration = min(candle.high, high) - low
            failure = candle.close > high
            filled = candle.high >= high
        fill_ratio = max(fill_ratio, min(1.0, max(0.0, penetration / width)))
        if failure:
            failed_at = index
            fill_ratio = 1.0
            observation_end = index
            return "FAILED", first_touch, filled_at, failed_at, fill_ratio, observation_end
        if filled:
            filled_at = index
            fill_ratio = 1.0
            observation_end = index
            return "FILLED", first_touch, filled_at, failed_at, fill_ratio, observation_end
    state = "PARTIAL" if first_touch is not None else "UNTOUCHED"
    return state, first_touch, filled_at, failed_at, fill_ratio, observation_end


def measure_displacement(candles: Sequence[Candle], lookback: int = 20) -> Displacement:
    if lookback < 1:
        raise ValueError("displacement lookback must be positive")
    if not candles:
        return Displacement("NONE", 0.0, 0.0, 0.0, -1, lookback=lookback)
    latest = candles[-1]
    true_ranges = _true_ranges(candles)
    current = true_ranges[-1]
    history = true_ranges[:-1][-lookback:]
    reference = mean(history) if history else current
    range_std = pstdev(history) if len(history) > 1 else 0.0
    range_zscore = (current - reference) / max(range_std, reference * 1e-9, 1e-12)
    bodies = tuple(abs(row.close - row.open) for row in candles[:-1])[-lookback:]
    body = abs(latest.close - latest.open)
    reference_body = mean(bodies) if bodies else body
    direction = "UP" if latest.close > latest.open else "DOWN" if latest.close < latest.open else "NONE"
    if direction == "UP":
        close_location = (latest.close - latest.low) / max(latest.high - latest.low, 1e-12)
    elif direction == "DOWN":
        close_location = (latest.high - latest.close) / max(latest.high - latest.low, 1e-12)
    else:
        close_location = 0.5
    return Displacement(
        direction=direction,
        range_value=current,
        reference_range=reference,
        expansion_ratio=current / max(reference, 1e-12),
        index=len(candles) - 1,
        body_value=body,
        reference_body=reference_body,
        body_ratio=body / max(reference_body, 1e-12),
        body_participation=body / max(current, 1e-12),
        close_location=min(1.0, max(0.0, close_location)),
        normalized_signed_move=(latest.close - latest.open) / max(reference, 1e-12),
        range_zscore=range_zscore,
        abnormal_expansion=range_zscore >= 2.0,
        lookback=lookback,
    )


def tick_observation(ticks: Sequence[RawTick]) -> TickObservation:
    ordered = tuple(sorted(ticks, key=lambda item: item.timestamp))
    if len(ordered) < 2:
        return TickObservation("UNCHANGED", 0.0, 0.0, 0.0, 0.0)
    events = classify_tick_directions(ordered)
    mids = tuple(exact_tick_mid(item) for item in ordered)
    moves = tuple(event.price_change for event in events)
    imbalance = tick_imbalance_observation(ordered).count_imbalance
    velocity_summary = tick_velocity_observation(ordered)
    duration = velocity_summary.duration_seconds
    velocity = velocity_summary.net_velocity
    acceleration = tick_acceleration_observation(ordered).latest_acceleration
    direction = events[-1].direction
    return TickObservation(direction, imbalance, velocity, acceleration, velocity_summary.quote_arrival_rate)


def classify_tick_directions(ticks: Sequence[RawTick]) -> tuple[TickDirectionEvent, ...]:
    ordered = tuple(sorted(ticks, key=lambda item: (item.timestamp, item.bid, item.ask)))
    symbols = {item.symbol for item in ordered}
    if len(symbols) > 1:
        raise ValueError("tick direction classification requires one symbol")
    events = []
    for index, (previous, current) in enumerate(zip(ordered, ordered[1:]), start=1):
        change = exact_tick_change(previous, current)
        direction = exact_tick_direction(previous, current)
        identity = (
            current.symbol,
            previous.timestamp.isoformat(),
            current.timestamp.isoformat(),
            previous.bid,
            previous.ask,
            current.bid,
            current.ask,
        )
        event_id = sha256(
            json.dumps(identity, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        events.append(
            TickDirectionEvent(
                event_id=event_id,
                symbol=current.symbol,
                index=index,
                timestamp=current.timestamp,
                previous_timestamp=previous.timestamp,
                previous_mid=exact_tick_mid(previous),
                current_mid=exact_tick_mid(current),
                bid_change=current.bid - previous.bid,
                ask_change=current.ask - previous.ask,
                price_change=change,
                direction=direction,
                interarrival_seconds=(current.timestamp - previous.timestamp).total_seconds(),
            )
        )
    return tuple(events)


def tick_imbalance_observation(ticks: Sequence[RawTick]) -> TickImbalanceObservation:
    ordered = tuple(sorted(ticks, key=lambda item: (item.timestamp, item.bid, item.ask)))
    events = classify_tick_directions(ordered)
    symbol = ordered[0].symbol if ordered else ""
    up = sum(event.direction == "UP" for event in events)
    down = sum(event.direction == "DOWN" for event in events)
    unchanged = sum(event.direction == "UNCHANGED" for event in events)
    active = up + down
    up_distance = sum(max(0.0, event.price_change) for event in events)
    down_distance = sum(max(0.0, -event.price_change) for event in events)
    gross = up_distance + down_distance
    return TickImbalanceObservation(
        symbol=symbol,
        sample_size=len(events),
        up_count=up,
        down_count=down,
        unchanged_count=unchanged,
        active_move_ratio=active / len(events) if events else 0.0,
        count_imbalance=(up - down) / active if active else 0.0,
        up_distance=up_distance,
        down_distance=down_distance,
        gross_distance=gross,
        net_change=up_distance - down_distance,
        distance_imbalance=(up_distance - down_distance) / gross if gross else 0.0,
        window_start=ordered[0].timestamp if ordered else None,
        window_end=ordered[-1].timestamp if ordered else None,
    )


def tick_velocity_observation(ticks: Sequence[RawTick]) -> TickVelocityObservation:
    ordered = tuple(sorted(ticks, key=lambda item: (item.timestamp, item.bid, item.ask)))
    events = classify_tick_directions(ordered)
    imbalance = tick_imbalance_observation(ordered)
    duration = (
        max(0.0, (ordered[-1].timestamp - ordered[0].timestamp).total_seconds())
        if len(ordered) > 1
        else 0.0
    )
    intervals = tuple(event.interarrival_seconds for event in events)
    velocities = tuple(
        event.price_change / event.interarrival_seconds
        for event in events
        if event.interarrival_seconds > 0
    )
    absolute_velocities = tuple(abs(value) for value in velocities)
    latest_velocity = (
        events[-1].price_change / events[-1].interarrival_seconds
        if events and events[-1].interarrival_seconds > 0
        else 0.0
    )
    symbol = ordered[0].symbol if ordered else ""
    return TickVelocityObservation(
        symbol=symbol,
        sample_size=len(events),
        duration_seconds=duration,
        quote_arrival_rate=len(events) / duration if duration else 0.0,
        mean_interarrival_seconds=mean(intervals) if intervals else 0.0,
        median_interarrival_seconds=median(intervals) if intervals else 0.0,
        zero_interval_count=sum(value <= 0 for value in intervals),
        net_velocity=imbalance.net_change / duration if duration else 0.0,
        path_velocity=imbalance.gross_distance / duration if duration else 0.0,
        upward_path_velocity=imbalance.up_distance / duration if duration else 0.0,
        downward_path_velocity=imbalance.down_distance / duration if duration else 0.0,
        mean_absolute_event_velocity=mean(absolute_velocities) if absolute_velocities else 0.0,
        maximum_absolute_event_velocity=max(absolute_velocities, default=0.0),
        latest_event_velocity=latest_velocity,
        window_start=ordered[0].timestamp if ordered else None,
        window_end=ordered[-1].timestamp if ordered else None,
    )


def tick_acceleration_observation(ticks: Sequence[RawTick]) -> TickAccelerationObservation:
    ordered = tuple(sorted(ticks, key=lambda item: (item.timestamp, item.bid, item.ask)))
    events = classify_tick_directions(ordered)
    velocity_events = tuple(
        (event.timestamp, event.price_change / event.interarrival_seconds)
        for event in events
        if event.interarrival_seconds > 0
    )
    accelerations = []
    zero_intervals = sum(event.interarrival_seconds <= 0 for event in events)
    for (previous_time, previous_velocity), (current_time, current_velocity) in zip(
        velocity_events, velocity_events[1:]
    ):
        elapsed = (current_time - previous_time).total_seconds()
        if elapsed <= 0:
            zero_intervals += 1
            continue
        accelerations.append((current_velocity - previous_velocity) / elapsed)
    values = tuple(accelerations)
    latest = values[-1] if values else 0.0
    baseline = values[:-1]
    baseline_mean = mean(baseline) if baseline else 0.0
    baseline_std = pstdev(baseline) if len(baseline) > 1 else 0.0
    scale = max(baseline_std, mean(tuple(abs(value) for value in baseline)) * 1e-9 if baseline else 0.0, 1e-12)
    zscore = (latest - baseline_mean) / scale if values else 0.0
    latest_velocity = velocity_events[-1][1] if velocity_events else 0.0
    previous_velocity = velocity_events[-2][1] if len(velocity_events) > 1 else 0.0
    absolute = tuple(abs(value) for value in values)
    return TickAccelerationObservation(
        symbol=ordered[0].symbol if ordered else "",
        velocity_sample_size=len(velocity_events),
        acceleration_sample_size=len(values),
        latest_velocity=latest_velocity,
        previous_velocity=previous_velocity,
        latest_acceleration=latest,
        mean_absolute_acceleration=mean(absolute) if absolute else 0.0,
        maximum_absolute_acceleration=max(absolute, default=0.0),
        positive_acceleration_count=sum(value > 0 for value in values),
        negative_acceleration_count=sum(value < 0 for value in values),
        unchanged_acceleration_count=sum(value == 0 for value in values),
        latest_acceleration_zscore=zscore,
        rapid_change=abs(zscore) >= 2.0,
        zero_interval_count=zero_intervals,
        window_start=ordered[0].timestamp if ordered else None,
        window_end=ordered[-1].timestamp if ordered else None,
    )


def realized_volatility_observation(
    candles: Sequence[Candle],
    lookback: int = 20,
) -> RealizedVolatilityObservation:
    if lookback < 2:
        raise ValueError("realized-volatility lookback must contain at least two candles")
    rows = tuple(candles[-lookback:])
    if len(rows) < 2:
        return RealizedVolatilityObservation(
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            len(rows),
            0,
        )
    if any(row.close <= 0 for row in rows):
        raise ValueError("realized volatility requires positive close prices")
    intervals = tuple(
        (current.end - previous.end).total_seconds()
        for previous, current in zip(rows, rows[1:])
    )
    if any(value <= 0 for value in intervals):
        raise ValueError("realized-volatility candles must have strictly increasing end times")
    returns = tuple(
        log(current.close / previous.close)
        for previous, current in zip(rows, rows[1:])
    )
    squared = tuple(value * value for value in returns)
    upside_squared = tuple(value * value for value in returns if value > 0)
    downside_squared = tuple(value * value for value in returns if value < 0)
    variance = sum(squared)
    return RealizedVolatilityObservation(
        realized_variance=variance,
        realized_volatility=sqrt(variance),
        return_standard_deviation=pstdev(returns) if len(returns) > 1 else 0.0,
        root_mean_square_return=sqrt(mean(squared)) if squared else 0.0,
        mean_return=mean(returns) if returns else 0.0,
        upside_semivolatility=sqrt(sum(upside_squared)) if upside_squared else 0.0,
        downside_semivolatility=sqrt(sum(downside_squared)) if downside_squared else 0.0,
        elapsed_seconds=(rows[-1].end - rows[0].end).total_seconds(),
        sample_size=len(rows),
        return_count=len(returns),
    )


def volatility_observation(candles: Sequence[Candle], lookback: int = 20) -> VolatilityObservation:
    rows = tuple(candles[-lookback:])
    if not rows:
        return VolatilityObservation(0.0, 0.0, 0.0, 0.0, False, False)
    ranges = _ranges(rows)
    true_ranges = _true_ranges(rows)
    realized = realized_volatility_observation(rows, lookback=max(2, len(rows)))
    atr = mean(true_ranges[-14:])
    normalized = atr / max(abs(rows[-1].close), 1e-12)
    prior = ranges[:-1]
    range_change = ranges[-1] / max(mean(prior), 1e-12) if prior else 1.0
    vol_of_vol = pstdev(ranges) / max(mean(ranges), 1e-12) if len(ranges) > 1 else 0.0
    return VolatilityObservation(
        realized.return_standard_deviation,
        atr,
        normalized,
        vol_of_vol,
        range_change < 0.7,
        range_change > 1.5,
    )


def build_session_range(candles: Sequence[Candle], name: str, start_hour: int, end_hour: int) -> SessionRange:
    if not candles:
        now = datetime.now(timezone.utc)
        return SessionRange(name, now, now, None, None, None, None)
    latest = candles[-1].end.astimezone(timezone.utc)
    day = latest.date()
    start = datetime.combine(day, time(start_hour), timezone.utc)
    end = datetime.combine(day, time(end_hour), timezone.utc)
    rows = tuple(item for item in candles if start <= item.start.astimezone(timezone.utc) < end)
    high = max((item.high for item in rows), default=None)
    low = min((item.low for item in rows), default=None)
    midpoint = (high + low) / 2 if high is not None and low is not None else None
    return SessionRange(name, start, end, high, low, midpoint, high - low if high is not None and low is not None else None)


def session_vwap(ticks: Sequence[RawTick]) -> VWAPObservation:
    if not ticks:
        return VWAPObservation(None, None, None, 0, "NO_TICKS")
    ordered = tuple(sorted(ticks, key=lambda item: item.timestamp))
    value = mean(item.mid for item in ordered)
    last = ordered[-1].mid
    return VWAPObservation(value, last, last - value, len(ordered), "UNWEIGHTED_QUOTE_MEAN_NOT_TRUE_VWAP")


def candle_vwap(candles: Sequence[Candle]) -> VWAPObservation:
    rows = tuple(candles)
    if not rows:
        return VWAPObservation(None, None, None, 0, "NO_CANDLES")
    weights = tuple(max(item.tick_volume, 0.0) for item in rows)
    total = sum(weights)
    if total <= 0:
        return VWAPObservation(None, rows[-1].close, None, len(rows), "NO_BROKER_ACTIVITY_WEIGHT")
    typical = tuple((item.high + item.low + item.close) / 3 for item in rows)
    value = sum(price * weight for price, weight in zip(typical, weights)) / total
    return VWAPObservation(value, rows[-1].close, rows[-1].close - value, len(rows), "HFM_TICK_VOLUME_WEIGHTED_TYPICAL_PRICE")


def _liquidity_sweep_outcome(
    candles: Sequence[Candle],
    *,
    index: int,
    direction: Literal["UP", "DOWN"],
    level: float,
    sweep_price: float,
    outcome_window: int,
) -> tuple[bool, bool, int | None, int | None, str, int]:
    observation_end = min(len(candles) - 1, index + outcome_window)
    reclaim_index = None
    continuation_index = None
    for future_index in range(index, observation_end + 1):
        candle = candles[future_index]
        if direction == "UP":
            reclaimed = candle.close < level
            continued = (
                future_index > index
                and candle.close > level
                and candle.high > sweep_price
            )
        else:
            reclaimed = candle.close > level
            continued = (
                future_index > index
                and candle.close < level
                and candle.low < sweep_price
            )
        if reclaimed:
            reclaim_index = future_index
            break
        if continued:
            continuation_index = future_index
            break
    if reclaim_index is not None:
        outcome = "RECLAIM"
    elif continuation_index is not None:
        outcome = "CONTINUATION"
    elif observation_end < index + outcome_window:
        outcome = "PENDING"
    else:
        outcome = "UNRESOLVED"
    return (
        reclaim_index is not None,
        continuation_index is not None,
        reclaim_index,
        continuation_index,
        outcome,
        observation_end,
    )


def detect_liquidity_sweeps(
    candles: Sequence[Candle],
    lookback: int = 10,
    outcome_window: int = 5,
) -> tuple[LiquiditySweep, ...]:
    if lookback < 1:
        raise ValueError("liquidity sweep lookback must be positive")
    if outcome_window < 1:
        raise ValueError("liquidity sweep outcome window must be positive")
    sweeps: list[LiquiditySweep] = []
    for index in range(lookback, len(candles)):
        current = candles[index]
        history = candles[index - lookback:index]
        prior_high = max(item.high for item in history)
        prior_low = min(item.low for item in history)
        if current.high > prior_high:
            reclaimed, continuation, reclaim_index, continuation_index, outcome, observation_end = _liquidity_sweep_outcome(
                candles,
                index=index,
                direction="UP",
                level=prior_high,
                sweep_price=current.high,
                outcome_window=outcome_window,
            )
            sweeps.append(
                LiquiditySweep(
                    "UP",
                    prior_high,
                    current.high,
                    reclaimed,
                    continuation,
                    index,
                    current.high - prior_high,
                    lookback,
                    observation_end,
                    reclaim_index,
                    continuation_index,
                    outcome,
                )
            )
        if current.low < prior_low:
            reclaimed, continuation, reclaim_index, continuation_index, outcome, observation_end = _liquidity_sweep_outcome(
                candles,
                index=index,
                direction="DOWN",
                level=prior_low,
                sweep_price=current.low,
                outcome_window=outcome_window,
            )
            sweeps.append(
                LiquiditySweep(
                    "DOWN",
                    prior_low,
                    current.low,
                    reclaimed,
                    continuation,
                    index,
                    prior_low - current.low,
                    lookback,
                    observation_end,
                    reclaim_index,
                    continuation_index,
                    outcome,
                )
            )
    return tuple(sweeps)
