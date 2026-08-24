"""Complete deterministic chart-intelligence analysis over one frozen snapshot."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from hashlib import sha256

from ..contracts import MarketState, TradeDecision
from ..features import build_fingerprint
from ..market import MarketSnapshot
from .advanced import (
    breakout_quality,
    continuation_observation,
    mean_reversion_observation,
    opening_range,
    regime_probability,
    reversal_observation,
    vwap_deviation,
)
from .atr import atr_intelligence_observation
from .volatility_regime import volatility_regime_probability
from .volatility_dynamics import (
    compression_observation,
    expansion_observation,
    volatility_of_volatility_observation,
)
from .chart import build_chart_pack
from .evidence import build_timeframe_evidence
from .liquidity import session_liquidity
from .microstructure import measure_microstructure
from .model import IntelligenceReport
from .renderer import render_svg
from .spread_intelligence import SpreadProfileBook
from .strategies import default_knowledge
from .knowledge import KnowledgeLibrary
from .market_features import (
    SessionRange,
    candle_vwap,
    tick_observation,
)
from .session import session_intervals, session_observation
from .session_profiles import SessionProfileLibrary
from .london_sweeps import LondonSweepLibrary
from .ny_transitions import NYTransitionLibrary
from .opening_ranges import OpeningRangeLibrary
from .opening_range_breakouts import OpeningRangeBreakoutLibrary
from .false_opening_range_breakouts import FalseOpeningRangeBreakoutLibrary
from .session_vwap import SessionVWAPLibrary, build_session_vwap_library
from .vwap_deviation_stats import VWAPDeviationLibrary
from .vwap_reclaim_rejection import VWAPReclaimRejectionLibrary
from .tick_volume import TickVolumeLibrary, build_tick_volume_library
from .relative_volume import RelativeVolumeLibrary, build_relative_volume_library
from .trend_engine import TrendEngineLibrary, build_trend_engine_library
from .trend_persistence import TrendPersistenceLibrary, build_trend_persistence_library
from .mean_reversion_engine import MeanReversionLibrary, build_mean_reversion_library
from .breakout_engine import BreakoutEngineLibrary, build_breakout_engine_library
from .breakout_quality_engine import BreakoutQualityLibrary, build_breakout_quality_library
from .failed_breakout import FailedBreakoutLibrary, build_failed_breakout_library
from .reversal_engine import ReversalLibrary, build_reversal_library
from .continuation_engine import ContinuationLibrary, build_continuation_library
from .cross_asset import CrossAssetLibrary, build_cross_asset_library
from .market_regime_engine import MarketRegimeLibrary, build_market_regime_library
from .regime_probability_engine import RegimeProbabilityLibrary, build_regime_probability_library
from .router import StrategyRouter, build_strategy_router
from .pattern_outcomes import PatternOutcomeLibrary, build_pattern_outcome_library
from .market_features import VWAPObservation
from ..snapshot import snapshot_state_id


class IntelligenceEngine:
    """Produces evidence; it never submits orders or applies risk decisions."""

    def __init__(
        self,
        spread_profiles: SpreadProfileBook | None = None,
        session_profiles: SessionProfileLibrary | None = None,
        london_sweeps: LondonSweepLibrary | None = None,
        ny_transitions: NYTransitionLibrary | None = None,
        opening_range_library: OpeningRangeLibrary | None = None,
        opening_range_breakouts: OpeningRangeBreakoutLibrary | None = None,
        false_opening_range_breakouts: FalseOpeningRangeBreakoutLibrary | None = None,
        session_vwap_library: SessionVWAPLibrary | None = None,
        vwap_deviation_library: VWAPDeviationLibrary | None = None,
        vwap_reclaim_rejection_library: VWAPReclaimRejectionLibrary | None = None,
        tick_volume_library: TickVolumeLibrary | None = None,
        relative_volume_library: RelativeVolumeLibrary | None = None,
        trend_engine_library: TrendEngineLibrary | None = None,
        trend_persistence_library: TrendPersistenceLibrary | None = None,
        mean_reversion_library: MeanReversionLibrary | None = None,
        breakout_engine_library: BreakoutEngineLibrary | None = None,
        breakout_quality_library: BreakoutQualityLibrary | None = None,
        failed_breakout_library: FailedBreakoutLibrary | None = None,
        reversal_library: ReversalLibrary | None = None,
        continuation_library: ContinuationLibrary | None = None,
        cross_asset_library: CrossAssetLibrary | None = None,
        market_regime_library: MarketRegimeLibrary | None = None,
        regime_probability_library: RegimeProbabilityLibrary | None = None,
        strategy_router: StrategyRouter | None = None,
        pattern_outcome_library: PatternOutcomeLibrary | None = None,
    ):
        self._spread_profiles = spread_profiles or SpreadProfileBook(())
        self._session_profiles = session_profiles
        self._london_sweeps = london_sweeps
        self._ny_transitions = ny_transitions
        self._opening_range_library = opening_range_library
        self._opening_range_breakouts = opening_range_breakouts
        self._false_opening_range_breakouts = false_opening_range_breakouts
        self._session_vwap_library = session_vwap_library
        self._vwap_deviation_library = vwap_deviation_library
        self._vwap_reclaim_rejection_library = vwap_reclaim_rejection_library
        self._tick_volume_library = tick_volume_library
        self._relative_volume_library = relative_volume_library
        self._trend_engine_library = trend_engine_library
        self._trend_persistence_library = trend_persistence_library
        self._mean_reversion_library = mean_reversion_library
        self._breakout_engine_library = breakout_engine_library
        self._breakout_quality_library = breakout_quality_library
        self._failed_breakout_library = failed_breakout_library
        self._reversal_library = reversal_library
        self._continuation_library = continuation_library
        self._cross_asset_library = cross_asset_library
        self._market_regime_library = market_regime_library
        self._regime_probability_library = regime_probability_library
        self._strategy_router = strategy_router
        self._pattern_outcome_library = pattern_outcome_library

    def analyse(
        self,
        snapshot: MarketSnapshot,
        *,
        cross_asset_context: Mapping[str, float] | None = None,
    ) -> IntelligenceReport:
        timeframe_evidence = build_timeframe_evidence(snapshot.candles)
        structures = timeframe_evidence.structure
        zones = timeframe_evidence.zones
        liquidity = timeframe_evidence.liquidity
        patterns = timeframe_evidence.patterns
        regimes = timeframe_evidence.regimes
        fair_value_gaps = timeframe_evidence.fair_value_gaps
        displacement = timeframe_evidence.displacement
        volatility = timeframe_evidence.volatility
        realized_volatility = timeframe_evidence.realized_volatility
        geometric = timeframe_evidence.geometric_patterns
        qualities = timeframe_evidence.zone_quality
        sweeps = timeframe_evidence.liquidity_sweeps
        momentum = timeframe_evidence.momentum
        trend = timeframe_evidence.trend
        activity = timeframe_evidence.relative_activity
        ticks = {"ALL": tick_observation(snapshot.ticks)}
        microstructure = measure_microstructure(snapshot.ticks)
        quote_time = snapshot.ticks[-1].timestamp if snapshot.ticks else snapshot.observed_at
        current_session_context = session_observation(quote_time)
        current_session = current_session_context.primary_session
        symbol_session_profiles = (
            self._session_profiles.profiles_for_symbol(snapshot.symbol)
            if self._session_profiles is not None
            else {}
        )
        symbol_asia_ranges = (
            self._session_profiles.observations_for_symbol_session(snapshot.symbol, "ASIA")
            if self._session_profiles is not None
            else ()
        )
        symbol_london_sweep_statistics = (
            self._london_sweeps.statistics.get(snapshot.symbol)
            if self._london_sweeps is not None
            else None
        )
        symbol_london_sweep_outcomes = (
            self._london_sweeps.outcomes_for_symbol(snapshot.symbol)
            if self._london_sweeps is not None
            else ()
        )
        symbol_ny_transition_statistics = (
            self._ny_transitions.statistics.get(snapshot.symbol)
            if self._ny_transitions is not None
            else None
        )
        symbol_ny_transition_outcomes = (
            self._ny_transitions.outcomes_for_symbol(snapshot.symbol)
            if self._ny_transitions is not None
            else ()
        )
        symbol_opening_range_history = (
            self._opening_range_library.records_for_symbol(snapshot.symbol)
            if self._opening_range_library is not None
            else ()
        )
        symbol_opening_range_breakout_statistics = (
            self._opening_range_breakouts.statistics_for_symbol(snapshot.symbol)
            if self._opening_range_breakouts is not None
            else {}
        )
        symbol_opening_range_breakout_outcomes = (
            self._opening_range_breakouts.outcomes_for_symbol(snapshot.symbol)
            if self._opening_range_breakouts is not None
            else ()
        )
        symbol_false_opening_range_breakout_statistics = (
            self._false_opening_range_breakouts.statistics_for_symbol(snapshot.symbol)
            if self._false_opening_range_breakouts is not None
            else {}
        )
        symbol_false_opening_range_breakout_outcomes = (
            self._false_opening_range_breakouts.outcomes_for_symbol(snapshot.symbol)
            if self._false_opening_range_breakouts is not None
            else ()
        )
        symbol_session_vwap_history = (
            self._session_vwap_library.observations_for_symbol(snapshot.symbol)
            if self._session_vwap_library is not None
            else ()
        )
        current_session_vwap = (
            self._session_vwap_library.current_for_symbol(snapshot.symbol, quote_time)
            if self._session_vwap_library is not None
            else None
        )
        current_vwap_deviation_context = (
            self._vwap_deviation_library.current_for_symbol(snapshot.symbol, quote_time)
            if self._vwap_deviation_library is not None
            else None
        )
        symbol_vwap_reclaim_rejection_statistics = (
            self._vwap_reclaim_rejection_library.statistics_for_symbol(snapshot.symbol)
            if self._vwap_reclaim_rejection_library is not None
            else {}
        )
        symbol_vwap_reclaim_rejection_outcomes = (
            self._vwap_reclaim_rejection_library.outcomes_for_symbol(snapshot.symbol)
            if self._vwap_reclaim_rejection_library is not None
            else ()
        )
        tick_volume_library = self._tick_volume_library or build_tick_volume_library(
            {snapshot.symbol: snapshot.candles}, observed_at=quote_time
        )
        symbol_tick_volume_context = tick_volume_library.observations_for_symbol(snapshot.symbol)
        relative_volume_library = self._relative_volume_library or build_relative_volume_library(
            {snapshot.symbol: snapshot.candles.get("M1", ())},
            observed_at=quote_time,
            session_vwap_library=(
                self._session_vwap_library
                or build_session_vwap_library(
                    {snapshot.symbol: snapshot.candles.get("M1", ())}, observed_at=quote_time
                )
            ),
        )
        symbol_relative_volume_context = relative_volume_library.observations_for_symbol(snapshot.symbol)
        trend_engine_library = self._trend_engine_library or build_trend_engine_library(
            {snapshot.symbol: snapshot.candles}, observed_at=quote_time
        )
        symbol_trend_engine_context = trend_engine_library.observations_for_symbol(snapshot.symbol)
        trend_persistence_library = self._trend_persistence_library or build_trend_persistence_library(
            {snapshot.symbol: snapshot.candles}, observed_at=quote_time
        )
        symbol_trend_persistence_context = trend_persistence_library.observations_for_symbol(snapshot.symbol)
        mean_reversion_library = self._mean_reversion_library or build_mean_reversion_library(
            {snapshot.symbol: snapshot.candles}, observed_at=quote_time
        )
        symbol_mean_reversion_context = mean_reversion_library.observations_for_symbol(snapshot.symbol)
        breakout_engine_library = self._breakout_engine_library or build_breakout_engine_library(
            {snapshot.symbol: snapshot.candles}, observed_at=quote_time
        )
        symbol_breakout_engine_context = breakout_engine_library.observations_for_symbol(snapshot.symbol)
        breakout_quality_library = self._breakout_quality_library or build_breakout_quality_library(
            {snapshot.symbol: snapshot.candles}, observed_at=quote_time
        )
        symbol_breakout_quality_context = breakout_quality_library.observations_for_symbol(snapshot.symbol)
        failed_breakout_library = self._failed_breakout_library or build_failed_breakout_library(
            {snapshot.symbol: snapshot.candles}, observed_at=quote_time
        )
        symbol_failed_breakout_context = failed_breakout_library.observations_for_symbol(snapshot.symbol)
        reversal_library = self._reversal_library or build_reversal_library(
            {snapshot.symbol: snapshot.candles}, observed_at=quote_time
        )
        symbol_reversal_context = reversal_library.observations_for_symbol(snapshot.symbol)
        continuation_library = self._continuation_library or build_continuation_library(
            {snapshot.symbol: snapshot.candles}, observed_at=quote_time
        )
        symbol_continuation_context = continuation_library.observations_for_symbol(snapshot.symbol)
        cross_asset_library = self._cross_asset_library or build_cross_asset_library(
            {snapshot.symbol: snapshot.candles}, observed_at=quote_time
        )
        symbol_cross_asset_context = cross_asset_library.observations_for_symbol(snapshot.symbol)
        symbol_correlation_context = cross_asset_library.correlations_for_symbol(snapshot.symbol)
        symbol_correlation_breakdown_context = cross_asset_library.breakdowns_for_symbol(snapshot.symbol)
        market_regime_library = self._market_regime_library or build_market_regime_library(
            {snapshot.symbol: snapshot.candles}, observed_at=quote_time
        )
        symbol_market_regime_context = market_regime_library.observations_for_symbol(snapshot.symbol)
        regime_probability_library = self._regime_probability_library or build_regime_probability_library(
            {snapshot.symbol: snapshot.candles}, observed_at=quote_time
        )
        symbol_regime_probability_context = regime_probability_library.observations_for_symbol(snapshot.symbol)
        strategy_router = self._strategy_router or build_strategy_router()
        current_market_regime = symbol_market_regime_context.get("M1")
        routing_regime = (
            current_market_regime.label
            if current_market_regime is not None
            else regimes.get("M1").label
            if regimes.get("M1") is not None
            else "UNKNOWN"
        )
        strategy_route = strategy_router.candidates(routing_regime, current_session)
        pattern_outcome_library = self._pattern_outcome_library or build_pattern_outcome_library(
            {snapshot.symbol: snapshot.candles}, observed_at=quote_time
        )
        symbol_pattern_outcomes = {
            item.outcome_id: item
            for item in pattern_outcome_library.records_for_symbol(snapshot.symbol)
        }
        symbol_failed_patterns = {
            item.outcome_id: item
            for item in pattern_outcome_library.failed_for_symbol(snapshot.symbol)
        }
        atr_intelligence = {
            key: atr_intelligence_observation(
                rows,
                symbol=snapshot.symbol,
                timeframe=key,
                regime=regimes[key].label,
            )
            for key, rows in snapshot.candles.items()
        }
        volatility_regime_probabilities = {
            key: volatility_regime_probability(
                rows,
                symbol=snapshot.symbol,
                timeframe=key,
            )
            for key, rows in snapshot.candles.items()
        }
        volatility_of_volatility = {
            key: volatility_of_volatility_observation(
                rows,
                symbol=snapshot.symbol,
                timeframe=key,
            )
            for key, rows in snapshot.candles.items()
        }
        compression = {
            key: compression_observation(
                rows,
                symbol=snapshot.symbol,
                timeframe=key,
            )
            for key, rows in snapshot.candles.items()
        }
        expansion = {
            key: expansion_observation(
                rows,
                symbol=snapshot.symbol,
                timeframe=key,
            )
            for key, rows in snapshot.candles.items()
        }
        current_regime = regimes.get("M1")
        spreads_observed = self._spread_profiles.observe(
            symbol=snapshot.symbol,
            observed_at=quote_time,
            session=current_session,
            regime=current_regime.label if current_regime is not None else "UNKNOWN",
            current_spread=microstructure.latest_spread,
        )
        session_source = snapshot.candles.get("M1", ())
        vwap = (
            VWAPObservation(
                current_session_vwap.value,
                current_session_vwap.last_price,
                current_session_vwap.deviation,
                current_session_vwap.candle_count,
                current_session_vwap.source,
            )
            if current_session_vwap is not None
            else candle_vwap(session_source)
        )
        vwap_deviation_observed = vwap_deviation(
            vwap.last_price,
            vwap.value,
            tuple(item.close for item in session_source[-50:]),
        )
        base_intervals = session_intervals(snapshot.observed_at)
        session_ranges = {
            interval.session_id: _session_range_between(
                session_source,
                interval.session_id,
                interval.start_utc,
                interval.end_utc,
            )
            for interval in base_intervals
        }
        london = session_ranges["LONDON"]
        new_york = session_ranges["NEW_YORK"]
        overlap_start = max(london.start, new_york.start)
        overlap_end = min(london.end, new_york.end)
        session_ranges["LONDON_NY_OVERLAP"] = _session_range_between(
            session_source,
            "LONDON_NY_OVERLAP",
            overlap_start,
            overlap_end,
        )
        if session_source:
            liquidity = dict(liquidity)
            liquidity["M1"] = tuple(
                (*liquidity.get("M1", ()), *session_liquidity(session_ranges, session_source[-1].close))
            )
        opening_ranges = {
            key: opening_range(
                tuple(row for row in session_source if value.start <= row.start < value.end),
                key,
                session_start=value.start,
                observed_at=snapshot.observed_at,
            )
            for key, value in session_ranges.items()
        }
        chart = build_chart_pack(
            snapshot,
            structures,
            zones,
            liquidity,
            fair_value_gaps,
            vwap,
            session_ranges,
        )
        probability = regime_probability(tuple(item.label for item in regimes.values()))
        breakouts = {}
        reversals = {}
        continuations = {}
        mean_reversion = {}
        for key, rows in snapshot.candles.items():
            reference_levels = tuple(
                item.price
                for item in liquidity[key]
                if item.kind in ("PREVIOUS_HIGH", "PREVIOUS_LOW")
            )
            breakouts[key] = tuple(
                breakout_quality(rows, level, activity[key].ratio)
                for level in reference_levels
            )
            reversals[key] = tuple(
                reversal_observation(
                    True,
                    any(pattern.index == sweep.index and pattern.direction != "NEUTRAL" for pattern in patterns[key]),
                    sweep.reclaimed,
                    "DOWN" if sweep.direction == "UP" else "UP",
                )
                for sweep in sweeps[key]
            )
            displacement_direction = displacement[key].direction
            continuations[key] = continuation_observation(
                rows,
                displacement_direction,
                _pullback_depth(rows, displacement_direction),
            )
            vol = volatility[key]
            current = rows[-1].close if rows else 0.0
            reference = sum(item.close for item in rows[-20:]) / len(rows[-20:]) if rows else 0.0
            mean_reversion[key] = mean_reversion_observation(current, reference, vol.atr)
        frameworks = tuple(item.strategy_id for item in default_knowledge())
        knowledge_records = KnowledgeLibrary.complete_vocabulary().all()
        chart_hashes = {
            key: sha256(
                render_svg(
                    value,
                    annotations=chart.annotations.get(key, ()),
                ).encode("utf-8")
            ).hexdigest()
            for key, value in snapshot.candles.items()
            if value
        }
        fingerprint = build_fingerprint(snapshot.ticks, snapshot.candles.get("M1", ())) if snapshot.ticks else None
        state_id = snapshot_state_id(snapshot)
        cross_context = dict(cross_asset_context or {})
        cross_available = bool(cross_context.get("available", 0.0))
        return IntelligenceReport(
            state_id=state_id,
            symbol=snapshot.symbol,
            observed_at=snapshot.observed_at.isoformat(),
            structure=structures,
            zones=zones,
            liquidity=liquidity,
            patterns=patterns,
            regimes=regimes,
            chart=chart,
            fair_value_gaps=fair_value_gaps,
            displacement=displacement,
            tick_observations=ticks,
            volatility=volatility,
            realized_volatility=realized_volatility,
            atr_intelligence=atr_intelligence,
            volatility_regime_probability=volatility_regime_probabilities,
            volatility_of_volatility=volatility_of_volatility,
            compression=compression,
            expansion=expansion,
            session_context=current_session_context,
            session_profiles=symbol_session_profiles,
            asia_ranges=symbol_asia_ranges,
            london_sweep_statistics=symbol_london_sweep_statistics,
            london_sweep_outcomes=symbol_london_sweep_outcomes,
            ny_transition_statistics=symbol_ny_transition_statistics,
            ny_transition_outcomes=symbol_ny_transition_outcomes,
            opening_range_history=symbol_opening_range_history,
            opening_range_breakout_statistics=symbol_opening_range_breakout_statistics,
            opening_range_breakout_outcomes=symbol_opening_range_breakout_outcomes,
            false_opening_range_breakout_statistics=symbol_false_opening_range_breakout_statistics,
            false_opening_range_breakout_outcomes=symbol_false_opening_range_breakout_outcomes,
            session_vwap_history=symbol_session_vwap_history,
            current_session_vwap=current_session_vwap,
            geometric_patterns=geometric,
            zone_quality=qualities,
            liquidity_sweeps=sweeps,
            momentum=momentum,
            relative_activity=activity,
            microstructure=microstructure,
            spread_context=spreads_observed,
            vwap=vwap,
            vwap_deviation=vwap_deviation_observed,
            vwap_deviation_context=current_vwap_deviation_context,
            vwap_reclaim_rejection_statistics=symbol_vwap_reclaim_rejection_statistics,
            vwap_reclaim_rejection_outcomes=symbol_vwap_reclaim_rejection_outcomes,
            tick_volume_context=symbol_tick_volume_context,
            relative_volume_context=symbol_relative_volume_context,
            trend_engine_context=symbol_trend_engine_context,
            trend_persistence_context=symbol_trend_persistence_context,
            mean_reversion_context=symbol_mean_reversion_context,
            breakout_engine_context=symbol_breakout_engine_context,
            breakout_quality_context=symbol_breakout_quality_context,
            failed_breakout_context=symbol_failed_breakout_context,
            reversal_engine_context=symbol_reversal_context,
            continuation_engine_context=symbol_continuation_context,
            cross_asset_context=symbol_cross_asset_context,
            correlation_context=symbol_correlation_context,
            correlation_breakdown_context=symbol_correlation_breakdown_context,
            market_regime_context=symbol_market_regime_context,
            regime_probability_context=symbol_regime_probability_context,
            strategy_routing_context=strategy_route,
            pattern_outcome_context=symbol_pattern_outcomes,
            failed_pattern_context=symbol_failed_patterns,
            trend=trend,
            breakouts=breakouts,
            mean_reversion=mean_reversion,
            reversals=reversals,
            continuations=continuations,
            session_ranges=session_ranges,
            opening_ranges=opening_ranges,
            regime_probability=probability,
            framework_hypotheses=frameworks,
            knowledge_records=knowledge_records,
            chart_hashes=chart_hashes,
            frame_status=dict(snapshot.frame_status),
            source_ids=tuple(snapshot.source_ids),
            evidence=(
                "COMPLETED_CANDLES", "RAW_TICKS", "STRUCTURE", "SWING_HIERARCHY", "ZONES",
                "ZONE_LIFECYCLE", "ZONE_QUALITY", "LIQUIDITY", "LIQUIDITY_SWEEPS", "REGIME",
                "REGIME_PROBABILITY", "FVG", "FVG_LIFECYCLE", "DISPLACEMENT",
                f"MICROSTRUCTURE_{microstructure.status}",
                f"SPREAD_CONTEXT_{spreads_observed.status}",
                "REALIZED_VOLATILITY_CLOSE_TO_CLOSE_NO_ATR",
                "ATR_CONTEXT",
                "ATR_SYMBOL_TIMEFRAME_SESSION_REGIME_NORMALIZATION",
                "VOLATILITY_REGIME_EMPIRICAL_PROBABILITIES",
                "VOLATILITY_OF_VOLATILITY_REALIZED_CHANGE",
                "COMPRESSION_RANGE_AND_VOLATILITY_CONTRACTION",
                "EXPANSION_AFTER_COMPRESSION_STATISTICS",
                "SESSION_CANONICAL_UTC_IANA_EXCHANGE_CLOCKS",
                f"SESSION_PROFILE_LIBRARY_{'OBSERVED' if symbol_session_profiles else 'UNAVAILABLE'}",
                f"ASIA_RANGE_HISTORY_{'OBSERVED' if symbol_asia_ranges else 'UNAVAILABLE'}",
                f"LONDON_ASIA_SWEEP_STATISTICS_{'OBSERVED' if symbol_london_sweep_statistics is not None else 'UNAVAILABLE'}",
                f"NY_TRANSITION_STATISTICS_{'OBSERVED' if symbol_ny_transition_statistics is not None else 'UNAVAILABLE'}",
                f"OPENING_RANGE_HISTORY_{'OBSERVED' if symbol_opening_range_history else 'UNAVAILABLE'}",
                f"OPENING_RANGE_BREAKOUT_STATISTICS_{'OBSERVED' if symbol_opening_range_breakout_statistics else 'UNAVAILABLE'}",
                f"FALSE_OPENING_RANGE_BREAKOUT_STATISTICS_{'OBSERVED' if symbol_false_opening_range_breakout_statistics else 'UNAVAILABLE'}",
                f"SESSION_VWAP_{'OBSERVED' if current_session_vwap is not None else 'UNAVAILABLE'}",
                f"VWAP_DEVIATION_STATISTICS_{'OBSERVED' if current_vwap_deviation_context is not None else 'UNAVAILABLE'}",
                f"VWAP_RECLAIM_REJECTION_STATISTICS_{'OBSERVED' if symbol_vwap_reclaim_rejection_statistics else 'UNAVAILABLE'}",
                f"TICK_VOLUME_ACTIVITY_{'OBSERVED' if symbol_tick_volume_context else 'UNAVAILABLE'}",
                f"RELATIVE_VOLUME_{'OBSERVED' if symbol_relative_volume_context else 'UNAVAILABLE'}",
                f"TREND_ENGINE_{'OBSERVED' if symbol_trend_engine_context else 'UNAVAILABLE'}",
                f"TREND_PERSISTENCE_{'OBSERVED' if symbol_trend_persistence_context else 'UNAVAILABLE'}",
                f"MEAN_REVERSION_ENGINE_{'OBSERVED' if symbol_mean_reversion_context else 'UNAVAILABLE'}",
                f"BREAKOUT_ENGINE_{'OBSERVED' if symbol_breakout_engine_context else 'UNAVAILABLE'}",
                f"BREAKOUT_QUALITY_{'OBSERVED' if symbol_breakout_quality_context else 'UNAVAILABLE'}",
                f"FAILED_BREAKOUT_LIBRARY_{'OBSERVED' if symbol_failed_breakout_context else 'UNAVAILABLE'}",
                f"REVERSAL_ENGINE_{'OBSERVED' if symbol_reversal_context else 'UNAVAILABLE'}",
                f"CONTINUATION_ENGINE_{'OBSERVED' if symbol_continuation_context else 'UNAVAILABLE'}",
                f"CROSS_ASSET_DATA_{'OBSERVED' if symbol_cross_asset_context else 'UNAVAILABLE'}",
                f"CORRELATION_ENGINE_{'OBSERVED' if symbol_correlation_context else 'UNAVAILABLE'}",
                f"CORRELATION_BREAKDOWN_{'OBSERVED' if symbol_correlation_breakdown_context else 'UNAVAILABLE'}",
                f"MARKET_REGIME_ENGINE_{'OBSERVED' if symbol_market_regime_context else 'UNAVAILABLE'}",
                f"REGIME_PROBABILITY_ENGINE_{'OBSERVED' if symbol_regime_probability_context else 'UNAVAILABLE'}",
                f"STRATEGY_ROUTER_{'OBSERVED' if strategy_route.candidate_ids else 'UNAVAILABLE'}",
                f"PATTERN_OUTCOMES_{'OBSERVED' if symbol_pattern_outcomes else 'UNAVAILABLE'}",
                f"FAILED_PATTERN_LIBRARY_{'OBSERVED' if symbol_failed_patterns else 'UNAVAILABLE'}",
                "VOLATILITY_OF_VOLATILITY",
                "MOMENTUM", "TREND", "BREAKOUT_QUALITY", "MEAN_REVERSION", "REVERSAL",
                "CONTINUATION", "RELATIVE_ACTIVITY", "SESSION_RANGES", "OPENING_RANGES", "VWAP",
                "VWAP_DEVIATION",
                "CANDLE_PATTERNS", "GEOMETRIC_PATTERNS", "FRAMEWORK_HYPOTHESES", "MARKET_KNOWLEDGE", "CHART_HASHES",
                f"CROSS_ASSET_{'OBSERVED' if cross_available else 'UNAVAILABLE'}",
                "SOURCE_PROVENANCE",
            ),
            metadata={
                "timeframes": tuple(snapshot.candles),
                "tick_observation": "ALL",
                "session": current_session,
                "session_canonical_utc": current_session_context.canonical_utc,
                "session_active": current_session_context.active_sessions,
                "fingerprint": asdict(fingerprint) if fingerprint is not None else None,
                "microstructure_status": microstructure.status,
                "microstructure_tick_count": microstructure.tick_count,
                "cross_asset_context": cross_context,
                "strategy_routing": asdict(strategy_route),
                "strategy_routing_role": "DESCRIPTIVE_RESEARCH_NOT_TRADE_APPROVAL",
                "pattern_outcome_role": "PATTERN_OUTCOMES_RESEARCH_NOT_TRADE_APPROVAL",
            },
        )

    def decide(self, state: MarketState) -> TradeDecision:
        """The decision adapter remains explicit; analysis is not hidden here."""

        raise RuntimeError("A validated decision provider must be injected; analysis does not place orders")


def _pullback_depth(rows: tuple[object, ...], direction: str) -> float:
    if len(rows) < 2 or direction not in ("UP", "DOWN"):
        return 0.0
    prior, current = rows[-2:]
    prior_range = max(prior.high - prior.low, 1e-12)
    if direction == "UP":
        return max(0.0, prior.high - current.close) / prior_range
    return max(0.0, current.close - prior.low) / prior_range


def _session_range_between(rows: tuple[object, ...], name: str, start: object, end: object):
    selected = tuple(item for item in rows if start <= item.start < end)
    high = max((item.high for item in selected), default=None)
    low = min((item.low for item in selected), default=None)
    midpoint = (high + low) / 2 if high is not None and low is not None else None
    return SessionRange(name, start, end, high, low, midpoint, high - low if high is not None and low is not None else None)
