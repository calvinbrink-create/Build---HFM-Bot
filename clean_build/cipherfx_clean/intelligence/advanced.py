"""Advanced market observations and empirical profile calculations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from math import exp
from statistics import mean, median, pstdev
from typing import Iterable, Literal, Mapping, Sequence

from ..market import Candle
from .market_features import SessionRange, VolatilityObservation


@dataclass(frozen=True)
class ZoneQuality:
    freshness: float
    displacement: float
    touches: int
    penetration: float
    reaction: float
    observation_count: int
    displacement_ratio: float = 0.0
    touch_quality: float = 0.0
    score: float = 0.0
    lifecycle: str = "FRESH"


@dataclass(frozen=True)
class SpreadContext:
    current: float
    historical_mean: float
    historical_p95: float
    percentile: float
    sample_size: int


@dataclass(frozen=True)
class RelativeActivity:
    current: float
    baseline: float
    ratio: float
    sample_size: int


@dataclass(frozen=True)
class RegimeProbability:
    probabilities: Mapping[str, float]
    sample_size: int


@dataclass(frozen=True)
class BreakoutObservation:
    direction: Literal["UP", "DOWN"]
    level: float
    close_distance: float
    activity_ratio: float | None
    retest: bool
    failed: bool


@dataclass(frozen=True)
class MomentumObservation:
    slope: float
    directional_persistence: float
    impulse_ratio: float
    pullback: bool
    continuation: bool
    velocity_per_second: float = 0.0
    acceleration_per_second_squared: float = 0.0
    persistence_strength: float = 0.0
    direction: str = "FLAT"
    net_price_change: float = 0.0
    elapsed_seconds: float = 0.0
    sample_size: int = 0
    source: str = "CLOSE_TO_CLOSE_PRICE_TIME"


@dataclass(frozen=True)
class SessionTransition:
    prior_session: str
    next_session: str
    prior_direction: str
    current_direction: str
    continuation: bool
    reversal: bool


@dataclass(frozen=True)
class OpeningRange:
    session: str
    start: datetime | None
    end: datetime | None
    high: float | None
    low: float | None
    midpoint: float | None
    completed: bool
    observation_count: int
    breakout: BreakoutObservation | None


@dataclass(frozen=True)
class MeanReversionObservation:
    value: float
    reference: float
    deviation: float
    stretched: bool
    returned_to_value: bool


@dataclass(frozen=True)
class ReversalObservation:
    sweep_present: bool
    rejection_present: bool
    reclaim_present: bool
    direction: str


@dataclass(frozen=True)
class ContinuationObservation:
    impulse_direction: str
    pullback_depth: float
    resumed: bool
    invalidated: bool


@dataclass(frozen=True)
class TrendObservation:
    direction: str
    slope: float
    normalized_slope: float
    fit: float
    persistence: float
    sample_size: int


@dataclass(frozen=True)
class VWAPDeviation:
    value: float | None
    reference: float | None
    absolute_deviation: float | None
    standardized_deviation: float | None
    sample_size: int


@dataclass(frozen=True)
class BreakoutQuality:
    present: bool
    direction: str | None
    close_beyond_level: float
    body_fraction: float
    range_expansion: float
    activity_ratio: float | None
    reclaimed: bool


def zone_quality(
    candles: Sequence[Candle],
    low: float,
    high: float,
    origin_index: int,
    *,
    kind: str | None = None,
    displacement_ratio: float | None = None,
    lifecycle: str | None = None,
    activation_index: int | None = None,
    invalidated_at: int | None = None,
) -> ZoneQuality:
    if low >= high or not 0 <= origin_index < len(candles):
        raise ValueError("zone quality requires valid bounds and origin")
    activation = origin_index if activation_index is None else activation_index
    if not origin_index <= activation < len(candles):
        raise ValueError("zone activation index is outside the candle sequence")
    stop = invalidated_at + 1 if invalidated_at is not None else len(candles)
    if stop <= activation or stop > len(candles):
        raise ValueError("zone invalidation index is outside the candle sequence")
    future = tuple(candles[activation + 1:stop])
    width = high - low
    touched = tuple(
        (index, item)
        for index, item in enumerate(future)
        if item.low <= high and item.high >= low
    )
    touches = len(touched)
    penetration = max(
        (
            max(0.0, min(high, item.high) - max(low, item.low)) / width
            for _, item in touched
        ),
        default=0.0,
    )
    zone_kind = kind or _infer_zone_kind(candles, origin_index, activation)
    reaction = _zone_reaction(future, touched, low, high, zone_kind)
    ratio = displacement_ratio if displacement_ratio is not None else _displacement_ratio(candles, activation)
    displacement = 1.0 - exp(-max(0.0, ratio - 1.0))
    observed_lifecycle = lifecycle or ("FRESH" if touches == 0 else "MITIGATED" if penetration >= 1.0 else "TESTED")
    if observed_lifecycle not in {"FRESH", "TESTED", "MITIGATED", "FAILED"}:
        raise ValueError("zone quality received an invalid lifecycle")
    freshness = {"FRESH": 1.0, "TESTED": 2.0 / 3.0, "MITIGATED": 1.0 / 3.0, "FAILED": 0.0}[observed_lifecycle]
    touch_quality = 1.0 / (1.0 + touches)
    score = mean((freshness, displacement, touch_quality, 1.0 - penetration, reaction))
    return ZoneQuality(
        freshness,
        displacement,
        touches,
        penetration,
        reaction,
        len(future),
        ratio,
        touch_quality,
        score,
        observed_lifecycle,
    )


def _infer_zone_kind(candles: Sequence[Candle], origin_index: int, activation_index: int) -> str:
    origin = candles[origin_index]
    activation = candles[activation_index]
    if activation.close > origin.high:
        return "DEMAND"
    if activation.close < origin.low:
        return "SUPPLY"
    return "UNKNOWN"


def _displacement_ratio(candles: Sequence[Candle], activation_index: int) -> float:
    current = candles[activation_index]
    reference = tuple(
        max(item.high - item.low, 1e-12)
        for item in candles[max(0, activation_index - 20):activation_index]
    )
    baseline = median(reference) if reference else max(current.high - current.low, 1e-12)
    return (current.high - current.low) / max(baseline, 1e-12)


def _zone_reaction(
    future: Sequence[Candle],
    touched: Sequence[tuple[int, Candle]],
    low: float,
    high: float,
    kind: str,
) -> float:
    width = high - low
    reactions = []
    for index, _ in touched:
        window = future[index:index + 3]
        if kind == "DEMAND":
            distance = max(item.high for item in window) - high
        elif kind == "SUPPLY":
            distance = low - min(item.low for item in window)
        else:
            distance = max(
                max(item.high for item in window) - high,
                low - min(item.low for item in window),
            )
        reactions.append(min(1.0, max(0.0, distance / width)))
    return max(reactions, default=0.0)


def spread_context(current: float, historical: Sequence[float]) -> SpreadContext:
    rows = tuple(sorted(historical))
    if not rows:
        return SpreadContext(current, 0.0, 0.0, 0.0, 0)
    rank = sum(value <= current for value in rows) / len(rows)
    p95 = rows[min(len(rows) - 1, int(len(rows) * .95))]
    return SpreadContext(current, mean(rows), p95, rank, len(rows))


def relative_activity(current: float, history: Sequence[float]) -> RelativeActivity:
    baseline = mean(history) if history else 0.0
    return RelativeActivity(current, baseline, current / baseline if baseline else 0.0, len(history))


def regime_probability(labels: Sequence[str]) -> RegimeProbability:
    counts: dict[str, int] = {}
    for label in labels:
        counts[label] = counts.get(label, 0) + 1
    total = sum(counts.values())
    return RegimeProbability({key: value / total for key, value in counts.items()} if total else {}, total)


def breakout_observation(candles: Sequence[Candle], level: float, activity: float | None = None, baseline_activity: float | None = None) -> BreakoutObservation | None:
    if len(candles) < 2:
        return None
    previous, current = candles[-2:]
    up = previous.close <= level < current.close
    down = previous.close >= level > current.close
    if not up and not down:
        return None
    direction = "UP" if up else "DOWN"
    distance = abs(current.close - level)
    retest = current.low <= level <= current.high
    failed = (up and current.close <= level) or (down and current.close >= level)
    ratio = activity / baseline_activity if activity is not None and baseline_activity else None
    return BreakoutObservation(direction, level, distance, ratio, retest, failed)


def volatility_regime_distribution(observations: Sequence[VolatilityObservation]) -> dict[str, float]:
    labels = []
    for item in observations:
        labels.append("EXPANSION" if item.expansion else "COMPRESSION" if item.compression else "NORMAL")
    return dict(regime_probability(labels).probabilities)


def momentum_observation(candles: Sequence[Candle], lookback: int = 8) -> MomentumObservation:
    if lookback < 2:
        raise ValueError("momentum lookback must contain at least two candles")
    rows = tuple(candles[-lookback:])
    if len(rows) < 2:
        return MomentumObservation(
            0.0,
            0.0,
            0.0,
            False,
            False,
            sample_size=len(rows),
        )
    intervals = tuple(
        (current.end - previous.end).total_seconds()
        for previous, current in zip(rows, rows[1:])
    )
    if any(value <= 0 for value in intervals):
        raise ValueError("momentum candles must have strictly increasing end times")
    moves = tuple(
        current.close - previous.close
        for previous, current in zip(rows, rows[1:])
    )
    velocities = tuple(move / duration for move, duration in zip(moves, intervals))
    signs = tuple(1 if move > 0 else -1 if move < 0 else 0 for move in moves)
    persistence = sum(signs) / len(signs)
    net_change = rows[-1].close - rows[0].close
    elapsed = (rows[-1].end - rows[0].end).total_seconds()
    velocity = net_change / elapsed
    slope = net_change / len(moves)
    if len(velocities) >= 2:
        acceleration_elapsed = (rows[-1].end - rows[1].end).total_seconds()
        acceleration = (velocities[-1] - velocities[0]) / acceleration_elapsed
    else:
        acceleration = 0.0
    reference_moves = tuple(abs(move) for move in moves[:-1])
    impulse = abs(moves[-1]) / max(mean(reference_moves), 1e-12) if reference_moves else 0.0
    expected = 1 if velocity > 0 else -1 if velocity < 0 else 0
    pullback = signs[-1] != 0 and signs[-1] != expected
    persistence_strength = (
        sum(sign == expected for sign in signs) / len(signs)
        if expected
        else sum(sign == 0 for sign in signs) / len(signs)
    )
    return MomentumObservation(
        slope=slope,
        directional_persistence=persistence,
        impulse_ratio=impulse,
        pullback=pullback,
        continuation=not pullback and abs(persistence) >= 0.4,
        velocity_per_second=velocity,
        acceleration_per_second_squared=acceleration,
        persistence_strength=persistence_strength,
        direction="UP" if velocity > 0 else "DOWN" if velocity < 0 else "FLAT",
        net_price_change=net_change,
        elapsed_seconds=elapsed,
        sample_size=len(rows),
    )


def session_transition(prior_session: str, next_session: str, prior_direction: str, current_direction: str) -> SessionTransition:
    continuation = prior_direction == current_direction and prior_direction in {"UP", "DOWN"}
    reversal = prior_direction != current_direction and prior_direction in {"UP", "DOWN"} and current_direction in {"UP", "DOWN"}
    return SessionTransition(prior_session, next_session, prior_direction, current_direction, continuation, reversal)


def opening_range(
    candles: Sequence[Candle],
    session: str,
    *,
    session_start: datetime | None = None,
    duration: timedelta = timedelta(minutes=30),
    observed_at: datetime | None = None,
) -> OpeningRange:
    """Measure only the declared opening interval, never the whole session."""

    if duration.total_seconds() <= 0:
        raise ValueError("opening range duration must be positive")
    start = session_start or (candles[0].start if candles else None)
    if start is None:
        return OpeningRange(session, None, None, None, None, None, False, 0, None)
    end = start + duration
    opening = tuple(item for item in candles if start <= item.start < end and item.end <= end)
    if not opening:
        return OpeningRange(session, start, end, None, None, None, False, 0, None)
    high = max(item.high for item in opening)
    low = min(item.low for item in opening)
    midpoint = (high + low) / 2
    latest_time = observed_at or max(item.end for item in candles)
    completed = latest_time >= end
    following = tuple(item for item in candles if item.start >= end)
    breakout = None
    if completed and len(following) >= 2:
        breakout = breakout_observation(following, high)
        if breakout is None:
            breakout = breakout_observation(following, low)
    return OpeningRange(session, start, end, high, low, midpoint, completed, len(opening), breakout)


def mean_reversion_observation(value: float, reference: float, scale: float, return_value: float | None = None) -> MeanReversionObservation:
    deviation = value - reference
    stretched = abs(deviation) > abs(scale)
    returned = return_value is not None and abs(return_value - reference) < abs(scale) * .25
    return MeanReversionObservation(value, reference, deviation, stretched, returned)


def reversal_observation(sweep_present: bool, rejection_present: bool, reclaim_present: bool, direction: str) -> ReversalObservation:
    return ReversalObservation(sweep_present, rejection_present, reclaim_present, direction)


def continuation_observation(candles: Sequence[Candle], impulse_direction: str, pullback_depth: float) -> ContinuationObservation:
    if len(candles) < 2:
        return ContinuationObservation(impulse_direction, pullback_depth, False, False)
    prior, last = candles[-2:]
    resumed = (impulse_direction == "UP" and last.close > prior.high) or (impulse_direction == "DOWN" and last.close < prior.low)
    invalidated = (impulse_direction == "UP" and last.close < prior.low) or (impulse_direction == "DOWN" and last.close > prior.high)
    return ContinuationObservation(impulse_direction, pullback_depth, resumed, invalidated)


def trend_observation(candles: Sequence[Candle], lookback: int = 20) -> TrendObservation:
    rows = tuple(candles[-lookback:])
    if len(rows) < 2:
        return TrendObservation("UNKNOWN", 0.0, 0.0, 0.0, 0.0, len(rows))
    values = tuple(item.close for item in rows)
    x_mean = (len(values) - 1) / 2
    y_mean = mean(values)
    denominator = sum((index - x_mean) ** 2 for index in range(len(values)))
    slope = sum((index - x_mean) * (value - y_mean) for index, value in enumerate(values)) / max(denominator, 1e-12)
    fitted = tuple(y_mean + slope * (index - x_mean) for index in range(len(values)))
    total = sum((value - y_mean) ** 2 for value in values)
    residual = sum((value - estimate) ** 2 for value, estimate in zip(values, fitted))
    fit = max(0.0, 1 - residual / total) if total else 0.0
    changes = tuple(current - previous for previous, current in zip(values, values[1:]))
    expected = 1 if slope > 0 else -1 if slope < 0 else 0
    persistence = sum((1 if change > 0 else -1 if change < 0 else 0) == expected for change in changes) / len(changes) if expected and changes else 0.0
    scale = mean(item.high - item.low for item in rows)
    direction = "UP" if slope > 0 else "DOWN" if slope < 0 else "FLAT"
    return TrendObservation(direction, slope, slope / max(scale, 1e-12), fit, persistence, len(rows))


def vwap_deviation(value: float | None, reference: float | None, history: Sequence[float]) -> VWAPDeviation:
    if value is None or reference is None:
        return VWAPDeviation(value, reference, None, None, len(history))
    deviation = value - reference
    scale = pstdev(history) if len(history) > 1 else 0.0
    return VWAPDeviation(value, reference, deviation, deviation / scale if scale else None, len(history))


def breakout_quality(candles: Sequence[Candle], level: float, activity_ratio: float | None = None) -> BreakoutQuality:
    observation = breakout_observation(candles, level)
    if observation is None:
        return BreakoutQuality(False, None, 0.0, 0.0, 0.0, activity_ratio, False)
    current = candles[-1]
    body = abs(current.close - current.open)
    span = max(current.high - current.low, 1e-12)
    prior_ranges = tuple(item.high - item.low for item in candles[:-1][-20:])
    expansion = span / max(mean(prior_ranges), 1e-12) if prior_ranges else 0.0
    return BreakoutQuality(True, observation.direction, observation.close_distance, body / span, expansion, activity_ratio, observation.failed)


def correlation_breakdown(current: float, historical: Sequence[float], threshold: float = .35) -> bool:
    if not historical:
        return False
    baseline = mean(historical)
    return abs(current - baseline) > threshold
