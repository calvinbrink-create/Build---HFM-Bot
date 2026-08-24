"""Synchronized whole-market history, feature vectors, outcomes, and analogues."""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from math import cos, isfinite, log1p, pi, sin, sqrt
from statistics import mean, pstdev
from typing import Iterable, Iterator, Mapping, Sequence

from .calendar import TradingCalendar
from .contracts import Candle, MarketSnapshot
from .data_quality import validate_research_window
from .hfm_data import SourceDataset
from .intelligence.evidence import build_timeframe_evidence
from .intelligence.market_features import tick_observation
from .intelligence.session import active_sessions, session_label


DEFAULT_HORIZONS = (60, 180, 300, 900, 3600, 14400)
TICK_HORIZONS = (5, 15, 30, 60, 180, 300, 900)
FEATURE_SCHEMA_VERSION = 4

TIMEFRAME_FEATURES = (
    "available",
    "body_return",
    "range_return",
    "lower_wick_fraction",
    "upper_wick_fraction",
    "close_location_in_range",
    "mean_body_return",
    "body_return_volatility",
    "mean_range_return",
    "range_return_volatility",
    "relative_tick_volume",
    "spread_return",
    "structure_direction",
    "bos_direction",
    "choch_direction",
    "structure_strength",
    "structure_persistence",
    "swing_count_log",
    "regime_state",
    "regime_volatility",
    "regime_persistence",
    "realized_volatility",
    "normalized_atr",
    "volatility_of_volatility",
    "compression",
    "expansion",
    "displacement_direction",
    "displacement_ratio",
    "trend_normalized_slope",
    "trend_fit",
    "trend_persistence",
    "momentum_slope_return",
    "momentum_persistence",
    "momentum_impulse_ratio",
    "momentum_pullback",
    "momentum_continuation",
    "active_demand_count_log",
    "active_supply_count_log",
    "nearest_demand_distance_return",
    "nearest_supply_distance_return",
    "nearest_zone_freshness",
    "liquidity_above_distance_return",
    "liquidity_below_distance_return",
    "liquidity_pool_count_log",
    "recent_up_sweep",
    "recent_down_sweep",
    "active_bullish_fvg_count_log",
    "active_bearish_fvg_count_log",
    "nearest_bullish_fvg_distance_return",
    "nearest_bearish_fvg_distance_return",
    "candle_pattern_bias",
    "candle_pattern_count_log",
    "geometric_pattern_bias",
    "geometric_pattern_count_log",
    "confirmed_geometric_count_log",
    "failed_geometric_count_log",
    "price_location_20",
    "distance_from_20_high_return",
    "distance_from_20_low_return",
)
TIMEFRAME_CATEGORICAL_FEATURES = frozenset({
    "available",
    "structure_direction",
    "bos_direction",
    "choch_direction",
    "regime_state",
    "compression",
    "expansion",
    "displacement_direction",
    "momentum_pullback",
    "momentum_continuation",
    "recent_up_sweep",
    "recent_down_sweep",
})
GLOBAL_FEATURES = (
    "tick_available",
    "tick_sample_count_log",
    "tick_duration_seconds_log",
    "tick_direction",
    "tick_imbalance",
    "tick_velocity_return_per_second",
    "tick_acceleration_return_per_second2",
    "tick_rate_log",
    "current_spread_return",
    "session_asia",
    "session_london",
    "session_new_york",
    "session_overlap",
    "minute_sin",
    "minute_cos",
    "weekday_sin",
    "weekday_cos",
    "cross_asset_available",
    "cross_asset_count_log",
    "cross_asset_mean_absolute_correlation",
    "cross_asset_max_absolute_correlation",
    "cross_asset_breakdown_count_log",
)
GLOBAL_CATEGORICAL_FEATURES = frozenset({
    "tick_available",
    "tick_direction",
    "session_asia",
    "session_london",
    "session_new_york",
    "session_overlap",
    "cross_asset_available",
})
FEATURES_PER_TIMEFRAME = len(TIMEFRAME_FEATURES)
GLOBAL_FEATURE_COUNT = len(GLOBAL_FEATURES)


@dataclass(frozen=True)
class HistoricalState:
    state_id: str
    dataset_id: str
    symbol: str
    observed_at: datetime
    vector: tuple[float, ...]
    labels: tuple[str, ...]
    source_ids: tuple[str, ...]
    feature_schema_version: int = FEATURE_SCHEMA_VERSION


@dataclass(frozen=True)
class HorizonOutcome:
    horizon_seconds: int
    close_return: float
    mfe_return: float
    mae_return: float


@dataclass(frozen=True)
class HistoricalPath:
    state_id: str
    outcomes: tuple[HorizonOutcome, ...]
    fidelity: str


@dataclass(frozen=True)
class SimilarState:
    state_id: str
    symbol: str
    observed_at: datetime
    distance: float
    labels: tuple[str, ...]
    path: HistoricalPath | None


def feature_vector(
    snapshot: MarketSnapshot,
    timeframes: Sequence[str],
    *,
    cross_asset_context: Mapping[str, float] | None = None,
) -> HistoricalState:
    vector: list[float] = []
    labels: list[str] = [f"SESSION:{session_label(snapshot.observed_at)}"]
    source_ids: list[str] = list(snapshot.source_ids)
    evidence = build_timeframe_evidence({
        timeframe: tuple(snapshot.candles.get(timeframe, ()))
        for timeframe in timeframes
    })
    for timeframe in timeframes:
        rows = snapshot.candles.get(timeframe, ())
        if not rows:
            vector.extend((0.0,) * FEATURES_PER_TIMEFRAME)
            labels.append(f"{timeframe}:MISSING")
            continue
        current = rows[-1]
        history = rows[-20:]
        price = max(abs(current.close), 1e-12)
        candle_range = max(current.high - current.low, 1e-12)
        returns = tuple((row.close - row.open) / max(abs(row.open), 1e-12) for row in history)
        ranges = tuple((row.high - row.low) / max(abs(row.close), 1e-12) for row in history)
        volumes = tuple(row.tick_volume for row in history)
        structure = evidence.structure[timeframe]
        regime = evidence.regimes[timeframe]
        patterns = evidence.patterns[timeframe]
        geometric = evidence.geometric_patterns[timeframe]
        zones = evidence.zones[timeframe]
        liquidity = evidence.liquidity[timeframe]
        gaps = evidence.fair_value_gaps[timeframe]
        sweeps = evidence.liquidity_sweeps[timeframe]
        displacement = evidence.displacement[timeframe]
        volatility = evidence.volatility[timeframe]
        trend = evidence.trend[timeframe]
        momentum = evidence.momentum[timeframe]
        active_zones = tuple(item for item in zones if item.freshness != "FAILED")
        demand = tuple(item for item in active_zones if item.kind == "DEMAND")
        supply = tuple(item for item in active_zones if item.kind == "SUPPLY")
        nearest_demand = min(demand, key=lambda item: abs(current.close - (item.low + item.high) / 2), default=None)
        nearest_supply = min(supply, key=lambda item: abs(current.close - (item.low + item.high) / 2), default=None)
        nearest_zone = min(active_zones, key=lambda item: abs(current.close - (item.low + item.high) / 2), default=None)
        above = tuple(item for item in liquidity if item.price >= current.close)
        below = tuple(item for item in liquidity if item.price <= current.close)
        active_bullish_gaps = tuple(item for item in gaps if item.direction == "BULLISH" and item.state in {"UNTOUCHED", "PARTIAL"})
        active_bearish_gaps = tuple(item for item in gaps if item.direction == "BEARISH" and item.state in {"UNTOUCHED", "PARTIAL"})
        recent_start = max(0, len(rows) - 10)
        recent_sweeps = tuple(item for item in sweeps if item.index >= recent_start)
        low_20 = min(row.low for row in history)
        high_20 = max(row.high for row in history)
        span_20 = max(high_20 - low_20, 1e-12)
        vector.extend(
            (
                1.0,
                (current.close - current.open) / price,
                (current.high - current.low) / price,
                (min(current.open, current.close) - current.low) / candle_range,
                (current.high - max(current.open, current.close)) / candle_range,
                (current.close - current.low) / candle_range,
                mean(returns) if returns else 0.0,
                pstdev(returns) if len(returns) > 1 else 0.0,
                mean(ranges) if ranges else 0.0,
                pstdev(ranges) if len(ranges) > 1 else 0.0,
                (volumes[-1] / max(mean(volumes[:-1]), 1e-12)) if len(volumes) > 1 else 0.0,
                current.spread_points * float(snapshot.contract.get("point", 0.0)) / price,
                {"UP": 1.0, "DOWN": -1.0, "RANGE": 0.0, "UNKNOWN": 0.0}[structure.direction],
                _direction_value(structure.bos_direction),
                _direction_value(structure.choch_direction),
                structure.trend_strength,
                structure.directional_persistence,
                log1p(len(structure.swings)),
                {"TREND_UP": 1.0, "TREND_DOWN": -1.0, "EXPANSION": .5, "COMPRESSION": -.5, "RANGE": 0.0, "UNKNOWN": 0.0}[regime.label],
                regime.volatility,
                regime.persistence,
                volatility.realized,
                volatility.normalized_atr,
                volatility.vol_of_vol,
                float(volatility.compression),
                float(volatility.expansion),
                _direction_value(displacement.direction),
                displacement.expansion_ratio,
                trend.normalized_slope,
                trend.fit,
                trend.persistence,
                momentum.slope / price,
                momentum.directional_persistence,
                momentum.impulse_ratio,
                float(momentum.pullback),
                float(momentum.continuation),
                log1p(len(demand)),
                log1p(len(supply)),
                _zone_distance(nearest_demand, current.close, price, "DEMAND"),
                _zone_distance(nearest_supply, current.close, price, "SUPPLY"),
                _zone_freshness(nearest_zone),
                min((item.price - current.close) / price for item in above) if above else 0.0,
                min((current.close - item.price) / price for item in below) if below else 0.0,
                log1p(len(liquidity)),
                float(any(item.direction == "UP" for item in recent_sweeps)),
                float(any(item.direction == "DOWN" for item in recent_sweeps)),
                log1p(len(active_bullish_gaps)),
                log1p(len(active_bearish_gaps)),
                _gap_distance(active_bullish_gaps, current.close, price),
                _gap_distance(active_bearish_gaps, current.close, price),
                _pattern_bias(patterns),
                log1p(len(patterns)),
                _pattern_bias(geometric),
                log1p(len(geometric)),
                log1p(sum(item.confirmed for item in geometric)),
                log1p(sum(item.failed for item in geometric)),
                (current.close - low_20) / span_20,
                (high_20 - current.close) / price,
                (current.close - low_20) / price,
            )
        )
        labels.extend((f"{timeframe}:STRUCTURE:{structure.direction}", f"{timeframe}:REGIME:{regime.label}"))
        labels.extend(f"{timeframe}:CANDLE:{item.name}:{item.direction}" for item in patterns)
        labels.extend(f"{timeframe}:CHART:{item.name}:{item.direction}" for item in geometric)
        if current.source_id:
            source_ids.append(current.source_id)
    vector.extend(_global_features(snapshot, cross_asset_context))
    state_payload = (
        FEATURE_SCHEMA_VERSION,
        snapshot.symbol,
        snapshot.observed_at.isoformat(),
        tuple(sorted(set(source_ids))),
        tuple(vector),
    )
    state_id = sha256(json.dumps(state_payload, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()
    return HistoricalState(
        state_id,
        snapshot.source_ids[0] if snapshot.source_ids else "",
        snapshot.symbol,
        snapshot.observed_at,
        tuple(vector),
        tuple(sorted(set(labels))),
        tuple(sorted(set(source_ids))),
        FEATURE_SCHEMA_VERSION,
    )


def feature_names(timeframes: Sequence[str]) -> tuple[str, ...]:
    return tuple(
        [f"{timeframe}:{suffix}" for timeframe in timeframes for suffix in TIMEFRAME_FEATURES]
        + [f"GLOBAL:{suffix}" for suffix in GLOBAL_FEATURES]
    )


def feature_kinds(timeframes: Sequence[str]) -> tuple[str, ...]:
    return tuple(
        [
            "CATEGORICAL" if suffix in TIMEFRAME_CATEGORICAL_FEATURES else "CONTINUOUS"
            for _timeframe in timeframes
            for suffix in TIMEFRAME_FEATURES
        ]
        + [
            "CATEGORICAL" if suffix in GLOBAL_CATEGORICAL_FEATURES else "CONTINUOUS"
            for suffix in GLOBAL_FEATURES
        ]
    )


def _direction_value(direction: str | None) -> float:
    return 1.0 if direction in {"UP", "BULLISH"} else -1.0 if direction in {"DOWN", "BEARISH"} else 0.0


def _zone_distance(zone: object | None, close: float, price: float, kind: str) -> float:
    if zone is None:
        return 0.0
    boundary = zone.high if kind == "DEMAND" else zone.low
    return (close - boundary) / price if kind == "DEMAND" else (boundary - close) / price


def _zone_freshness(zone: object | None) -> float:
    if zone is None:
        return 0.0
    return {"FRESH": 1.0, "TESTED": 0.5, "MITIGATED": 0.25, "FAILED": 0.0}[zone.freshness]


def _gap_distance(gaps: Sequence[object], close: float, price: float) -> float:
    if not gaps:
        return 0.0
    gap = min(gaps, key=lambda item: abs(close - (item.low + item.high) / 2))
    return ((gap.low + gap.high) / 2 - close) / price


def _pattern_bias(patterns: Sequence[object]) -> float:
    if not patterns:
        return 0.0
    weighted = []
    for item in patterns:
        strength = float(getattr(item, "strength", getattr(item, "confidence", 1.0)))
        weighted.append(_direction_value(item.direction) * strength)
    return mean(weighted)


def _global_features(snapshot: MarketSnapshot, cross_asset_context: Mapping[str, float] | None) -> tuple[float, ...]:
    ticks = tuple(sorted(snapshot.ticks, key=lambda item: item.timestamp))
    tick = tick_observation(ticks)
    tick_available = bool(ticks)
    duration = (ticks[-1].timestamp - ticks[0].timestamp).total_seconds() if len(ticks) > 1 else 0.0
    price = max(abs(ticks[-1].mid), 1e-12) if ticks else 1.0
    sessions = frozenset(active_sessions(snapshot.observed_at))
    minute = snapshot.observed_at.hour * 60 + snapshot.observed_at.minute
    minute_angle = 2 * pi * minute / 1440
    weekday_angle = 2 * pi * snapshot.observed_at.weekday() / 7
    context = dict(cross_asset_context or {})
    cross_available = bool(context.get("available", 0.0))
    values = (
        float(tick_available),
        log1p(len(ticks)),
        log1p(max(duration, 0.0)),
        _direction_value(tick.direction),
        tick.imbalance,
        tick.velocity / price,
        tick.acceleration / price,
        log1p(max(tick.tick_rate, 0.0)),
        ticks[-1].spread / price if ticks else 0.0,
        float("ASIA" in sessions),
        float("LONDON" in sessions),
        float("NEW_YORK" in sessions),
        float("LONDON" in sessions and "NEW_YORK" in sessions),
        sin(minute_angle),
        cos(minute_angle),
        sin(weekday_angle),
        cos(weekday_angle),
        float(cross_available),
        log1p(max(context.get("count", 0.0), 0.0)),
        context.get("mean_absolute_correlation", 0.0),
        context.get("max_absolute_correlation", 0.0),
        log1p(max(context.get("breakdown_count", 0.0), 0.0)),
    )
    if len(values) != GLOBAL_FEATURE_COUNT or any(not isfinite(value) for value in values):
        raise ValueError("invalid global market-state fingerprint")
    return values


def synchronized_snapshots(
    dataset: SourceDataset,
    *,
    timeframes: Sequence[str],
    anchor_timeframe: str = "M1",
    lookback: int = 100,
    warmup: int = 100,
    stride: int = 1,
    window_calendars: Mapping[str, TradingCalendar] | None = None,
    exclude_contaminated: bool = True,
    minimum_bars: Mapping[str, int] | None = None,
) -> Iterator[MarketSnapshot]:
    anchor = dataset.bars.get(anchor_timeframe, ())
    if stride <= 0 or lookback <= 0:
        raise ValueError("stride and lookback must be positive")
    starts = {frame: tuple(row.end for row in dataset.bars.get(frame, ())) for frame in timeframes}
    for anchor_index in range(warmup, len(anchor), stride):
        observed_at = anchor[anchor_index].end
        frames: dict[str, tuple[Candle, ...]] = {}
        status: dict[str, str] = {}
        for timeframe in timeframes:
            rows = dataset.bars.get(timeframe, ())
            index = bisect_right(starts[timeframe], observed_at)
            selected = tuple(rows[max(0, index - lookback):index])
            frames[timeframe] = selected
            status[timeframe] = "COMPLETED" if selected else "MISSING"
        if window_calendars and exclude_contaminated:
            qualities = tuple(
                validate_research_window(
                    frames[timeframe],
                    timeframe=timeframe,
                    calendar=window_calendars[timeframe],
                    minimum_bars=(minimum_bars or {}).get(timeframe, min(lookback, warmup)),
                )
                for timeframe in timeframes
            )
            if any(item.status != "CLEAN" for item in qualities):
                continue
        source_ids = (dataset.dataset_id,)
        yield MarketSnapshot(dataset.symbol, observed_at, (), frames, status, source_ids)


def path_outcome(
    state: HistoricalState,
    anchor: Sequence[Candle],
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    *,
    anchor_ends: Sequence[datetime] | None = None,
) -> HistoricalPath:
    ends = tuple(anchor_ends) if anchor_ends is not None else tuple(row.end for row in anchor)
    if len(ends) != len(anchor):
        raise ValueError("anchor_ends length does not match anchor")
    index = bisect_right(ends, state.observed_at)
    if index == 0 or index >= len(anchor):
        return HistoricalPath(state.state_id, (), "BAR_OHLC_NO_EXECUTABLE_TICKS")
    entry = anchor[index - 1].close
    output: list[HorizonOutcome] = []
    for horizon in horizons:
        horizon_end = datetime.fromtimestamp(state.observed_at.timestamp() + horizon, tz=state.observed_at.tzinfo)
        end_index = bisect_right(ends, horizon_end)
        future = anchor[index:end_index]
        if not future:
            continue
        output.append(
            HorizonOutcome(
                horizon,
                (future[-1].close - entry) / max(abs(entry), 1e-12),
                (max(row.high for row in future) - entry) / max(abs(entry), 1e-12),
                (min(row.low for row in future) - entry) / max(abs(entry), 1e-12),
            )
        )
    return HistoricalPath(state.state_id, tuple(output), "BAR_OHLC_NO_EXECUTABLE_TICKS")


def tick_path_outcome(
    state: HistoricalState,
    ticks: Sequence[object],
    horizons: Sequence[int] = TICK_HORIZONS,
) -> HistoricalPath:
    from bisect import bisect_right

    ordered = tuple(sorted(ticks, key=lambda item: item.timestamp))
    timestamps = tuple(item.timestamp for item in ordered)
    index = bisect_right(timestamps, state.observed_at)
    if index >= len(ordered):
        return HistoricalPath(state.state_id, (), "HFM_EXECUTABLE_TICKS")
    entry = ordered[index].mid
    output = []
    for horizon in horizons:
        horizon_end = datetime.fromtimestamp(state.observed_at.timestamp() + horizon, tz=state.observed_at.tzinfo)
        end_index = bisect_right(timestamps, horizon_end)
        future = ordered[index:end_index]
        if not future:
            continue
        output.append(
            HorizonOutcome(
                horizon,
                (future[-1].mid - entry) / max(abs(entry), 1e-12),
                (max(item.bid for item in future) - entry) / max(abs(entry), 1e-12),
                (min(item.ask for item in future) - entry) / max(abs(entry), 1e-12),
            )
        )
    return HistoricalPath(state.state_id, tuple(output), "HFM_EXECUTABLE_TICKS")


class HistoricalAnalogueIndex:
    """Inspectable standardized nearest-neighbour index with attached paths."""

    def __init__(self, states: Iterable[HistoricalState] = (), paths: Iterable[HistoricalPath] = ()):
        self._states: dict[str, HistoricalState] = {row.state_id: row for row in states}
        self._paths: dict[str, HistoricalPath] = {row.state_id: row for row in paths}

    def add(self, state: HistoricalState, path: HistoricalPath | None = None) -> None:
        if state.state_id in self._states and self._states[state.state_id] != state:
            raise ValueError("historical state identity conflict")
        self._states[state.state_id] = state
        if path is not None:
            if path.state_id != state.state_id:
                raise ValueError("path state identity mismatch")
            self._paths[state.state_id] = path

    def search(self, current: HistoricalState, *, limit: int = 20, same_symbol: bool = True) -> tuple[SimilarState, ...]:
        candidates = tuple(row for row in self._states.values() if row.state_id != current.state_id and (not same_symbol or row.symbol == current.symbol))
        if not candidates or limit <= 0:
            return ()
        width = len(current.vector)
        if any(len(row.vector) != width for row in candidates):
            raise ValueError("incompatible feature vector dimensions")
        columns = tuple(tuple(row.vector[index] for row in candidates) for index in range(width))
        centers = tuple(mean(column) for column in columns)
        scales = tuple(pstdev(column) if len(column) > 1 else 1.0 for column in columns)
        def distance(row: HistoricalState) -> float:
            return sqrt(sum(((left - right) / max(scale, 1e-12)) ** 2 for left, right, scale in zip(current.vector, row.vector, scales)))
        ranked = sorted(candidates, key=lambda row: (distance(row), row.observed_at, row.state_id))[:limit]
        return tuple(SimilarState(row.state_id, row.symbol, row.observed_at, distance(row), row.labels, self._paths.get(row.state_id)) for row in ranked)

    def states(self) -> tuple[HistoricalState, ...]:
        return tuple(self._states.values())
