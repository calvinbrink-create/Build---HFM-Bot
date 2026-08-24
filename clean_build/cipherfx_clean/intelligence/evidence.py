"""One canonical extractor for completed-candle market evidence.

Both live intelligence and historical memory consume this bundle.  Keeping the
detectors behind one function prevents the research fingerprint from drifting
away from the observations shown in live reports and charts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from ..contracts import Candle
from .advanced import momentum_observation, relative_activity, trend_observation, zone_quality
from .candles import identify_patterns
from .liquidity import identify_liquidity
from .market_features import (
    detect_fair_value_gaps,
    detect_liquidity_sweeps,
    measure_displacement,
    realized_volatility_observation,
    volatility_observation,
)
from .patterns import detect_geometric_patterns
from .regime import classify_regime
from .structure import analyse_structure
from .zones import identify_zones


@dataclass(frozen=True)
class TimeframeEvidence:
    structure: Mapping[str, object]
    zones: Mapping[str, tuple[object, ...]]
    liquidity: Mapping[str, tuple[object, ...]]
    patterns: Mapping[str, tuple[object, ...]]
    regimes: Mapping[str, object]
    fair_value_gaps: Mapping[str, tuple[object, ...]]
    displacement: Mapping[str, object]
    volatility: Mapping[str, object]
    geometric_patterns: Mapping[str, tuple[object, ...]]
    zone_quality: Mapping[str, tuple[object, ...]]
    liquidity_sweeps: Mapping[str, tuple[object, ...]]
    momentum: Mapping[str, object]
    trend: Mapping[str, object]
    relative_activity: Mapping[str, object]
    realized_volatility: Mapping[str, object]


def build_timeframe_evidence(candles: Mapping[str, tuple[Candle, ...]]) -> TimeframeEvidence:
    structures = {key: analyse_structure(value) for key, value in candles.items()}
    zones = {key: identify_zones(value) for key, value in candles.items()}
    liquidity = {key: identify_liquidity(value) for key, value in candles.items()}
    patterns = {key: identify_patterns(value) for key, value in candles.items()}
    regimes = {key: classify_regime(value) for key, value in candles.items()}
    fair_value_gaps = {key: detect_fair_value_gaps(value) for key, value in candles.items()}
    displacement = {key: measure_displacement(value) for key, value in candles.items()}
    volatility = {key: volatility_observation(value) for key, value in candles.items()}
    geometric = {key: detect_geometric_patterns(value) for key, value in candles.items()}
    qualities = {
        key: tuple(
            zone_quality(
                candles[key],
                item.low,
                item.high,
                item.origin_index,
                kind=item.kind,
                displacement_ratio=item.displacement,
                lifecycle=item.freshness,
                activation_index=item.origin_index + 1,
                invalidated_at=item.invalidated_at,
            )
            for item in rows
        )
        for key, rows in zones.items()
    }
    sweeps = {key: detect_liquidity_sweeps(value) for key, value in candles.items()}
    momentum = {key: momentum_observation(value) for key, value in candles.items()}
    trend = {key: trend_observation(value) for key, value in candles.items()}
    activity = {
        key: relative_activity(
            value[-1].tick_volume if value else 0.0,
            tuple(row.tick_volume for row in value[:-1]),
        )
        for key, value in candles.items()
    }
    realized_volatility = {
        key: realized_volatility_observation(value)
        for key, value in candles.items()
    }
    return TimeframeEvidence(
        structures,
        zones,
        liquidity,
        patterns,
        regimes,
        fair_value_gaps,
        displacement,
        volatility,
        geometric,
        qualities,
        sweeps,
        momentum,
        trend,
        activity,
        realized_volatility,
    )
