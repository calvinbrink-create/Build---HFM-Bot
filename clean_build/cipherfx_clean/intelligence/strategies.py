"""Machine-readable research vocabulary.

Entries describe hypotheses and observable conditions only.  The library does
not select a strategy, add a veto, or place an order.  A research experiment
must supply the outcomes that determine whether an entry is useful.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class StrategyHypothesis:
    strategy_id: str
    family: Literal["PRICE_ACTION", "SMC", "WYCKOFF", "BREAKOUT", "MEAN_REVERSION", "MOMENTUM", "PATTERN", "LIQUIDITY", "SESSION", "VOLATILITY"]
    conditions: tuple[str, ...]
    entry_observation: str
    invalidation_observation: str
    outcome_horizon_seconds: tuple[int, ...]
    version: int = 1
    status: Literal["CANDIDATE", "HISTORICAL_VALIDATED", "SHADOW", "APPROVED", "DEGRADED", "REJECTED"] = "CANDIDATE"
    directions: tuple[Literal["BUY", "SELL", "NO_TRADE"], ...] = ("BUY", "SELL", "NO_TRADE")
    required_observations: tuple[str, ...] = ()


@dataclass(frozen=True)
class PatternDefinition:
    pattern_id: str
    family: str
    geometry: tuple[str, ...]
    bullish_context: str
    bearish_context: str
    failure_condition: str


PRICE_ACTION = (
    "candle_body_wick_control", "rejection_at_reference", "engulfing_shift",
    "inside_bar_compression", "outside_bar_expansion", "break_retest",
)

SMC = (
    "liquidity_high_low", "bos", "choch", "fvg", "order_block",
    "displacement", "premium_discount", "sweep_reclaim",
)

WYCKOFF = (
    "selling_climax", "automatic_rally", "secondary_test", "spring",
    "upthrust", "sign_of_strength", "last_point_of_support",
)

BREAKOUT = (
    "opening_range_break", "session_range_break", "volatility_expansion",
    "breakout_retest", "failed_breakout_reclaim",
)

MEAN_REVERSION = (
    "distance_from_vwap", "range_extreme", "liquidity_rejection",
    "volatility_normalization", "return_to_value",
)

MOMENTUM = (
    "directional_persistence", "impulse_expansion", "relative_activity",
    "pullback_continuation", "trend_slope",
)


def default_knowledge() -> tuple[StrategyHypothesis, ...]:
    return (
        StrategyHypothesis("price_action_rejection", "PRICE_ACTION", PRICE_ACTION, "next executable quote after rejection", "rejection extreme breached", (5, 15, 60, 300), required_observations=("completed_candle", "reference_level")),
        StrategyHypothesis("smc_sweep_reclaim", "SMC", SMC, "reclaim after liquidity sweep and displacement", "sweep extreme accepted", (15, 60, 300, 900), required_observations=("liquidity_level", "sweep", "reclaim")),
        StrategyHypothesis("wyckoff_spring", "WYCKOFF", WYCKOFF, "spring or upthrust confirmation", "range boundary accepted", (300, 900, 3600), required_observations=("range", "test", "volume_proxy")),
        StrategyHypothesis("session_breakout", "BREAKOUT", BREAKOUT, "closed crossing beyond session range", "breakout returns inside range", (60, 300, 900), required_observations=("session_range", "crossing", "costs")),
        StrategyHypothesis("value_reversion", "MEAN_REVERSION", MEAN_REVERSION, "rejection at statistically stretched value", "stretch continues", (60, 300, 900), required_observations=("vwap_deviation", "volatility_scale")),
        StrategyHypothesis("impulse_continuation", "MOMENTUM", MOMENTUM, "pullback followed by directional expansion", "directional persistence fails", (60, 300, 900), required_observations=("impulse", "pullback", "resumption")),
        StrategyHypothesis("liquidity_failure_continuation", "LIQUIDITY", ("sweep", "failed_reversal", "acceptance"), "acceptance beyond swept level", "reclaim through swept level", (60, 300, 900), required_observations=("sweep", "failed_pattern")),
        StrategyHypothesis("opening_range_outcome", "SESSION", ("opening_range", "break", "reclaim", "session"), "crossing or failed crossing", "session close", (300, 900, 3600), required_observations=("opening_range", "session_calendar")),
        StrategyHypothesis("compression_expansion", "VOLATILITY", ("compression", "expansion", "direction"), "range expansion after compression", "compression resumes", (60, 300, 900), required_observations=("realized_volatility", "volatility_of_volatility")),
        StrategyHypothesis("chart_geometry_outcome", "PATTERN", tuple(item.pattern_id for item in chart_patterns()), "confirmed geometry break", "pattern failure condition", (300, 900, 3600), required_observations=("geometry", "confirmation", "failure_condition")),
    )


def chart_patterns() -> tuple[PatternDefinition, ...]:
    return (
        PatternDefinition("double_top", "REVERSAL", ("two_peaks", "neckline", "similar_height"), "none", "neckline_close", "second_peak_breaks"),
        PatternDefinition("double_bottom", "REVERSAL", ("two_troughs", "neckline", "similar_depth"), "neckline_close", "none", "second_trough_breaks"),
        PatternDefinition("head_shoulders", "REVERSAL", ("left_shoulder", "head", "right_shoulder", "neckline"), "none", "neckline_close", "right_shoulder_breaks"),
        PatternDefinition("inverse_head_shoulders", "REVERSAL", ("left_trough", "head", "right_trough", "neckline"), "neckline_close", "none", "right_trough_breaks"),
        PatternDefinition("bull_flag", "CONTINUATION", ("impulse_up", "descending_channel", "breakout"), "channel_break", "none", "pole_origin_breaks"),
        PatternDefinition("bear_flag", "CONTINUATION", ("impulse_down", "ascending_channel", "breakdown"), "none", "channel_break", "pole_origin_breaks"),
        PatternDefinition("ascending_triangle", "BREAKOUT", ("flat_resistance", "rising_lows"), "resistance_close", "none", "rising_support_breaks"),
        PatternDefinition("descending_triangle", "BREAKOUT", ("flat_support", "falling_highs"), "none", "support_close", "falling_resistance_breaks"),
        PatternDefinition("rising_wedge", "REVERSAL", ("rising_highs", "rising_lows", "convergence"), "none", "lower_line_break", "upper_line_break"),
        PatternDefinition("falling_wedge", "REVERSAL", ("falling_highs", "falling_lows", "convergence"), "upper_line_break", "none", "lower_line_break"),
        PatternDefinition("channel", "CONTINUATION", ("parallel_highs", "parallel_lows"), "upper_boundary_break", "lower_boundary_break", "channel_failure"),
    )


def knowledge_by_family() -> dict[str, tuple[str, ...]]:
    return {
        "PRICE_ACTION": PRICE_ACTION,
        "SMC": SMC,
        "WYCKOFF": WYCKOFF,
        "BREAKOUT": BREAKOUT,
        "MEAN_REVERSION": MEAN_REVERSION,
        "MOMENTUM": MOMENTUM,
        "PATTERN": tuple(item.pattern_id for item in chart_patterns()),
    }
