"""Typed outputs of the intelligence layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Mapping, Tuple

from ..market import Candle, RawTick
from .market_features import (
    Displacement,
    FairValueGap,
    RealizedVolatilityObservation,
    TickObservation,
    VolatilityObservation,
)
from .session import SessionObservation


@dataclass(frozen=True)
class SwingPoint:
    index: int
    price: float
    kind: Literal["HIGH", "LOW"]
    strength: float
    label: Literal["HH", "LH", "EH", "HL", "LL", "EL"] | None = None
    hierarchy: Literal["MAJOR", "MINOR"] = "MINOR"
    significance: float = 0.0


@dataclass(frozen=True)
class StructureState:
    direction: Literal["UP", "DOWN", "RANGE", "UNKNOWN"]
    swings: Tuple[SwingPoint, ...]
    break_of_structure: bool
    change_of_character: bool
    swing_sequence: Tuple[str, ...] = ()
    bos_direction: str | None = None
    choch_direction: str | None = None
    trend_strength: float = 0.0
    directional_persistence: float = 0.0


@dataclass(frozen=True)
class Zone:
    kind: Literal["SUPPLY", "DEMAND"]
    low: float
    high: float
    origin_index: int
    freshness: Literal["FRESH", "TESTED", "MITIGATED", "FAILED"]
    displacement: float
    touches: int = 0
    penetration_ratio: float = 0.0
    invalidated_at: int | None = None


@dataclass(frozen=True)
class LiquidityPool:
    kind: Literal["EQUAL_HIGH", "EQUAL_LOW", "SWING_HIGH", "SWING_LOW", "PREVIOUS_HIGH", "PREVIOUS_LOW", "SESSION_HIGH", "SESSION_LOW"]
    price: float
    distance: float
    indices: Tuple[int, ...] = ()
    observation_count: int = 1
    source: str = "STRUCTURE"
    session_id: str | None = None


@dataclass(frozen=True)
class CandlePattern:
    name: str
    direction: Literal["BULLISH", "BEARISH", "NEUTRAL"]
    index: int
    strength: float


@dataclass(frozen=True)
class RegimeState:
    label: Literal["TREND_UP", "TREND_DOWN", "RANGE", "EXPANSION", "COMPRESSION", "UNKNOWN"]
    volatility: float
    persistence: float


@dataclass(frozen=True)
class ChartPack:
    symbol: str
    observed_at: str
    timeframes: Mapping[str, Tuple[Candle, ...]]
    annotations: Mapping[str, tuple[Mapping[str, object], ...]]
    pack_id: str = ""
    source_ids: Tuple[str, ...] = ()


@dataclass(frozen=True)
class IntelligenceReport:
    state_id: str
    symbol: str
    observed_at: str
    structure: Mapping[str, StructureState]
    zones: Mapping[str, Tuple[Zone, ...]]
    liquidity: Mapping[str, Tuple[LiquidityPool, ...]]
    patterns: Mapping[str, Tuple[CandlePattern, ...]]
    regimes: Mapping[str, RegimeState]
    chart: ChartPack
    fair_value_gaps: Mapping[str, tuple[FairValueGap, ...]] = field(default_factory=dict)
    displacement: Mapping[str, Displacement] = field(default_factory=dict)
    tick_observations: Mapping[str, TickObservation] = field(default_factory=dict)
    volatility: Mapping[str, VolatilityObservation] = field(default_factory=dict)
    realized_volatility: Mapping[str, RealizedVolatilityObservation] = field(default_factory=dict)
    atr_intelligence: Mapping[str, object] = field(default_factory=dict)
    volatility_regime_probability: Mapping[str, object] = field(default_factory=dict)
    volatility_of_volatility: Mapping[str, object] = field(default_factory=dict)
    compression: Mapping[str, object] = field(default_factory=dict)
    expansion: Mapping[str, object] = field(default_factory=dict)
    session_context: SessionObservation | None = None
    session_profiles: Mapping[str, object] = field(default_factory=dict)
    asia_ranges: Tuple[object, ...] = ()
    london_sweep_statistics: object | None = None
    london_sweep_outcomes: Tuple[object, ...] = ()
    ny_transition_statistics: object | None = None
    ny_transition_outcomes: Tuple[object, ...] = ()
    opening_range_history: Tuple[object, ...] = ()
    opening_range_breakout_statistics: Mapping[str, object] = field(default_factory=dict)
    opening_range_breakout_outcomes: Tuple[object, ...] = ()
    false_opening_range_breakout_statistics: Mapping[str, object] = field(default_factory=dict)
    false_opening_range_breakout_outcomes: Tuple[object, ...] = ()
    session_vwap_history: Tuple[object, ...] = ()
    current_session_vwap: object | None = None
    geometric_patterns: Mapping[str, tuple[object, ...]] = field(default_factory=dict)
    zone_quality: Mapping[str, tuple[object, ...]] = field(default_factory=dict)
    liquidity_sweeps: Mapping[str, tuple[object, ...]] = field(default_factory=dict)
    momentum: Mapping[str, object] = field(default_factory=dict)
    relative_activity: Mapping[str, object] = field(default_factory=dict)
    microstructure: object | None = None
    spread_context: object | None = None
    vwap: object | None = None
    vwap_deviation: object | None = None
    vwap_deviation_context: object | None = None
    vwap_reclaim_rejection_statistics: Mapping[str, object] = field(default_factory=dict)
    vwap_reclaim_rejection_outcomes: Tuple[object, ...] = ()
    tick_volume_context: Mapping[str, object] = field(default_factory=dict)
    relative_volume_context: Mapping[str, object] = field(default_factory=dict)
    trend_engine_context: Mapping[str, object] = field(default_factory=dict)
    trend_persistence_context: Mapping[str, object] = field(default_factory=dict)
    mean_reversion_context: Mapping[str, object] = field(default_factory=dict)
    breakout_engine_context: Mapping[str, object] = field(default_factory=dict)
    breakout_quality_context: Mapping[str, object] = field(default_factory=dict)
    failed_breakout_context: Mapping[str, object] = field(default_factory=dict)
    reversal_engine_context: Mapping[str, object] = field(default_factory=dict)
    continuation_engine_context: Mapping[str, object] = field(default_factory=dict)
    cross_asset_context: Mapping[str, object] = field(default_factory=dict)
    correlation_context: Mapping[str, object] = field(default_factory=dict)
    correlation_breakdown_context: Mapping[str, object] = field(default_factory=dict)
    market_regime_context: Mapping[str, object] = field(default_factory=dict)
    regime_probability_context: Mapping[str, object] = field(default_factory=dict)
    strategy_routing_context: object | None = None
    pattern_outcome_context: Mapping[str, object] = field(default_factory=dict)
    failed_pattern_context: Mapping[str, object] = field(default_factory=dict)
    trend: Mapping[str, object] = field(default_factory=dict)
    breakouts: Mapping[str, tuple[object, ...]] = field(default_factory=dict)
    mean_reversion: Mapping[str, object] = field(default_factory=dict)
    reversals: Mapping[str, tuple[object, ...]] = field(default_factory=dict)
    continuations: Mapping[str, object] = field(default_factory=dict)
    session_ranges: Mapping[str, object] = field(default_factory=dict)
    opening_ranges: Mapping[str, object] = field(default_factory=dict)
    regime_probability: object | None = None
    framework_hypotheses: Tuple[str, ...] = ()
    knowledge_records: Tuple[object, ...] = ()
    chart_hashes: Mapping[str, str] = field(default_factory=dict)
    frame_status: Mapping[str, str] = field(default_factory=dict)
    source_ids: Tuple[str, ...] = ()
    evidence: Tuple[str, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)
