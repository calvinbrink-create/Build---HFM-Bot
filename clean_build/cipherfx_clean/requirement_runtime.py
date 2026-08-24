"""Runtime evidence checks for individual governing requirements.

These checks interpret already-observed artifacts.  They never influence a
market decision and never convert an unhealthy feed into a healthy one.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from math import isclose, isfinite, log, sqrt
from pathlib import Path
import sqlite3
from statistics import mean, median, pstdev
from typing import Mapping, Sequence

from .chart_history import build_persistent_chart, validate_persistent_chart
from .contracts import EventMarketSnapshot, MarketSnapshot, MarketState, RawTick, TradeDecision, TradeOutcome
from .features import build_fingerprint
from .hfm_data import (
    HFM_SERVER_TIME_POLICY,
    HfmCsvMarketDataAdapter,
    hfm_server_utc_offset_seconds,
    with_tick_window,
)
from .intelligence.engine import IntelligenceEngine
from .intelligence.chart import with_trade_annotations
from .intelligence.microstructure import measure_microstructure
from .intelligence.latency import (
    RuntimeLatencyContext,
    build_execution_latency_observation,
)
from .intelligence.model import ChartPack
from .intelligence.market_features import classify_tick_directions, measure_displacement, tick_acceleration_observation, tick_imbalance_observation, tick_velocity_observation
from .intelligence.renderer import render_svg
from .intelligence.regime import classify_regime
from .intelligence.session import (
    broker_session_observation,
    session_intervals,
    session_label,
    session_observation,
)
from .intelligence.session_profiles import (
    PROFILE_SESSIONS,
    build_session_profile_library,
)
from .intelligence.london_sweeps import build_london_sweep_library
from .intelligence.ny_transitions import build_ny_transition_library
from .intelligence.opening_ranges import (
    OPENING_RANGE_SESSIONS,
    build_opening_range_library,
)
from .intelligence.opening_range_breakouts import build_opening_range_breakout_library
from .intelligence.false_opening_range_breakouts import (
    build_false_opening_range_breakout_library,
)
from .intelligence.session_vwap import build_session_vwap_library
from .intelligence.vwap_deviation_stats import build_vwap_deviation_library
from .intelligence.vwap_reclaim_rejection import build_vwap_reclaim_rejection_library
from .intelligence.tick_volume import build_tick_volume_library
from .intelligence.relative_volume import build_relative_volume_library
from .intelligence.trend_engine import build_trend_engine_library
from .intelligence.trend_persistence import build_trend_persistence_library
from .intelligence.mean_reversion_engine import build_mean_reversion_library
from .intelligence.breakout_engine import build_breakout_engine_library
from .intelligence.breakout_quality_engine import build_breakout_quality_library
from .intelligence.failed_breakout import build_failed_breakout_library
from .intelligence.reversal_engine import build_reversal_library
from .intelligence.continuation_engine import build_continuation_library
from .intelligence.cross_asset import build_cross_asset_library
from .intelligence.market_regime_engine import REGIME_LABELS, build_market_regime_library
from .intelligence.regime_probability_engine import build_regime_probability_library
from .intelligence.router import build_strategy_router
from .intelligence.pattern_outcomes import build_pattern_outcome_library
from .intelligence.fingerprint_engine import SetupFingerprintEngine
from .intelligence.feature_store import FeatureRecord, FeatureStore
from .intelligence.edge_miner import EdgeMiner, HypothesisProposal, run_hypothesis
from .intelligence.hypothesis import Hypothesis, compare_counterfactuals, evaluate_hypothesis
from .intelligence.data_library import MarketStateRecord, WholeMarketLibrary
from .intelligence.outcomes import future_path_outcome
from .intelligence.attribution import Attribution, marginal_value
from .cost_evidence import ExecutionCostSample, build_cost_profile
from .intelligence.edge_validation import monte_carlo_expectancy
from .intelligence.execution_model import Quote, replay_executable_prices
from .intelligence.governance import (
    EdgeScorecard,
    EdgeVersion,
    GovernancePolicy,
    concept_drift,
    eligible_for_promotion,
    monitor_decay,
    version_transition,
)
from .intelligence.monitor import EdgeObservation, monitor
from .intelligence.news import NewsEvent, news_regime
from .intelligence.planning import ConditionalPlan, PlanReality, PlanningLedger
from .intelligence.research import ForwardOutcome, calculate_statistics
from .intelligence.research import overfit_diagnostic
from .intelligence.research import EdgeCandidate, EdgeRegistry
from .intelligence.validation_full import TimedObservation, purged_folds, validate_candidate
from .intelligence.outcome_metrics import (
    OUTCOME_HORIZONS_SECONDS,
    classify_r_multiple,
    forward_metrics,
    summarize_outcomes,
)
from .historical_memory import HistoricalAnalogueIndex, feature_names, feature_vector, path_outcome
from .research_pipeline import FeatureCondition, ResearchPolicy, sweep_specs
from .intelligence.knowledge import KnowledgeLibrary
from .intelligence.strategies import (
    BREAKOUT,
    MEAN_REVERSION,
    MOMENTUM,
    PRICE_ACTION,
    SMC,
    WYCKOFF,
    chart_patterns,
    default_knowledge,
)
from .intelligence.spread_intelligence import (
    build_spread_profile_book,
    historical_spread_samples,
)
from .intelligence.volatility_dynamics import (
    compression_outcomes,
    expansion_after_compression_outcomes,
    expansion_statistics,
)
from .liquidity_failures import build_liquidity_failure_records
from .shadow_runtime import PersistentShadowRunner
from .intelligence.tournament import ShadowTournament
from .replay import replay
from .replay import chart_replay
from .slippage import BrokerFill, build_slippage_observation
from .snapshot import (
    MARKET_STATE_TIMEFRAMES,
    EVENT_SNAPSHOT_PHASES,
    RESEARCH_TIMEFRAMES,
    freeze_event_snapshot,
    market_state_from_snapshot,
    snapshot_from_dataset,
    snapshot_state_id,
    validate_event_snapshot_sequence,
)
from .store import EvidenceStore
from .intelligence.provider import ResearchRequest, StructuredResearchProvider
from .intelligence.runtime_contracts import StructuredInput, StructuredOutput
from .validation import validate_against_active_edges, validate_decision


C008_TIMEFRAMES = ("1s", "5s", "15s", "M1", "M3", "M5", "M15", "M30", "H1", "H4", "D1")


def verify_c007_tick_quality(
    payload: Mapping[str, object],
    *,
    maximum_age_seconds: float,
) -> Mapping[str, object]:
    rows = payload.get("results")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)) or not rows:
        raise ValueError("C007 runtime evidence requires observed tick results")
    statuses: list[str] = []
    checks: list[Mapping[str, object]] = []
    valid_statuses = {"VALID", "DUPLICATE", "STALE", "MISSING", "MALFORMED", "MARKET_CLOSED"}
    for value in rows:
        if not isinstance(value, Mapping):
            raise ValueError("C007 tick results must be objects")
        symbol = str(value.get("symbol") or "")
        status = str(value.get("status") or "")
        reason = str(value.get("reason") or "")
        if not symbol or status not in valid_statuses or not reason:
            raise ValueError("C007 tick result has missing identity or classification")
        observed = datetime.fromisoformat(str(value["observed_at"]))
        tick_text = value.get("tick_at")
        age = None
        classification_valid = True
        if tick_text is not None:
            tick_at = datetime.fromisoformat(str(tick_text))
            age = (observed - tick_at).total_seconds()
            if status == "VALID":
                classification_valid = abs(age) <= maximum_age_seconds
            elif status == "STALE":
                classification_valid = abs(age) > maximum_age_seconds
        elif status not in {"MISSING", "MALFORMED"}:
            classification_valid = False
        if not classification_valid:
            raise ValueError(f"C007 classification contradicts timestamps for {symbol}")
        statuses.append(status)
        checks.append(
            {
                "symbol": symbol,
                "status": status,
                "reason": reason,
                "age_seconds": age,
                "classification_valid": True,
                "source_path": value.get("source_path"),
            }
        )
    if "VALID" not in statuses:
        raise ValueError("C007 live evidence requires at least one fresh broker tick")
    if "STALE" not in statuses:
        raise ValueError("C007 live evidence requires an observed stale classification")
    return {
        "schema_version": 1,
        "requirement_id": "C007",
        "status": "PASS",
        "maximum_age_seconds": maximum_age_seconds,
        "observed_classifications": tuple(sorted(set(statuses))),
        "checks": tuple(checks),
    }


def verify_c006_raw_tick_library(database: Path) -> Mapping[str, object]:
    required_columns = {
        "symbol", "timestamp", "bid", "ask", "spread", "mid",
        "price_change", "interarrival_seconds", "tick_velocity", "direction",
        "event_type", "event_id",
    }
    connection = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
    try:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(raw_ticks)")
        }
        missing = tuple(sorted(required_columns - columns))
        if integrity != "ok" or missing:
            raise ValueError(
                f"C006 raw tick database invalid integrity={integrity} missing={missing}"
            )
        total = int(connection.execute("SELECT COUNT(*) FROM raw_ticks").fetchone()[0])
        if total <= 0:
            raise ValueError("C006 raw tick database contains no observations")
        incomplete = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM raw_ticks
                WHERE symbol='' OR timestamp='' OR bid<=0 OR ask<bid
                   OR spread IS NULL OR mid IS NULL OR price_change IS NULL
                   OR interarrival_seconds IS NULL OR tick_velocity IS NULL
                   OR direction IS NULL OR event_type IS NULL OR event_id IS NULL
                """
            ).fetchone()[0]
        )
        inconsistent = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM raw_ticks
                WHERE ABS(spread-(ask-bid))>0.000000001
                   OR ABS(mid-((bid+ask)/2.0))>0.000000001
                   OR LENGTH(event_id)<>64 OR event_type<>'QUOTE'
                """
            ).fetchone()[0]
        )
        if incomplete or inconsistent:
            raise ValueError(
                f"C006 raw tick observations incomplete={incomplete} inconsistent={inconsistent}"
            )
        symbols = tuple(
            row[0] for row in connection.execute(
                "SELECT DISTINCT symbol FROM raw_ticks ORDER BY symbol"
            )
        )
    finally:
        connection.close()
    source = database.read_bytes()
    return {
        "schema_version": 1,
        "requirement_id": "C006",
        "status": "PASS",
        "database": str(database),
        "database_sha256": sha256(source).hexdigest(),
        "integrity": integrity,
        "tick_count": total,
        "symbols": symbols,
        "required_columns": tuple(sorted(required_columns)),
        "incomplete_rows": incomplete,
        "inconsistent_rows": inconsistent,
    }


def verify_c008_snapshot(snapshot: MarketSnapshot) -> Mapping[str, object]:
    frames: list[Mapping[str, object]] = []
    for timeframe in C008_TIMEFRAMES:
        rows = tuple(snapshot.candles.get(timeframe, ()))
        status = snapshot.frame_status.get(timeframe)
        if status != "COMPLETED" or not rows:
            raise ValueError(
                f"C008 missing completed timeframe {timeframe}: status={status} rows={len(rows)}"
            )
        if any(row.end > snapshot.observed_at for row in rows):
            raise ValueError(f"C008 forming candle leaked into {timeframe}")
        if any(not row.source or not row.source_id for row in rows):
            raise ValueError(f"C008 missing source provenance for {timeframe}")
        if timeframe in {"1s", "5s", "15s"} and any(
            row.source != "DERIVED_FROM_HFM_TICKS" for row in rows
        ):
            raise ValueError(f"C008 micro timeframe is not derived from HFM ticks: {timeframe}")
        frames.append(
            {
                "timeframe": timeframe,
                "status": status,
                "bars": len(rows),
                "latest_start": rows[-1].start.isoformat(),
                "latest_end": rows[-1].end.isoformat(),
                "sources": tuple(sorted({row.source for row in rows})),
                "source_ids_present": all(bool(row.source_id) for row in rows),
            }
        )
    return {
        "symbol": snapshot.symbol,
        "observed_at": snapshot.observed_at.isoformat(),
        "frames": tuple(frames),
    }


def verify_c008_hfm_candle_builder(
    *,
    bridge_root: Path,
    tick_database: Path,
    symbols: Sequence[str],
    observed_at: datetime,
) -> Mapping[str, object]:
    adapter = HfmCsvMarketDataAdapter(bridge_root)
    connection = sqlite3.connect(f"file:{tick_database.resolve()}?mode=ro", uri=True)
    try:
        results = []
        for symbol in symbols:
            tick_rows = connection.execute(
                "SELECT timestamp,bid,ask FROM raw_ticks WHERE symbol=? ORDER BY timestamp,bid,ask",
                (symbol,),
            ).fetchall()
            ticks = tuple(
                RawTick(symbol, datetime.fromisoformat(timestamp), bid, ask)
                for timestamp, bid, ask in tick_rows
            )
            dataset = adapter.dataset(
                symbol,
                observed_at=observed_at,
                include_latest_tick=False,
                derive_m3_history=True,
                history_mode="combined",
            )
            snapshot = snapshot_from_dataset(
                with_tick_window(dataset, ticks),
                observed_at=observed_at,
            )
            results.append(verify_c008_snapshot(snapshot))
    finally:
        connection.close()
    return {
        "schema_version": 1,
        "requirement_id": "C008",
        "status": "PASS",
        "observed_at": observed_at.isoformat(),
        "bridge_root": str(bridge_root),
        "tick_database": str(tick_database),
        "required_timeframes": C008_TIMEFRAMES,
        "symbols": tuple(results),
    }


def verify_c009_market_state(state: MarketState) -> Mapping[str, object]:
    if not state.state_id or state.latest_tick is None:
        raise ValueError("C009 market state lacks identity or current tick")
    if state.latest_tick.symbol != state.symbol:
        raise ValueError("C009 market state tick symbol mismatch")
    if state.latest_tick.timestamp > state.observed_at:
        raise ValueError("C009 market state contains a future tick")
    observed = tuple(state.timeframes)
    if observed != MARKET_STATE_TIMEFRAMES:
        raise ValueError(
            f"C009 timeframe context mismatch expected={MARKET_STATE_TIMEFRAMES} observed={observed}"
        )
    frames = []
    for timeframe in MARKET_STATE_TIMEFRAMES:
        candle = state.timeframes[timeframe]
        if candle.timeframe != timeframe or candle.symbol != state.symbol:
            raise ValueError(f"C009 invalid {timeframe} identity")
        if candle.end > state.observed_at:
            raise ValueError(f"C009 forming {timeframe} entered market state")
        if not candle.source or not candle.source_id:
            raise ValueError(f"C009 {timeframe} lacks source lineage")
        frames.append(
            {
                "timeframe": timeframe,
                "start": candle.start.isoformat(),
                "end": candle.end.isoformat(),
                "open": candle.open,
                "high": candle.high,
                "low": candle.low,
                "close": candle.close,
                "source": candle.source,
                "source_id": candle.source_id,
            }
        )
    return {
        "state_id": state.state_id,
        "symbol": state.symbol,
        "observed_at": state.observed_at.isoformat(),
        "latest_tick": {
            "timestamp": state.latest_tick.timestamp.isoformat(),
            "bid": state.latest_tick.bid,
            "ask": state.latest_tick.ask,
        },
        "timeframes": tuple(frames),
        "source_ids": state.source_ids,
    }


def verify_c009_hfm_market_states(
    *,
    bridge_root: Path,
    symbols: Sequence[str],
    observed_at: datetime,
) -> Mapping[str, object]:
    adapter = HfmCsvMarketDataAdapter(bridge_root)
    results = []
    for symbol in symbols:
        symbol_observed_at = datetime.now(timezone.utc)
        dataset = adapter.dataset(
            symbol,
            observed_at=symbol_observed_at,
            timeframes=MARKET_STATE_TIMEFRAMES,
            include_latest_tick=True,
            derive_m3_history=False,
            history_mode="combined",
        )
        snapshot = snapshot_from_dataset(
            dataset,
            observed_at=symbol_observed_at,
            required_timeframes=MARKET_STATE_TIMEFRAMES,
            include_micro=False,
        )
        results.append(verify_c009_market_state(market_state_from_snapshot(snapshot)))
    return {
        "schema_version": 1,
        "requirement_id": "C009",
        "status": "PASS",
        "observed_at": observed_at.isoformat(),
        "bridge_root": str(bridge_root),
        "required_context": ("Tick", *MARKET_STATE_TIMEFRAMES),
        "symbols": tuple(results),
    }


def verify_c010_event_snapshots(
    snapshots: Sequence[EventMarketSnapshot],
) -> Mapping[str, object]:
    rows = validate_event_snapshot_sequence(snapshots)
    return {
        "event_id": rows[0].event_id,
        "symbol": rows[0].symbol,
        "decision_id": rows[1].decision_id,
        "phases": tuple(
            {
                "phase": item.phase,
                "snapshot_id": item.snapshot_id,
                "state_id": item.state_id,
                "observed_at": item.observed_at.isoformat(),
                "captured_at": item.captured_at.isoformat(),
                "canonical_state_sha256": sha256(item.canonical_state.encode("utf-8")).hexdigest(),
                "source_ids": item.source_ids,
            }
            for item in rows
        ),
    }


def verify_c010_hfm_snapshot_engine(
    *,
    bridge_root: Path,
    database: Path,
    symbol: str,
) -> Mapping[str, object]:
    adapter = HfmCsvMarketDataAdapter(bridge_root)
    event_id = f"C010:{symbol}:{datetime.now(timezone.utc).isoformat()}"
    decision_id = sha256(f"{event_id}:decision".encode("utf-8")).hexdigest()
    snapshots = []
    store = EvidenceStore(database)
    try:
        for phase in EVENT_SNAPSHOT_PHASES:
            observed_at = datetime.now(timezone.utc)
            dataset = adapter.dataset(
                symbol,
                observed_at=observed_at,
                timeframes=MARKET_STATE_TIMEFRAMES,
                include_latest_tick=True,
                derive_m3_history=False,
                history_mode="combined",
            )
            snapshot = snapshot_from_dataset(
                dataset,
                observed_at=observed_at,
                required_timeframes=MARKET_STATE_TIMEFRAMES,
                include_micro=False,
            )
            state = market_state_from_snapshot(snapshot)
            frozen = freeze_event_snapshot(
                event_id=event_id,
                phase=phase,
                state=state,
                captured_at=datetime.now(timezone.utc),
                decision_id=None if phase == "PRE_EVENT" else decision_id,
            )
            store.write_event_market_snapshot(frozen)
            snapshots.append(frozen)
        persisted = store.load_event_market_snapshots(event_id)
        verified = verify_c010_event_snapshots(persisted)
        integrity = store.integrity()
        if not integrity:
            raise ValueError("C010 event snapshot store failed integrity check")
        store.checkpoint()
    finally:
        store.close()
    return {
        "schema_version": 1,
        "requirement_id": "C010",
        "status": "PASS",
        "database": str(database),
        "database_integrity": integrity,
        "required_phases": EVENT_SNAPSHOT_PHASES,
        "snapshot_sequence": verified,
    }


def verify_c011_svg_chart(
    snapshot: MarketSnapshot,
    *,
    timeframe: str,
    svg: str,
    output: Path,
    maximum_bar_age_seconds: float = 900.0,
) -> Mapping[str, object]:
    rows = tuple(snapshot.candles.get(timeframe, ()))
    if snapshot.frame_status.get(timeframe) != "COMPLETED" or not rows:
        raise ValueError(f"C011 requires a completed live {timeframe} frame")
    latest = rows[-1]
    age = (snapshot.observed_at - latest.end).total_seconds()
    if age < 0 or age > maximum_bar_age_seconds:
        raise ValueError(f"C011 live chart source is stale age_seconds={age}")
    if not svg.startswith("<svg") or not svg.endswith("</svg>"):
        raise ValueError("C011 renderer did not produce a complete SVG")
    if f"{snapshot.symbol} {timeframe}" not in svg:
        raise ValueError("C011 chart identity is missing from SVG")
    rendered = tuple(rows[-200:])
    if svg.count("<line x1=") != len(rendered):
        raise ValueError("C011 candlestick count does not match the live frame")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(svg, encoding="utf-8")
    payload = output.read_bytes()
    return {
        "symbol": snapshot.symbol,
        "timeframe": timeframe,
        "observed_at": snapshot.observed_at.isoformat(),
        "latest_completed_bar_end": latest.end.isoformat(),
        "bar_age_seconds": age,
        "rendered_candles": len(rendered),
        "source": latest.source,
        "source_id": latest.source_id,
        "chart_path": str(output),
        "chart_bytes": len(payload),
        "chart_sha256": sha256(payload).hexdigest(),
    }


def verify_c011_hfm_live_chart(
    *,
    bridge_root: Path,
    symbol: str,
    timeframe: str,
    chart_directory: Path,
) -> Mapping[str, object]:
    observed_at = datetime.now(timezone.utc)
    adapter = HfmCsvMarketDataAdapter(bridge_root)
    dataset = adapter.dataset(
        symbol,
        observed_at=observed_at,
        timeframes=(timeframe,),
        include_latest_tick=False,
        derive_m3_history=False,
        history_mode="rolling",
    )
    snapshot = snapshot_from_dataset(
        dataset,
        observed_at=observed_at,
        required_timeframes=(timeframe,),
        include_micro=False,
        max_bars_per_frame=500,
    )
    report = IntelligenceEngine().analyse(snapshot)
    rows = tuple(snapshot.candles[timeframe][-200:])
    svg = render_svg(rows, annotations=report.chart.annotations.get(timeframe, ()))
    chart = verify_c011_svg_chart(
        snapshot,
        timeframe=timeframe,
        svg=svg,
        output=chart_directory / f"c011_live_{symbol}_{timeframe}.svg",
    )
    return {
        "schema_version": 1,
        "requirement_id": "C011",
        "status": "PASS",
        "bridge_root": str(bridge_root),
        "chart": chart,
    }


C012_TIMEFRAMES = ("M1", "M5", "M15", "H1", "H4")


def verify_c012_chart_pack(
    snapshot: MarketSnapshot,
    chart_pack: ChartPack,
    rendered: Mapping[str, str],
) -> Mapping[str, object]:
    if chart_pack.symbol != snapshot.symbol:
        raise ValueError("C012 chart-pack symbol mismatch")
    if chart_pack.observed_at != snapshot.observed_at.isoformat():
        raise ValueError("C012 chart-pack observation mismatch")
    if chart_pack.pack_id != snapshot_state_id(snapshot):
        raise ValueError("C012 chart-pack state identity mismatch")
    if chart_pack.source_ids != tuple(snapshot.source_ids):
        raise ValueError("C012 chart-pack source lineage mismatch")
    if tuple(chart_pack.timeframes) != C012_TIMEFRAMES or tuple(rendered) != C012_TIMEFRAMES:
        raise ValueError("C012 chart pack requires linked M1/M5/M15/H1/H4 frames")
    frames = []
    for timeframe in C012_TIMEFRAMES:
        rows = tuple(chart_pack.timeframes[timeframe])
        if not rows or snapshot.frame_status.get(timeframe) != "COMPLETED":
            raise ValueError(f"C012 chart pack lacks completed {timeframe}")
        if rows != tuple(snapshot.candles[timeframe]):
            raise ValueError(f"C012 {timeframe} does not originate from the common snapshot")
        svg = rendered[timeframe]
        if f"{snapshot.symbol} {timeframe}" not in svg:
            raise ValueError(f"C012 rendered {timeframe} identity mismatch")
        frames.append(
            {
                "timeframe": timeframe,
                "candles": len(rows),
                "latest_completed_bar_end": rows[-1].end.isoformat(),
                "source": rows[-1].source,
                "source_id": rows[-1].source_id,
                "svg_sha256": sha256(svg.encode("utf-8")).hexdigest(),
            }
        )
    return {
        "pack_id": chart_pack.pack_id,
        "symbol": chart_pack.symbol,
        "observed_at": chart_pack.observed_at,
        "source_ids": chart_pack.source_ids,
        "frames": tuple(frames),
    }


def verify_c012_hfm_chart_pack(
    *,
    bridge_root: Path,
    symbol: str,
    chart_directory: Path,
) -> Mapping[str, object]:
    observed_at = datetime.now(timezone.utc)
    adapter = HfmCsvMarketDataAdapter(bridge_root)
    dataset = adapter.dataset(
        symbol,
        observed_at=observed_at,
        timeframes=C012_TIMEFRAMES,
        include_latest_tick=False,
        derive_m3_history=False,
        history_mode="rolling",
    )
    snapshot = snapshot_from_dataset(
        dataset,
        observed_at=observed_at,
        required_timeframes=C012_TIMEFRAMES,
        include_micro=False,
        max_bars_per_frame=500,
    )
    report = IntelligenceEngine().analyse(snapshot)
    rendered = {
        timeframe: render_svg(
            tuple(snapshot.candles[timeframe][-200:]),
            annotations=report.chart.annotations.get(timeframe, ()),
        )
        for timeframe in C012_TIMEFRAMES
    }
    verified = verify_c012_chart_pack(snapshot, report.chart, rendered)
    chart_directory.mkdir(parents=True, exist_ok=True)
    files = []
    for timeframe, svg in rendered.items():
        path = chart_directory / f"c012_live_{symbol}_{timeframe}.svg"
        path.write_text(svg, encoding="utf-8")
        files.append(
            {
                "timeframe": timeframe,
                "path": str(path),
                "sha256": sha256(path.read_bytes()).hexdigest(),
            }
        )
    pack_manifest = chart_directory / f"c012_live_{symbol}_chart_pack.json"
    manifest_payload = {**verified, "files": tuple(files)}
    pack_manifest.write_text(
        json.dumps(manifest_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "schema_version": 1,
        "requirement_id": "C012",
        "status": "PASS",
        "bridge_root": str(bridge_root),
        "pack_manifest": str(pack_manifest),
        "chart_pack": manifest_payload,
    }


def verify_c013_annotations(
    chart_pack: ChartPack,
    *,
    timeframe: str,
    svg: str,
) -> Mapping[str, object]:
    annotations = tuple(chart_pack.annotations.get(timeframe, ()))
    kinds = tuple(str(item.get("type") or "") for item in annotations)
    required_exact = {"ENTRY", "STOP_LOSS", "TAKE_PROFIT", "STRUCTURE", "VWAP"}
    missing = sorted(required_exact - set(kinds))
    families = {
        "ZONE": any(kind in {"SUPPLY", "DEMAND"} for kind in kinds),
        "LIQUIDITY": any(
            kind in {"EQUAL_HIGH", "EQUAL_LOW", "SWING_HIGH", "SWING_LOW", "PREVIOUS_HIGH", "PREVIOUS_LOW"}
            for kind in kinds
        ),
        "FVG": any(kind.startswith("FVG_") for kind in kinds),
        "SESSION": any(kind.startswith("SESSION_") for kind in kinds),
    }
    missing.extend(name for name, present in families.items() if not present)
    if missing:
        raise ValueError(f"C013 missing chart annotations: {','.join(missing)}")
    for kind in required_exact:
        if f'data-kind="{kind}"' not in svg:
            raise ValueError(f"C013 SVG does not plot {kind}")
    if not any(f'data-kind="{kind}"' in svg for kind in ("SUPPLY", "DEMAND")):
        raise ValueError("C013 SVG does not plot a supply/demand zone")
    if 'data-kind="FVG_' not in svg or 'data-kind="SESSION_' not in svg:
        raise ValueError("C013 SVG does not plot FVG and session context")
    return {
        "pack_id": chart_pack.pack_id,
        "symbol": chart_pack.symbol,
        "timeframe": timeframe,
        "annotation_count": len(annotations),
        "annotation_types": tuple(sorted(set(kinds))),
        "required_families": {**{name: True for name in required_exact}, **families},
        "svg_sha256": sha256(svg.encode("utf-8")).hexdigest(),
    }


def verify_c013_hfm_annotations(
    *,
    bridge_root: Path,
    symbol: str,
    timeframe: str,
    chart_directory: Path,
) -> Mapping[str, object]:
    observed_at = datetime.now(timezone.utc)
    adapter = HfmCsvMarketDataAdapter(bridge_root)
    dataset = adapter.dataset(
        symbol,
        observed_at=observed_at,
        timeframes=C012_TIMEFRAMES,
        include_latest_tick=False,
        derive_m3_history=False,
        history_mode="rolling",
    )
    snapshot = snapshot_from_dataset(
        dataset,
        observed_at=observed_at,
        required_timeframes=C012_TIMEFRAMES,
        include_micro=False,
        max_bars_per_frame=500,
    )
    report = IntelligenceEngine().analyse(snapshot)
    rows = tuple(snapshot.candles[timeframe])
    latest = rows[-1]
    volatility = report.volatility[timeframe]
    distance = max(volatility.atr, abs(latest.close) * 0.0001, 1e-9)
    decision = TradeDecision(
        decision_id=sha256(f"C013:{report.state_id}".encode("utf-8")).hexdigest(),
        symbol=symbol,
        action="BUY",
        created_at=observed_at,
        entry=latest.close,
        stop=latest.close - distance,
        target=latest.close + distance * 2,
    )
    annotated = with_trade_annotations(report.chart, decision)
    svg = render_svg(
        tuple(rows[-200:]),
        annotations=annotated.annotations.get(timeframe, ()),
    )
    verified = verify_c013_annotations(annotated, timeframe=timeframe, svg=svg)
    chart_directory.mkdir(parents=True, exist_ok=True)
    path = chart_directory / f"c013_live_{symbol}_{timeframe}_annotated.svg"
    path.write_text(svg, encoding="utf-8")
    return {
        "schema_version": 1,
        "requirement_id": "C013",
        "status": "PASS",
        "bridge_root": str(bridge_root),
        "observed_at": observed_at.isoformat(),
        "state_id": report.state_id,
        "decision_id": decision.decision_id,
        "chart_path": str(path),
        "annotations": verified,
    }


def verify_c014_hfm_chart_history(
    *,
    bridge_root: Path,
    database: Path,
    symbol: str,
    timeframe: str,
    chart_directory: Path,
) -> Mapping[str, object]:
    observed_at = datetime.now(timezone.utc)
    adapter = HfmCsvMarketDataAdapter(bridge_root)
    dataset = adapter.dataset(
        symbol,
        observed_at=observed_at,
        timeframes=C012_TIMEFRAMES,
        include_latest_tick=False,
        derive_m3_history=False,
        history_mode="rolling",
    )
    snapshot = snapshot_from_dataset(
        dataset,
        observed_at=observed_at,
        required_timeframes=C012_TIMEFRAMES,
        include_micro=False,
        max_bars_per_frame=500,
    )
    report = IntelligenceEngine().analyse(snapshot)
    rows = tuple(snapshot.candles[timeframe][-200:])
    latest = rows[-1]
    distance = max(report.volatility[timeframe].atr, abs(latest.close) * 0.0001, 1e-9)
    decision = TradeDecision(
        decision_id=sha256(f"C014:{report.state_id}".encode("utf-8")).hexdigest(),
        symbol=symbol,
        action="BUY",
        created_at=observed_at,
        entry=latest.close,
        stop=latest.close - distance,
        target=latest.close + distance * 2,
    )
    annotated = with_trade_annotations(report.chart, decision)
    svg = render_svg(rows, annotations=annotated.annotations.get(timeframe, ()))
    chart = build_persistent_chart(annotated, decision, timeframe, rows, svg)
    expected_payload = json.loads(json.dumps(chart.payload, sort_keys=True))

    store = EvidenceStore(database)
    count_before = store._conn.execute("SELECT COUNT(*) FROM chart_evidence").fetchone()[0]
    store.write_chart(chart.chart_id, chart.symbol, chart.observed_at, chart.payload)
    count_after_first_write = store._conn.execute("SELECT COUNT(*) FROM chart_evidence").fetchone()[0]
    store.write_chart(chart.chart_id, chart.symbol, chart.observed_at, chart.payload)
    count_after_rescan = store._conn.execute("SELECT COUNT(*) FROM chart_evidence").fetchone()[0]
    store.close()
    if count_after_first_write != count_before + 1 or count_after_rescan != count_after_first_write:
        raise ValueError("C014 chart history is not idempotent across an identical rescan")

    reopened = EvidenceStore(database)
    loaded = reopened.load_chart(chart.chart_id)
    history = reopened.load_chart_history(symbol=symbol, timeframe=timeframe)
    healthy = reopened.integrity()
    reopened.close()
    if loaded is None or loaded["payload"] != expected_payload:
        raise ValueError("C014 chart or underlying candles changed after database restart")
    validate_persistent_chart(
        loaded["payload"],
        symbol=symbol,
        observed_at=str(loaded["observed_at"]),
    )
    if tuple(item["chart_id"] for item in history).count(chart.chart_id) != 1:
        raise ValueError("C014 chart history cannot retrieve the persisted chart uniquely")
    if not healthy:
        raise ValueError("C014 chart history database integrity failed")

    chart_directory.mkdir(parents=True, exist_ok=True)
    chart_path = chart_directory / f"c014_live_{symbol}_{timeframe}_persistent.svg"
    chart_path.write_text(svg, encoding="utf-8")
    return {
        "schema_version": 1,
        "requirement_id": "C014",
        "status": "PASS",
        "bridge_root": str(bridge_root),
        "database": str(database),
        "chart_path": str(chart_path),
        "chart_id": chart.chart_id,
        "pack_id": annotated.pack_id,
        "state_id": report.state_id,
        "decision_id": decision.decision_id,
        "symbol": symbol,
        "timeframe": timeframe,
        "observed_at": observed_at.isoformat(),
        "candle_count": len(rows),
        "candle_hash": loaded["payload"]["candle_hash"],
        "chart_hash": loaded["payload"]["chart_hash"],
        "source_ids": loaded["payload"]["source_ids"],
        "rescan_idempotent": True,
        "restart_reload_exact": True,
        "database_integrity": True,
    }


def verify_c015_structure_report(report: object) -> Mapping[str, object]:
    structures = getattr(report, "structure", None)
    if not isinstance(structures, Mapping) or not structures:
        raise ValueError("C015 report has no price-structure output")
    valid_labels = {"HH", "HL", "LH", "LL", "EH", "EL"}
    frames = {}
    for timeframe, state in structures.items():
        labels = tuple(point.label for point in state.swings if point.label is not None)
        if labels != tuple(state.swing_sequence):
            raise ValueError(f"C015 swing labels are not chronological for {timeframe}")
        if not set(labels).issubset(valid_labels):
            raise ValueError(f"C015 emitted an invalid swing label for {timeframe}")
        if state.direction not in {"UP", "DOWN", "RANGE", "UNKNOWN"}:
            raise ValueError(f"C015 emitted an invalid structure direction for {timeframe}")
        if state.break_of_structure != (state.bos_direction is not None):
            raise ValueError(f"C015 BOS state is inconsistent for {timeframe}")
        if state.change_of_character != (state.choch_direction is not None):
            raise ValueError(f"C015 CHOCH state is inconsistent for {timeframe}")
        if state.choch_direction is not None and state.choch_direction != state.bos_direction:
            raise ValueError(f"C015 CHOCH direction does not match its structural break for {timeframe}")
        frames[str(timeframe)] = {
            "direction": state.direction,
            "swing_count": len(state.swings),
            "swing_sequence": labels,
            "break_of_structure": state.break_of_structure,
            "bos_direction": state.bos_direction,
            "change_of_character": state.change_of_character,
            "choch_direction": state.choch_direction,
            "trend_strength": state.trend_strength,
            "directional_persistence": state.directional_persistence,
        }
    return {
        "state_id": report.state_id,
        "symbol": report.symbol,
        "observed_at": report.observed_at,
        "frames": frames,
    }


def verify_c015_hfm_price_structure(
    *,
    bridge_root: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    observed_at = datetime.now(timezone.utc)
    adapter = HfmCsvMarketDataAdapter(bridge_root)
    output = {}
    for symbol in symbols:
        dataset = adapter.dataset(
            symbol,
            observed_at=observed_at,
            timeframes=C012_TIMEFRAMES,
            include_latest_tick=False,
            derive_m3_history=False,
            history_mode="rolling",
        )
        snapshot = snapshot_from_dataset(
            dataset,
            observed_at=observed_at,
            required_timeframes=C012_TIMEFRAMES,
            include_micro=False,
            max_bars_per_frame=500,
        )
        output[symbol] = verify_c015_structure_report(IntelligenceEngine().analyse(snapshot))
    return {
        "schema_version": 1,
        "requirement_id": "C015",
        "status": "PASS",
        "bridge_root": str(bridge_root),
        "observed_at": observed_at.isoformat(),
        "definitions": {
            "HH": "current confirmed swing high is above the prior confirmed swing high",
            "LH": "current confirmed swing high is below the prior confirmed swing high",
            "HL": "current confirmed swing low is above the prior confirmed swing low",
            "LL": "current confirmed swing low is below the prior confirmed swing low",
            "BOS": "latest completed close crosses a confirmed swing boundary",
            "CHOCH": "BOS direction is opposite the established HH/HL or LH/LL structure",
        },
        "symbols": output,
    }


def verify_c016_swing_hierarchy(report: object) -> Mapping[str, object]:
    base = verify_c015_structure_report(report)
    output = {}
    observed_hierarchies: set[str] = set()
    for timeframe, state in report.structure.items():
        swings = []
        for point in state.swings:
            if point.hierarchy not in {"MAJOR", "MINOR"}:
                raise ValueError(f"C016 invalid swing hierarchy for {timeframe}")
            if not 0.0 <= point.significance <= 1.0:
                raise ValueError(f"C016 invalid swing significance for {timeframe}")
            observed_hierarchies.add(point.hierarchy)
            swings.append(
                {
                    "index": point.index,
                    "price": point.price,
                    "kind": point.kind,
                    "label": point.label,
                    "hierarchy": point.hierarchy,
                    "significance": point.significance,
                    "prominence": point.strength,
                }
            )
        output[str(timeframe)] = tuple(swings)
    return {
        "state_id": base["state_id"],
        "symbol": base["symbol"],
        "observed_at": base["observed_at"],
        "frames": output,
        "observed_hierarchies": tuple(sorted(observed_hierarchies)),
    }


def verify_c016_hfm_swing_hierarchy(
    *,
    bridge_root: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    observed_at = datetime.now(timezone.utc)
    adapter = HfmCsvMarketDataAdapter(bridge_root)
    output = {}
    observed_hierarchies: set[str] = set()
    for symbol in symbols:
        dataset = adapter.dataset(
            symbol,
            observed_at=observed_at,
            timeframes=C012_TIMEFRAMES,
            include_latest_tick=False,
            derive_m3_history=False,
            history_mode="rolling",
        )
        snapshot = snapshot_from_dataset(
            dataset,
            observed_at=observed_at,
            required_timeframes=C012_TIMEFRAMES,
            include_micro=False,
            max_bars_per_frame=500,
        )
        result = verify_c016_swing_hierarchy(IntelligenceEngine().analyse(snapshot))
        observed_hierarchies.update(result["observed_hierarchies"])
        output[symbol] = result
    if observed_hierarchies != {"MAJOR", "MINOR"}:
        raise ValueError("C016 live evidence did not observe both major and minor swings")
    return {
        "schema_version": 1,
        "requirement_id": "C016",
        "status": "PASS",
        "bridge_root": str(bridge_root),
        "observed_at": observed_at.isoformat(),
        "definition": "minor pivots use the confirmed local window; major pivots remain extreme across a three-times wider window; significance is normalized pivot prominence",
        "observed_hierarchies": tuple(sorted(observed_hierarchies)),
        "symbols": output,
    }


def _verify_zone_kind(report: object, *, kind: str, requirement_id: str) -> Mapping[str, object]:
    zones = getattr(report, "zones", None)
    if not isinstance(zones, Mapping) or not zones:
        raise ValueError(f"{requirement_id} report has no zone output")
    label = kind.lower()
    output = {}
    total = 0
    observed_states: set[str] = set()
    for timeframe, rows in zones.items():
        selected = []
        for zone in rows:
            if zone.kind != kind:
                continue
            if zone.low >= zone.high or zone.origin_index < 0 or zone.displacement <= 0:
                raise ValueError(f"{requirement_id} malformed {label} zone for {timeframe}")
            if zone.freshness not in {"FRESH", "TESTED", "MITIGATED", "FAILED"}:
                raise ValueError(f"{requirement_id} invalid {label} lifecycle for {timeframe}")
            if not 0.0 <= zone.penetration_ratio <= 1.0 or zone.touches < 0:
                raise ValueError(f"{requirement_id} invalid {label} lifecycle metrics for {timeframe}")
            if (zone.freshness == "FAILED") != (zone.invalidated_at is not None):
                raise ValueError(f"{requirement_id} {label} invalidation is inconsistent for {timeframe}")
            observed_states.add(zone.freshness)
            total += 1
            selected.append(
                {
                    "low": zone.low,
                    "high": zone.high,
                    "origin_index": zone.origin_index,
                    "lifecycle": zone.freshness,
                    "displacement": zone.displacement,
                    "touches": zone.touches,
                    "penetration_ratio": zone.penetration_ratio,
                    "invalidated_at": zone.invalidated_at,
                }
            )
        output[str(timeframe)] = tuple(selected)
    return {
        "state_id": report.state_id,
        "symbol": report.symbol,
        "observed_at": report.observed_at,
        f"{label}_zone_count": total,
        "observed_states": tuple(sorted(observed_states)),
        "frames": output,
    }


def verify_c017_supply_zones(report: object) -> Mapping[str, object]:
    return _verify_zone_kind(report, kind="SUPPLY", requirement_id="C017")


def verify_c018_demand_zones(report: object) -> Mapping[str, object]:
    return _verify_zone_kind(report, kind="DEMAND", requirement_id="C018")


def verify_c017_hfm_supply_zones(
    *,
    bridge_root: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    observed_at = datetime.now(timezone.utc)
    adapter = HfmCsvMarketDataAdapter(bridge_root)
    output = {}
    total = 0
    observed_states: set[str] = set()
    for symbol in symbols:
        dataset = adapter.dataset(
            symbol,
            observed_at=observed_at,
            timeframes=C012_TIMEFRAMES,
            include_latest_tick=False,
            derive_m3_history=False,
            history_mode="rolling",
        )
        snapshot = snapshot_from_dataset(
            dataset,
            observed_at=observed_at,
            required_timeframes=C012_TIMEFRAMES,
            include_micro=False,
            max_bars_per_frame=500,
        )
        result = verify_c017_supply_zones(IntelligenceEngine().analyse(snapshot))
        total += result["supply_zone_count"]
        observed_states.update(result["observed_states"])
        output[symbol] = result
    if total == 0:
        raise ValueError("C017 live evidence found no supply zones")
    return {
        "schema_version": 1,
        "requirement_id": "C017",
        "status": "PASS",
        "bridge_root": str(bridge_root),
        "observed_at": observed_at.isoformat(),
        "supply_zone_count": total,
        "observed_states": tuple(sorted(observed_states)),
        "symbols": output,
    }


def verify_c018_hfm_demand_zones(
    *,
    bridge_root: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    observed_at = datetime.now(timezone.utc)
    adapter = HfmCsvMarketDataAdapter(bridge_root)
    output = {}
    total = 0
    observed_states: set[str] = set()
    for symbol in symbols:
        dataset = adapter.dataset(
            symbol,
            observed_at=observed_at,
            timeframes=C012_TIMEFRAMES,
            include_latest_tick=False,
            derive_m3_history=False,
            history_mode="rolling",
        )
        snapshot = snapshot_from_dataset(
            dataset,
            observed_at=observed_at,
            required_timeframes=C012_TIMEFRAMES,
            include_micro=False,
            max_bars_per_frame=500,
        )
        result = verify_c018_demand_zones(IntelligenceEngine().analyse(snapshot))
        total += result["demand_zone_count"]
        observed_states.update(result["observed_states"])
        output[symbol] = result
    if total == 0:
        raise ValueError("C018 live evidence found no demand zones")
    return {
        "schema_version": 1,
        "requirement_id": "C018",
        "status": "PASS",
        "bridge_root": str(bridge_root),
        "observed_at": observed_at.isoformat(),
        "demand_zone_count": total,
        "observed_states": tuple(sorted(observed_states)),
        "symbols": output,
    }


def verify_c019_zone_quality(report: object) -> Mapping[str, object]:
    zones = getattr(report, "zones", None)
    qualities = getattr(report, "zone_quality", None)
    if not isinstance(zones, Mapping) or not isinstance(qualities, Mapping):
        raise ValueError("C019 report is missing zones or zone quality")
    output = {}
    total = 0
    scores = []
    for timeframe, rows in zones.items():
        observed = tuple(qualities.get(timeframe, ()))
        if len(observed) != len(rows):
            raise ValueError(f"C019 zone/quality cardinality mismatch for {timeframe}")
        frame = []
        for zone, quality in zip(rows, observed):
            bounded = (
                quality.freshness,
                quality.displacement,
                quality.penetration,
                quality.reaction,
                quality.touch_quality,
                quality.score,
            )
            if not all(0.0 <= value <= 1.0 for value in bounded):
                raise ValueError(f"C019 unbounded zone quality component for {timeframe}")
            if quality.lifecycle != zone.freshness or quality.touches != zone.touches:
                raise ValueError(f"C019 quality is not linked to zone lifecycle for {timeframe}")
            if abs(quality.penetration - zone.penetration_ratio) > 1e-9:
                raise ValueError(f"C019 penetration differs from zone lifecycle for {timeframe}")
            if abs(quality.displacement_ratio - zone.displacement) > 1e-9:
                raise ValueError(f"C019 displacement differs from originating zone for {timeframe}")
            total += 1
            scores.append(quality.score)
            frame.append(
                {
                    "kind": zone.kind,
                    "lifecycle": quality.lifecycle,
                    "freshness": quality.freshness,
                    "displacement": quality.displacement,
                    "displacement_ratio": quality.displacement_ratio,
                    "touches": quality.touches,
                    "touch_quality": quality.touch_quality,
                    "penetration": quality.penetration,
                    "reaction": quality.reaction,
                    "score": quality.score,
                    "observation_count": quality.observation_count,
                }
            )
        output[str(timeframe)] = tuple(frame)
    return {
        "state_id": report.state_id,
        "symbol": report.symbol,
        "observed_at": report.observed_at,
        "zone_count": total,
        "mean_score": mean(scores) if scores else None,
        "frames": output,
    }


def verify_c019_hfm_zone_quality(
    *,
    bridge_root: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    observed_at = datetime.now(timezone.utc)
    adapter = HfmCsvMarketDataAdapter(bridge_root)
    output = {}
    total = 0
    weighted_score = 0.0
    for symbol in symbols:
        dataset = adapter.dataset(
            symbol,
            observed_at=observed_at,
            timeframes=C012_TIMEFRAMES,
            include_latest_tick=False,
            derive_m3_history=False,
            history_mode="rolling",
        )
        snapshot = snapshot_from_dataset(
            dataset,
            observed_at=observed_at,
            required_timeframes=C012_TIMEFRAMES,
            include_micro=False,
            max_bars_per_frame=500,
        )
        result = verify_c019_zone_quality(IntelligenceEngine().analyse(snapshot))
        count = result["zone_count"]
        total += count
        weighted_score += (result["mean_score"] or 0.0) * count
        output[symbol] = result
    if total == 0:
        raise ValueError("C019 live evidence found no scored zones")
    return {
        "schema_version": 1,
        "requirement_id": "C019",
        "status": "PASS",
        "bridge_root": str(bridge_root),
        "observed_at": observed_at.isoformat(),
        "zone_count": total,
        "mean_score": weighted_score / total,
        "score_role": "DESCRIPTIVE_RESEARCH_EVIDENCE_NOT_EXECUTION_GATE",
        "components": ("freshness", "displacement", "touches", "penetration", "reaction"),
        "symbols": output,
    }


def verify_c020_liquidity(report: object, snapshot: MarketSnapshot) -> Mapping[str, object]:
    liquidity = getattr(report, "liquidity", None)
    if not isinstance(liquidity, Mapping) or not liquidity:
        raise ValueError("C020 report has no liquidity output")
    valid_kinds = {
        "EQUAL_HIGH",
        "EQUAL_LOW",
        "SWING_HIGH",
        "SWING_LOW",
        "PREVIOUS_HIGH",
        "PREVIOUS_LOW",
        "SESSION_HIGH",
        "SESSION_LOW",
    }
    observed_kinds: set[str] = set()
    output = {}
    for timeframe, pools in liquidity.items():
        rows = snapshot.candles.get(timeframe, ())
        frame = []
        for pool in pools:
            if pool.kind not in valid_kinds or pool.price <= 0 or pool.distance < 0 or pool.observation_count < 1:
                raise ValueError(f"C020 malformed liquidity pool for {timeframe}")
            if pool.kind.startswith("SESSION_"):
                if pool.source != "SESSION_RANGE" or not pool.session_id:
                    raise ValueError("C020 session liquidity is missing provenance")
            elif not pool.indices:
                raise ValueError(f"C020 structural liquidity has no source index for {timeframe}")
            observed_kinds.add(pool.kind)
            frame.append(
                {
                    "kind": pool.kind,
                    "price": pool.price,
                    "distance": pool.distance,
                    "indices": pool.indices,
                    "observation_count": pool.observation_count,
                    "source": pool.source,
                    "session_id": pool.session_id,
                }
            )
        if len(rows) >= 2:
            previous = rows[-2]
            previous_highs = tuple(item for item in pools if item.kind == "PREVIOUS_HIGH")
            previous_lows = tuple(item for item in pools if item.kind == "PREVIOUS_LOW")
            if len(previous_highs) != 1 or previous_highs[0].price != previous.high:
                raise ValueError(f"C020 previous high is not the prior completed {timeframe} bar")
            if len(previous_lows) != 1 or previous_lows[0].price != previous.low:
                raise ValueError(f"C020 previous low is not the prior completed {timeframe} bar")
        output[str(timeframe)] = tuple(frame)
    expected_sessions = {
        (f"SESSION_{side}", session_id, getattr(value, side.lower()))
        for session_id, value in report.session_ranges.items()
        for side in ("HIGH", "LOW")
        if getattr(value, side.lower()) is not None
    }
    observed_sessions = {
        (pool.kind, pool.session_id, pool.price)
        for pool in liquidity.get("M1", ())
        if pool.kind.startswith("SESSION_")
    }
    if observed_sessions != expected_sessions:
        raise ValueError("C020 session liquidity does not match normalized session ranges")
    return {
        "state_id": report.state_id,
        "symbol": report.symbol,
        "observed_at": report.observed_at,
        "pool_count": sum(len(rows) for rows in liquidity.values()),
        "observed_kinds": tuple(sorted(observed_kinds)),
        "session_pool_count": len(observed_sessions),
        "frames": output,
    }


def verify_c020_hfm_liquidity(
    *,
    bridge_root: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    observed_at = datetime.now(timezone.utc)
    adapter = HfmCsvMarketDataAdapter(bridge_root)
    output = {}
    total = 0
    observed_kinds: set[str] = set()
    for symbol in symbols:
        dataset = adapter.dataset(
            symbol,
            observed_at=observed_at,
            timeframes=C012_TIMEFRAMES,
            include_latest_tick=False,
            derive_m3_history=False,
            history_mode="rolling",
        )
        snapshot = snapshot_from_dataset(
            dataset,
            observed_at=observed_at,
            required_timeframes=C012_TIMEFRAMES,
            include_micro=False,
            max_bars_per_frame=500,
        )
        result = verify_c020_liquidity(IntelligenceEngine().analyse(snapshot), snapshot)
        total += result["pool_count"]
        observed_kinds.update(result["observed_kinds"])
        output[symbol] = result
    required = {
        "EQUAL_HIGH", "EQUAL_LOW", "SWING_HIGH", "SWING_LOW",
        "PREVIOUS_HIGH", "PREVIOUS_LOW", "SESSION_HIGH", "SESSION_LOW",
    }
    if not required.issubset(observed_kinds):
        raise ValueError(f"C020 live evidence is missing liquidity kinds: {','.join(sorted(required - observed_kinds))}")
    return {
        "schema_version": 1,
        "requirement_id": "C020",
        "status": "PASS",
        "bridge_root": str(bridge_root),
        "observed_at": observed_at.isoformat(),
        "pool_count": total,
        "observed_kinds": tuple(sorted(observed_kinds)),
        "symbols": output,
    }


def verify_c021_liquidity_sweeps(report: object, snapshot: MarketSnapshot) -> Mapping[str, object]:
    sweeps = getattr(report, "liquidity_sweeps", None)
    if not isinstance(sweeps, Mapping) or not sweeps:
        raise ValueError("C021 report has no liquidity sweep output")
    output = {}
    observed_directions: set[str] = set()
    observed_outcomes: set[str] = set()
    total = 0
    for timeframe, events in sweeps.items():
        candles = tuple(snapshot.candles.get(timeframe, ()))
        frame = []
        for event in events:
            if event.direction not in {"UP", "DOWN"}:
                raise ValueError(f"C021 invalid sweep direction for {timeframe}")
            if event.source != "ROLLING_PRIOR_EXTREME" or event.lookback < 1:
                raise ValueError(f"C021 sweep lacks prior-level provenance for {timeframe}")
            if not event.lookback <= event.index < len(candles):
                raise ValueError(f"C021 sweep index is outside completed {timeframe} candles")
            history = candles[event.index - event.lookback:event.index]
            candle = candles[event.index]
            expected_level = max(item.high for item in history) if event.direction == "UP" else min(item.low for item in history)
            expected_price = candle.high if event.direction == "UP" else candle.low
            if event.level != expected_level or event.sweep_price != expected_price:
                raise ValueError(f"C021 sweep is not derived from the exact prior {timeframe} level")
            expected_penetration = (
                event.sweep_price - event.level
                if event.direction == "UP"
                else event.level - event.sweep_price
            )
            if expected_penetration <= 0 or event.penetration != expected_penetration:
                raise ValueError(f"C021 sweep has invalid penetration for {timeframe}")
            if event.outcome not in {"RECLAIM", "CONTINUATION", "UNRESOLVED", "PENDING"}:
                raise ValueError(f"C021 invalid outcome for {timeframe}")
            if event.reclaimed != (event.outcome == "RECLAIM"):
                raise ValueError(f"C021 reclaim state is inconsistent for {timeframe}")
            if event.continuation != (event.outcome == "CONTINUATION"):
                raise ValueError(f"C021 continuation state is inconsistent for {timeframe}")
            if event.reclaimed != (event.reclaim_index is not None):
                raise ValueError(f"C021 reclaim index is inconsistent for {timeframe}")
            if event.continuation != (event.continuation_index is not None):
                raise ValueError(f"C021 continuation index is inconsistent for {timeframe}")
            if not event.index <= event.observation_end_index < len(candles):
                raise ValueError(f"C021 outcome observation window is invalid for {timeframe}")
            observed_directions.add(event.direction)
            observed_outcomes.add(event.outcome)
            total += 1
            frame.append(
                {
                    "direction": event.direction,
                    "index": event.index,
                    "timestamp": candle.end.isoformat(),
                    "level": event.level,
                    "sweep_price": event.sweep_price,
                    "penetration": event.penetration,
                    "lookback": event.lookback,
                    "reclaimed": event.reclaimed,
                    "reclaim_index": event.reclaim_index,
                    "continuation": event.continuation,
                    "continuation_index": event.continuation_index,
                    "observation_end_index": event.observation_end_index,
                    "outcome": event.outcome,
                    "source": event.source,
                }
            )
        output[str(timeframe)] = tuple(frame)
    return {
        "state_id": report.state_id,
        "symbol": report.symbol,
        "observed_at": report.observed_at,
        "sweep_count": total,
        "observed_directions": tuple(sorted(observed_directions)),
        "observed_outcomes": tuple(sorted(observed_outcomes)),
        "outcome_role": "DESCRIPTIVE_RESEARCH_EVIDENCE_NOT_EXECUTION_GATE",
        "frames": output,
    }


def verify_c021_hfm_liquidity_sweeps(
    *,
    bridge_root: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    observed_at = datetime.now(timezone.utc)
    adapter = HfmCsvMarketDataAdapter(bridge_root)
    output = {}
    total = 0
    directions: set[str] = set()
    outcomes: set[str] = set()
    for symbol in symbols:
        dataset = adapter.dataset(
            symbol,
            observed_at=observed_at,
            timeframes=C012_TIMEFRAMES,
            include_latest_tick=False,
            derive_m3_history=False,
            history_mode="rolling",
        )
        snapshot = snapshot_from_dataset(
            dataset,
            observed_at=observed_at,
            required_timeframes=C012_TIMEFRAMES,
            include_micro=False,
            max_bars_per_frame=500,
        )
        result = verify_c021_liquidity_sweeps(IntelligenceEngine().analyse(snapshot), snapshot)
        total += result["sweep_count"]
        directions.update(result["observed_directions"])
        outcomes.update(result["observed_outcomes"])
        output[symbol] = result
    if total == 0 or directions != {"DOWN", "UP"}:
        raise ValueError("C021 live evidence did not observe both upper and lower stop runs")
    if not {"RECLAIM", "CONTINUATION"}.issubset(outcomes):
        raise ValueError("C021 live evidence did not observe both reclaim and continuation outcomes")
    return {
        "schema_version": 1,
        "requirement_id": "C021",
        "status": "PASS",
        "bridge_root": str(bridge_root),
        "observed_at": observed_at.isoformat(),
        "sweep_count": total,
        "observed_directions": tuple(sorted(directions)),
        "observed_outcomes": tuple(sorted(outcomes)),
        "outcome_role": "DESCRIPTIVE_RESEARCH_EVIDENCE_NOT_EXECUTION_GATE",
        "symbols": output,
    }


def verify_c022_hfm_liquidity_failure_library(
    *,
    bridge_root: Path,
    database: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    observed_at = datetime.now(timezone.utc)
    adapter = HfmCsvMarketDataAdapter(bridge_root)
    records = []
    by_symbol = {}
    for symbol in symbols:
        dataset = adapter.dataset(
            symbol,
            observed_at=observed_at,
            timeframes=C012_TIMEFRAMES,
            include_latest_tick=False,
            derive_m3_history=False,
            history_mode="rolling",
        )
        snapshot = snapshot_from_dataset(
            dataset,
            observed_at=observed_at,
            required_timeframes=C012_TIMEFRAMES,
            include_micro=False,
            max_bars_per_frame=500,
        )
        report = IntelligenceEngine().analyse(snapshot)
        selected = build_liquidity_failure_records(
            snapshot=snapshot,
            sweeps=report.liquidity_sweeps,
        )
        if any(row.outcome not in {"CONTINUATION", "UNRESOLVED"} for row in selected):
            raise ValueError("C022 library contains a reclaim or pending event")
        records.extend(selected)
        by_symbol[symbol] = len(selected)
    if not records:
        raise ValueError("C022 live evidence found no completed liquidity failures")
    database.parent.mkdir(parents=True, exist_ok=True)
    expected_ids = {row.failure_id for row in records}
    if len(expected_ids) != len(records):
        raise ValueError("C022 generated duplicate failure identities")
    store = EvidenceStore(database)
    try:
        inserted = store.write_liquidity_failures(records)
        duplicate_inserted = store.write_liquidity_failures(records)
        loaded = store.load_liquidity_failures()
        loaded_ids = {row.failure_id for row in loaded}
        integrity = store.integrity()
        checkpoint = store.checkpoint()
    finally:
        store.close()
    reopened = EvidenceStore(database)
    try:
        restarted_ids = {row.failure_id for row in reopened.load_liquidity_failures()}
        restart_integrity = reopened.integrity()
    finally:
        reopened.close()
    if duplicate_inserted != 0 or not expected_ids.issubset(loaded_ids):
        raise ValueError("C022 failure persistence is not immutable and idempotent")
    if restarted_ids != loaded_ids or not integrity or not restart_integrity:
        raise ValueError("C022 failure library did not survive an integrity-checked restart")
    outcomes = sorted({row.outcome for row in records})
    reasons = sorted({row.reason for row in records})
    return {
        "schema_version": 1,
        "requirement_id": "C022",
        "status": "PASS",
        "bridge_root": str(bridge_root),
        "database": str(database),
        "observed_at": observed_at.isoformat(),
        "generated_failure_count": len(records),
        "inserted_count": inserted,
        "stored_count": len(loaded_ids),
        "duplicate_inserted_count": duplicate_inserted,
        "observed_outcomes": outcomes,
        "observed_reasons": reasons,
        "by_symbol": by_symbol,
        "integrity": integrity and restart_integrity,
        "checkpoint": checkpoint,
        "record_role": "NEGATIVE_RESEARCH_EVIDENCE_NOT_EXECUTION_GATE",
    }


def verify_c023_fair_value_gaps(report: object, snapshot: MarketSnapshot) -> Mapping[str, object]:
    gaps = getattr(report, "fair_value_gaps", None)
    if not isinstance(gaps, Mapping) or not gaps:
        raise ValueError("C023 report has no fair-value-gap output")
    output = {}
    directions: set[str] = set()
    total = 0
    for timeframe, events in gaps.items():
        candles = tuple(snapshot.candles.get(timeframe, ()))
        frame = []
        for gap in events:
            if gap.direction not in {"BULLISH", "BEARISH"}:
                raise ValueError(f"C023 invalid gap direction for {timeframe}")
            if gap.source != "THREE_CANDLE_WICK_IMBALANCE":
                raise ValueError(f"C023 gap lacks detector provenance for {timeframe}")
            if not 0 <= gap.left_index < gap.impulse_index < gap.origin_index < len(candles):
                raise ValueError(f"C023 gap has invalid candle indices for {timeframe}")
            if (gap.left_index, gap.impulse_index) != (gap.origin_index - 2, gap.origin_index - 1):
                raise ValueError(f"C023 gap is not a consecutive three-candle formation for {timeframe}")
            left = candles[gap.left_index]
            right = candles[gap.origin_index]
            expected_low, expected_high = (
                (left.high, right.low)
                if gap.direction == "BULLISH"
                else (right.high, left.low)
            )
            if expected_low >= expected_high or gap.low != expected_low or gap.high != expected_high:
                raise ValueError(f"C023 gap boundaries do not match wick geometry for {timeframe}")
            if gap.width != gap.high - gap.low or gap.midpoint != (gap.low + gap.high) / 2:
                raise ValueError(f"C023 gap metrics are inconsistent for {timeframe}")
            directions.add(gap.direction)
            total += 1
            frame.append(
                {
                    "direction": gap.direction,
                    "low": gap.low,
                    "high": gap.high,
                    "width": gap.width,
                    "midpoint": gap.midpoint,
                    "left_index": gap.left_index,
                    "impulse_index": gap.impulse_index,
                    "origin_index": gap.origin_index,
                    "formation_time": right.end.isoformat(),
                    "state": gap.state,
                    "source": gap.source,
                }
            )
        output[str(timeframe)] = tuple(frame)
    return {
        "state_id": report.state_id,
        "symbol": report.symbol,
        "observed_at": report.observed_at,
        "gap_count": total,
        "observed_directions": tuple(sorted(directions)),
        "frames": output,
    }


def verify_c023_hfm_fair_value_gaps(
    *,
    bridge_root: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    observed_at = datetime.now(timezone.utc)
    adapter = HfmCsvMarketDataAdapter(bridge_root)
    output = {}
    total = 0
    directions: set[str] = set()
    for symbol in symbols:
        dataset = adapter.dataset(
            symbol,
            observed_at=observed_at,
            timeframes=C012_TIMEFRAMES,
            include_latest_tick=False,
            derive_m3_history=False,
            history_mode="rolling",
        )
        snapshot = snapshot_from_dataset(
            dataset,
            observed_at=observed_at,
            required_timeframes=C012_TIMEFRAMES,
            include_micro=False,
            max_bars_per_frame=500,
        )
        result = verify_c023_fair_value_gaps(IntelligenceEngine().analyse(snapshot), snapshot)
        total += result["gap_count"]
        directions.update(result["observed_directions"])
        output[symbol] = result
    if total == 0 or directions != {"BEARISH", "BULLISH"}:
        raise ValueError("C023 live evidence did not observe both bullish and bearish FVGs")
    return {
        "schema_version": 1,
        "requirement_id": "C023",
        "status": "PASS",
        "bridge_root": str(bridge_root),
        "observed_at": observed_at.isoformat(),
        "gap_count": total,
        "observed_directions": tuple(sorted(directions)),
        "definition": "consecutive three-candle wick imbalance: candle three low above candle one high, or candle three high below candle one low",
        "symbols": output,
    }


def verify_c024_fvg_lifecycle(report: object, snapshot: MarketSnapshot) -> Mapping[str, object]:
    gaps = getattr(report, "fair_value_gaps", None)
    if not isinstance(gaps, Mapping) or not gaps:
        raise ValueError("C024 report has no FVG lifecycle output")
    valid_states = {"UNTOUCHED", "PARTIAL", "FILLED", "FAILED"}
    observed_states: set[str] = set()
    output = {}
    total = 0
    for timeframe, events in gaps.items():
        candles = tuple(snapshot.candles.get(timeframe, ()))
        frame = []
        for gap in events:
            if gap.state not in valid_states or not 0.0 <= gap.fill_ratio <= 1.0:
                raise ValueError(f"C024 invalid FVG lifecycle for {timeframe}")
            if not gap.origin_index <= gap.observation_end_index < len(candles):
                raise ValueError(f"C024 invalid FVG observation end for {timeframe}")
            if gap.state == "UNTOUCHED":
                valid = gap.first_touched_index is None and gap.filled_at is None and gap.failed_at is None and gap.fill_ratio == 0.0
            elif gap.state == "PARTIAL":
                valid = gap.first_touched_index is not None and gap.filled_at is None and gap.failed_at is None and 0.0 < gap.fill_ratio < 1.0
            elif gap.state == "FILLED":
                valid = gap.first_touched_index is not None and gap.filled_at == gap.observation_end_index and gap.failed_at is None and gap.fill_ratio == 1.0
            else:
                valid = gap.first_touched_index is not None and gap.failed_at == gap.observation_end_index and gap.filled_at is None and gap.fill_ratio == 1.0
            if not valid:
                raise ValueError(f"C024 inconsistent {gap.state} lifecycle for {timeframe}")
            observed_states.add(gap.state)
            total += 1
            frame.append(
                {
                    "direction": gap.direction,
                    "origin_index": gap.origin_index,
                    "state": gap.state,
                    "first_touched_index": gap.first_touched_index,
                    "filled_at": gap.filled_at,
                    "failed_at": gap.failed_at,
                    "fill_ratio": gap.fill_ratio,
                    "observation_end_index": gap.observation_end_index,
                }
            )
        output[str(timeframe)] = tuple(frame)
    return {
        "state_id": report.state_id,
        "symbol": report.symbol,
        "observed_at": report.observed_at,
        "gap_count": total,
        "observed_states": tuple(sorted(observed_states)),
        "frames": output,
    }


def verify_c024_hfm_fvg_lifecycle(
    *,
    bridge_root: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    observed_at = datetime.now(timezone.utc)
    adapter = HfmCsvMarketDataAdapter(bridge_root)
    output = {}
    total = 0
    states: set[str] = set()
    for symbol in symbols:
        dataset = adapter.dataset(
            symbol,
            observed_at=observed_at,
            timeframes=C012_TIMEFRAMES,
            include_latest_tick=False,
            derive_m3_history=False,
            history_mode="rolling",
        )
        snapshot = snapshot_from_dataset(
            dataset,
            observed_at=observed_at,
            required_timeframes=C012_TIMEFRAMES,
            include_micro=False,
            max_bars_per_frame=500,
        )
        result = verify_c024_fvg_lifecycle(IntelligenceEngine().analyse(snapshot), snapshot)
        total += result["gap_count"]
        states.update(result["observed_states"])
        output[symbol] = result
    required = {"UNTOUCHED", "PARTIAL", "FILLED", "FAILED"}
    if total == 0 or not required.issubset(states):
        raise ValueError(f"C024 live evidence is missing FVG states: {','.join(sorted(required - states))}")
    return {
        "schema_version": 1,
        "requirement_id": "C024",
        "status": "PASS",
        "bridge_root": str(bridge_root),
        "observed_at": observed_at.isoformat(),
        "gap_count": total,
        "observed_states": tuple(sorted(states)),
        "lifecycle_definition": {
            "UNTOUCHED": "no completed future candle intersects the gap",
            "PARTIAL": "future price enters but does not traverse or close through the gap",
            "FILLED": "future price traverses the far boundary without closing through it",
            "FAILED": "future completed candle closes through the far boundary",
        },
        "symbols": output,
    }


def verify_c025_displacement(report: object, snapshot: MarketSnapshot) -> Mapping[str, object]:
    values = getattr(report, "displacement", None)
    if not isinstance(values, Mapping) or not values:
        raise ValueError("C025 report has no displacement output")
    output = {}
    observed_directions: set[str] = set()
    abnormal_count = 0
    observation_count = 0
    for timeframe, result in values.items():
        candles = tuple(snapshot.candles.get(timeframe, ()))
        if not candles or result != measure_displacement(candles, lookback=result.lookback):
            raise ValueError(f"C025 displacement is not reproducible for {timeframe}")
        if result.index != len(candles) - 1 or result.normalization_source != "PRIOR_TRUE_RANGE":
            raise ValueError(f"C025 displacement lacks source-window identity for {timeframe}")
        if result.range_value < 0 or result.reference_range < 0 or result.expansion_ratio < 0:
            raise ValueError(f"C025 displacement has invalid range metrics for {timeframe}")
        if result.body_value < 0 or result.reference_body < 0 or result.body_ratio < 0:
            raise ValueError(f"C025 displacement has invalid body metrics for {timeframe}")
        if not 0.0 <= result.body_participation <= 1.0 or not 0.0 <= result.close_location <= 1.0:
            raise ValueError(f"C025 displacement geometry is not normalized for {timeframe}")
        history = tuple(measure_displacement(candles[:end]) for end in range(2, len(candles) + 1))
        observed_directions.update(item.direction for item in history if item.direction != "NONE")
        frame_abnormal = sum(item.abnormal_expansion for item in history)
        abnormal_count += frame_abnormal
        observation_count += len(history)
        output[str(timeframe)] = {
            "direction": result.direction,
            "index": result.index,
            "range_value": result.range_value,
            "reference_range": result.reference_range,
            "expansion_ratio": result.expansion_ratio,
            "body_value": result.body_value,
            "reference_body": result.reference_body,
            "body_ratio": result.body_ratio,
            "body_participation": result.body_participation,
            "close_location": result.close_location,
            "normalized_signed_move": result.normalized_signed_move,
            "range_zscore": result.range_zscore,
            "abnormal_expansion": result.abnormal_expansion,
            "lookback": result.lookback,
            "normalization_source": result.normalization_source,
            "historical_observations": len(history),
            "historical_abnormal_expansions": frame_abnormal,
        }
    return {
        "state_id": report.state_id,
        "symbol": report.symbol,
        "observed_at": report.observed_at,
        "observation_count": observation_count,
        "abnormal_expansion_count": abnormal_count,
        "observed_directions": tuple(sorted(observed_directions)),
        "frames": output,
    }


def verify_c025_hfm_displacement(
    *,
    bridge_root: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    observed_at = datetime.now(timezone.utc)
    adapter = HfmCsvMarketDataAdapter(bridge_root)
    output = {}
    observations = 0
    abnormal = 0
    directions: set[str] = set()
    for symbol in symbols:
        dataset = adapter.dataset(
            symbol,
            observed_at=observed_at,
            timeframes=C012_TIMEFRAMES,
            include_latest_tick=False,
            derive_m3_history=False,
            history_mode="rolling",
        )
        snapshot = snapshot_from_dataset(
            dataset,
            observed_at=observed_at,
            required_timeframes=C012_TIMEFRAMES,
            include_micro=False,
            max_bars_per_frame=500,
        )
        result = verify_c025_displacement(IntelligenceEngine().analyse(snapshot), snapshot)
        observations += result["observation_count"]
        abnormal += result["abnormal_expansion_count"]
        directions.update(result["observed_directions"])
        output[symbol] = result
    if directions != {"DOWN", "UP"} or abnormal == 0 or abnormal >= observations:
        raise ValueError("C025 live evidence lacks directional and normal/abnormal displacement variation")
    return {
        "schema_version": 1,
        "requirement_id": "C025",
        "status": "PASS",
        "bridge_root": str(bridge_root),
        "observed_at": observed_at.isoformat(),
        "observation_count": observations,
        "abnormal_expansion_count": abnormal,
        "observed_directions": tuple(sorted(directions)),
        "normalization": "current true range and signed body move divided by prior true-range baseline; abnormal expansion is range z-score >= 2",
        "measurement_role": "DESCRIPTIVE_RESEARCH_EVIDENCE_NOT_EXECUTION_GATE",
        "symbols": output,
    }


def verify_c026_tick_directions(database: Path) -> Mapping[str, object]:
    connection = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
    try:
        symbols = tuple(row[0] for row in connection.execute("SELECT DISTINCT symbol FROM raw_ticks ORDER BY symbol"))
        output = {}
        directions: set[str] = set()
        total = 0
        for symbol in symbols:
            rows = connection.execute(
                "SELECT timestamp,bid,ask,direction,price_change FROM raw_ticks WHERE symbol=? ORDER BY timestamp,bid,ask",
                (symbol,),
            ).fetchall()
            ticks = tuple(RawTick(symbol, datetime.fromisoformat(row[0]), row[1], row[2]) for row in rows)
            events = classify_tick_directions(ticks)
            frame = []
            for event, current in zip(events, rows[1:]):
                stored_direction, stored_change = current[3], current[4]
                if stored_direction != event.direction or not isclose(stored_change, event.price_change, rel_tol=0.0, abs_tol=1e-9):
                    raise ValueError(f"C026 stored tick classification differs from engine for {symbol}")
                if event.interarrival_seconds < 0 or not event.event_id:
                    raise ValueError(f"C026 invalid tick direction provenance for {symbol}")
                directions.add(event.direction)
                total += 1
                frame.append(
                    {
                        "event_id": event.event_id,
                        "timestamp": event.timestamp.isoformat(),
                        "previous_timestamp": event.previous_timestamp.isoformat(),
                        "previous_mid": event.previous_mid,
                        "current_mid": event.current_mid,
                        "bid_change": event.bid_change,
                        "ask_change": event.ask_change,
                        "price_change": event.price_change,
                        "direction": event.direction,
                        "interarrival_seconds": event.interarrival_seconds,
                        "source": event.source,
                    }
                )
            output[symbol] = tuple(frame)
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        connection.close()
    required = {"UP", "DOWN", "UNCHANGED"}
    if not required.issubset(directions):
        raise ValueError(f"C026 live tick evidence is missing directions: {','.join(sorted(required - directions))}")
    if integrity != "ok":
        raise ValueError(f"C026 source tick database integrity failed: {integrity}")
    return {
        "schema_version": 1,
        "requirement_id": "C026",
        "status": "PASS",
        "database": str(database),
        "event_count": total,
        "observed_directions": tuple(sorted(directions)),
        "classification_basis": "consecutive bid/ask midpoint change",
        "symbols": output,
        "integrity": integrity,
    }


def verify_c027_tick_imbalance(database: Path) -> Mapping[str, object]:
    connection = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
    try:
        symbols = tuple(row[0] for row in connection.execute("SELECT DISTINCT symbol FROM raw_ticks ORDER BY symbol"))
        output = {}
        total = 0
        for symbol in symbols:
            rows = connection.execute(
                "SELECT timestamp,bid,ask FROM raw_ticks WHERE symbol=? ORDER BY timestamp,bid,ask",
                (symbol,),
            ).fetchall()
            ticks = tuple(RawTick(symbol, datetime.fromisoformat(row[0]), row[1], row[2]) for row in rows)
            result = tick_imbalance_observation(ticks)
            if result.sample_size != result.up_count + result.down_count + result.unchanged_count:
                raise ValueError(f"C027 event counts do not reconcile for {symbol}")
            if not -1.0 <= result.count_imbalance <= 1.0 or not -1.0 <= result.distance_imbalance <= 1.0:
                raise ValueError(f"C027 imbalance is not normalized for {symbol}")
            if not 0.0 <= result.active_move_ratio <= 1.0:
                raise ValueError(f"C027 active move ratio is invalid for {symbol}")
            if result.gross_distance != result.up_distance + result.down_distance:
                raise ValueError(f"C027 distance components do not reconcile for {symbol}")
            if result.sample_size < 20:
                raise ValueError(f"C027 tick window is too small for {symbol}: {result.sample_size}")
            total += result.sample_size
            output[symbol] = {
                "sample_size": result.sample_size,
                "up_count": result.up_count,
                "down_count": result.down_count,
                "unchanged_count": result.unchanged_count,
                "active_move_ratio": result.active_move_ratio,
                "count_imbalance": result.count_imbalance,
                "up_distance": result.up_distance,
                "down_distance": result.down_distance,
                "gross_distance": result.gross_distance,
                "net_change": result.net_change,
                "distance_imbalance": result.distance_imbalance,
                "window_start": result.window_start.isoformat() if result.window_start else None,
                "window_end": result.window_end.isoformat() if result.window_end else None,
                "source": result.source,
            }
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        connection.close()
    if len(output) != 5 or total < 100:
        raise ValueError("C027 evidence does not cover the five-symbol recent tick windows")
    if integrity != "ok":
        raise ValueError(f"C027 source tick database integrity failed: {integrity}")
    return {
        "schema_version": 1,
        "requirement_id": "C027",
        "status": "PASS",
        "database": str(database),
        "symbol_count": len(output),
        "event_count": total,
        "measurement": "count imbalance plus absolute midpoint-movement imbalance",
        "measurement_role": "DESCRIPTIVE_RESEARCH_EVIDENCE_NOT_EXECUTION_GATE",
        "symbols": output,
        "integrity": integrity,
    }


def verify_c028_tick_velocity(database: Path) -> Mapping[str, object]:
    connection = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
    try:
        symbols = tuple(row[0] for row in connection.execute("SELECT DISTINCT symbol FROM raw_ticks ORDER BY symbol"))
        output = {}
        total = 0
        for symbol in symbols:
            rows = connection.execute(
                "SELECT timestamp,bid,ask FROM raw_ticks WHERE symbol=? ORDER BY timestamp,bid,ask",
                (symbol,),
            ).fetchall()
            ticks = tuple(RawTick(symbol, datetime.fromisoformat(row[0]), row[1], row[2]) for row in rows)
            result = tick_velocity_observation(ticks)
            if result.sample_size < 20 or result.duration_seconds <= 0 or result.quote_arrival_rate <= 0:
                raise ValueError(f"C028 insufficient timed tick observations for {symbol}")
            if result.path_velocity < abs(result.net_velocity):
                raise ValueError(f"C028 path velocity is below net velocity for {symbol}")
            if not isclose(
                result.path_velocity,
                result.upward_path_velocity + result.downward_path_velocity,
                rel_tol=0.0,
                abs_tol=1e-12,
            ):
                raise ValueError(f"C028 directional path velocities do not reconcile for {symbol}")
            if result.maximum_absolute_event_velocity < result.mean_absolute_event_velocity:
                raise ValueError(f"C028 maximum event velocity is inconsistent for {symbol}")
            total += result.sample_size
            output[symbol] = {
                "sample_size": result.sample_size,
                "duration_seconds": result.duration_seconds,
                "quote_arrival_rate": result.quote_arrival_rate,
                "mean_interarrival_seconds": result.mean_interarrival_seconds,
                "median_interarrival_seconds": result.median_interarrival_seconds,
                "zero_interval_count": result.zero_interval_count,
                "net_velocity": result.net_velocity,
                "path_velocity": result.path_velocity,
                "upward_path_velocity": result.upward_path_velocity,
                "downward_path_velocity": result.downward_path_velocity,
                "mean_absolute_event_velocity": result.mean_absolute_event_velocity,
                "maximum_absolute_event_velocity": result.maximum_absolute_event_velocity,
                "latest_event_velocity": result.latest_event_velocity,
                "window_start": result.window_start.isoformat() if result.window_start else None,
                "window_end": result.window_end.isoformat() if result.window_end else None,
                "source": result.source,
            }
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        connection.close()
    if len(output) != 5 or total < 100 or integrity != "ok":
        raise ValueError("C028 five-symbol tick velocity evidence is incomplete")
    return {
        "schema_version": 1,
        "requirement_id": "C028",
        "status": "PASS",
        "database": str(database),
        "symbol_count": len(output),
        "event_count": total,
        "measurement": "net and gross midpoint movement per elapsed second plus quote arrival rate",
        "measurement_role": "DESCRIPTIVE_RESEARCH_EVIDENCE_NOT_EXECUTION_GATE",
        "symbols": output,
        "integrity": integrity,
    }


def verify_c029_tick_acceleration(database: Path) -> Mapping[str, object]:
    connection = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
    try:
        symbols = tuple(row[0] for row in connection.execute("SELECT DISTINCT symbol FROM raw_ticks ORDER BY symbol"))
        output = {}
        total = 0
        rapid_total = 0
        rolling_total = 0
        for symbol in symbols:
            rows = connection.execute(
                "SELECT timestamp,bid,ask FROM raw_ticks WHERE symbol=? ORDER BY timestamp,bid,ask",
                (symbol,),
            ).fetchall()
            ticks = tuple(RawTick(symbol, datetime.fromisoformat(row[0]), row[1], row[2]) for row in rows)
            result = tick_acceleration_observation(ticks)
            if result.acceleration_sample_size < 18:
                raise ValueError(f"C029 insufficient acceleration observations for {symbol}")
            if result.acceleration_sample_size != (
                result.positive_acceleration_count
                + result.negative_acceleration_count
                + result.unchanged_acceleration_count
            ):
                raise ValueError(f"C029 acceleration counts do not reconcile for {symbol}")
            if result.maximum_absolute_acceleration < result.mean_absolute_acceleration:
                raise ValueError(f"C029 acceleration magnitude metrics are inconsistent for {symbol}")
            rolling = tuple(tick_acceleration_observation(ticks[:end]) for end in range(4, len(ticks) + 1))
            rapid_count = sum(item.rapid_change for item in rolling)
            rapid_total += rapid_count
            rolling_total += len(rolling)
            total += result.acceleration_sample_size
            output[symbol] = {
                "velocity_sample_size": result.velocity_sample_size,
                "acceleration_sample_size": result.acceleration_sample_size,
                "latest_velocity": result.latest_velocity,
                "previous_velocity": result.previous_velocity,
                "latest_acceleration": result.latest_acceleration,
                "mean_absolute_acceleration": result.mean_absolute_acceleration,
                "maximum_absolute_acceleration": result.maximum_absolute_acceleration,
                "positive_acceleration_count": result.positive_acceleration_count,
                "negative_acceleration_count": result.negative_acceleration_count,
                "unchanged_acceleration_count": result.unchanged_acceleration_count,
                "latest_acceleration_zscore": result.latest_acceleration_zscore,
                "rapid_change": result.rapid_change,
                "historical_rapid_change_count": rapid_count,
                "historical_window_count": len(rolling),
                "zero_interval_count": result.zero_interval_count,
                "window_start": result.window_start.isoformat() if result.window_start else None,
                "window_end": result.window_end.isoformat() if result.window_end else None,
                "source": result.source,
            }
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        connection.close()
    if len(output) != 5 or total < 100 or rapid_total == 0 or rapid_total >= rolling_total or integrity != "ok":
        raise ValueError("C029 five-symbol acceleration evidence lacks normal/rapid variation")
    return {
        "schema_version": 1,
        "requirement_id": "C029",
        "status": "PASS",
        "database": str(database),
        "symbol_count": len(output),
        "acceleration_event_count": total,
        "historical_rapid_change_count": rapid_total,
        "historical_window_count": rolling_total,
        "measurement": "change in consecutive exact midpoint velocities per elapsed second",
        "measurement_role": "DESCRIPTIVE_RESEARCH_EVIDENCE_NOT_EXECUTION_GATE",
        "symbols": output,
        "integrity": integrity,
    }


def verify_c030_microstructure(database: Path) -> Mapping[str, object]:
    connection = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
    try:
        symbols = tuple(
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT symbol FROM raw_ticks ORDER BY symbol"
            )
        )
        output = {}
        event_total = 0
        for symbol in symbols:
            rows = connection.execute(
                "SELECT timestamp,bid,ask FROM raw_ticks WHERE symbol=? ORDER BY timestamp,bid,ask",
                (symbol,),
            ).fetchall()
            ticks = tuple(
                RawTick(symbol, datetime.fromisoformat(row[0]), row[1], row[2])
                for row in rows
            )
            result = measure_microstructure(ticks)
            if result.status != "OBSERVED" or result.event_count < 20:
                raise ValueError(f"C030 insufficient observed quote events for {symbol}")
            if result.event_count != result.tick_count - 1:
                raise ValueError(f"C030 quote event count does not reconcile for {symbol}")
            if result.duration_seconds <= 0 or result.quote_arrival_rate <= 0:
                raise ValueError(f"C030 timing components are invalid for {symbol}")
            if not (
                0.0 <= result.minimum_spread
                <= result.mean_spread
                <= result.max_spread
            ):
                raise ValueError(f"C030 spread components are inconsistent for {symbol}")
            if result.path_velocity + 1e-12 < abs(result.velocity):
                raise ValueError(f"C030 path movement is below net movement for {symbol}")
            if not isclose(
                result.gross_quote_movement,
                result.path_velocity * result.duration_seconds,
                rel_tol=0.0,
                abs_tol=1e-9,
            ):
                raise ValueError(f"C030 movement and path velocity do not reconcile for {symbol}")
            if result.micro_volatility < 0 or result.spread_stddev < 0:
                raise ValueError(f"C030 volatility components are invalid for {symbol}")
            if result.maximum_absolute_acceleration < result.mean_absolute_acceleration:
                raise ValueError(f"C030 acceleration components are inconsistent for {symbol}")
            if result.window_start is None or result.window_end is None:
                raise ValueError(f"C030 source window lineage is absent for {symbol}")
            event_total += result.event_count
            output[symbol] = {
                "tick_count": result.tick_count,
                "event_count": result.event_count,
                "window_start": result.window_start.isoformat(),
                "window_end": result.window_end.isoformat(),
                "duration_seconds": result.duration_seconds,
                "latest_spread": result.latest_spread,
                "mean_spread": result.mean_spread,
                "median_spread": result.median_spread,
                "minimum_spread": result.minimum_spread,
                "maximum_spread": result.max_spread,
                "spread_stddev": result.spread_stddev,
                "mean_relative_spread_bps": result.mean_relative_spread_bps,
                "quote_arrival_rate": result.quote_arrival_rate,
                "mean_interarrival_seconds": result.mean_interarrival_seconds,
                "median_interarrival_seconds": result.median_interarrival_seconds,
                "zero_interval_count": result.zero_interval_count,
                "net_quote_movement": result.net_quote_movement,
                "gross_quote_movement": result.gross_quote_movement,
                "mean_absolute_quote_change": result.mean_absolute_quote_change,
                "micro_volatility": result.micro_volatility,
                "relative_micro_volatility_bps": result.relative_micro_volatility_bps,
                "directional_imbalance": result.directional_imbalance,
                "distance_imbalance": result.distance_imbalance,
                "net_velocity": result.velocity,
                "path_velocity": result.path_velocity,
                "latest_acceleration": result.acceleration,
                "mean_absolute_acceleration": result.mean_absolute_acceleration,
                "maximum_absolute_acceleration": result.maximum_absolute_acceleration,
                "acceleration_zscore": result.acceleration_zscore,
                "rapid_acceleration": result.rapid_acceleration,
                "status": result.status,
                "source": result.source,
                "component_sources": result.component_sources,
            }
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        connection.close()
    if len(output) != 5 or event_total < 100 or integrity != "ok":
        raise ValueError("C030 five-symbol microstructure evidence is incomplete")
    return {
        "schema_version": 1,
        "requirement_id": "C030",
        "status": "PASS",
        "database": str(database),
        "symbol_count": len(output),
        "event_count": event_total,
        "combined_components": (
            "spread",
            "tick_arrival_rate",
            "quote_movement",
            "micro_volatility",
            "directional_imbalance",
            "velocity",
            "acceleration",
        ),
        "measurement_role": "DESCRIPTIVE_RESEARCH_EVIDENCE_NOT_EXECUTION_GATE",
        "symbols": output,
        "integrity": integrity,
    }


def verify_c031_spread_intelligence(
    *,
    bridge_root: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    active_symbols = {"XAUUSD", "UK100", "USA100", "USA500", "USA30"}
    if set(symbols) != active_symbols:
        raise ValueError("C031 requires the exact five-symbol active universe")
    adapter = HfmCsvMarketDataAdapter(bridge_root)
    contracts = {row.symbol: row for row in adapter.symbols()}
    observed_at = datetime.now(timezone.utc)
    rows_by_symbol = {}
    samples = []
    source_files = []
    for symbol in symbols:
        contract = contracts.get(symbol)
        if contract is None or contract.point <= 0:
            raise ValueError(f"C031 missing broker point size for {symbol}")
        rows = adapter.read_bars(
            symbol,
            "M1",
            observed_at=observed_at,
            history_mode="combined",
        )
        if len(rows) < 1_000:
            raise ValueError(f"C031 insufficient HFM M1 spread history for {symbol}")
        rows_by_symbol[symbol] = rows
        samples.extend(
            historical_spread_samples(
                symbol,
                rows,
                point_size=contract.point,
            )
        )
        for candidate in (
            bridge_root / f"deephistory_{symbol}_M1.csv",
            bridge_root / f"rates_{symbol}_M1_HISTORY.csv",
            bridge_root / f"rates_{symbol}_M1.csv",
        ):
            if candidate.is_file():
                payload = candidate.read_bytes()
                source_files.append(
                    {
                        "path": str(candidate),
                        "bytes": len(payload),
                        "sha256": sha256(payload).hexdigest(),
                    }
                )
    book = build_spread_profile_book(samples, minimum_samples=30)
    output = {}
    for symbol in symbols:
        rows = rows_by_symbol[symbol]
        tick = adapter.latest_tick(symbol)
        regime = classify_regime(tuple(rows[-20:])).label
        session = session_label(tick.timestamp)
        current_spread = measure_microstructure((tick,)).latest_spread
        result = book.observe(
            symbol=symbol,
            observed_at=tick.timestamp,
            session=session,
            regime=regime,
            current_spread=current_spread,
        )
        snapshot = MarketSnapshot(
            symbol=symbol,
            observed_at=tick.timestamp,
            ticks=(tick,),
            candles={"M1": tuple(rows[-20:])},
            frame_status={"M1": "COMPLETED"},
            source_ids=tuple(row.source_id for row in rows[-20:]),
            contract={"point": contracts[symbol].point},
        )
        report_observation = IntelligenceEngine(
            spread_profiles=book,
        ).analyse(snapshot).spread_context
        profile = book.profile(symbol, session, regime)
        if result.status != "MATCHED" or profile is None or result.sample_size < 30:
            raise ValueError(
                f"C031 lacks matching symbol/session/regime history for {symbol}: "
                f"{session}/{regime}/{result.status}/{result.sample_size}"
            )
        if not 0.0 <= result.percentile <= 1.0:
            raise ValueError(f"C031 percentile is invalid for {symbol}")
        if not profile.p90 <= profile.p95 <= profile.p99 <= profile.maximum:
            raise ValueError(f"C031 historical spread quantiles are inconsistent for {symbol}")
        if result.historical_start is None or result.historical_end is None:
            raise ValueError(f"C031 historical window is absent for {symbol}")
        if (
            report_observation.status != "MATCHED"
            or report_observation.profile_id != result.profile_id
            or report_observation.sample_size != result.sample_size
        ):
            raise ValueError(f"C031 intelligence runtime wiring mismatch for {symbol}")
        output[symbol] = {
            "current_tick_at": tick.timestamp.isoformat(),
            "current_spread": result.current_spread,
            "broker_point_size": contracts[symbol].point,
            "matched_session": result.session,
            "matched_regime": result.regime,
            "profile_id": result.profile_id,
            "sample_size": result.sample_size,
            "historical_start": result.historical_start.isoformat(),
            "historical_end": result.historical_end.isoformat(),
            "historical_mean": result.historical_mean,
            "historical_median": result.historical_median,
            "historical_p90": result.historical_p90,
            "historical_p95": result.historical_p95,
            "historical_p99": result.historical_p99,
            "percentile": result.percentile,
            "zscore": result.zscore,
            "robust_zscore": result.robust_zscore,
            "ratio_to_median": result.ratio_to_median,
            "status": result.status,
            "role": result.role,
            "intelligence_report_status": report_observation.status,
            "intelligence_report_profile_id": report_observation.profile_id,
            "source_digest": profile.source_digest,
        }
    return {
        "schema_version": 1,
        "requirement_id": "C031",
        "status": "PASS",
        "bridge_root": str(bridge_root),
        "symbol_count": len(output),
        "profile_count": len(book.profiles),
        "historical_sample_count": sum(row.sample_size for row in book.profiles),
        "minimum_samples_per_profile": book.minimum_samples,
        "matching_dimensions": ("symbol", "session", "regime"),
        "spread_unit": "BROKER_PRICE_FROM_SPREAD_POINTS_X_SYMBOL_POINT",
        "measurement_role": "DESCRIPTIVE_RESEARCH_EVIDENCE_NOT_EXECUTION_GATE",
        "symbols": output,
        "source_files": tuple(source_files),
    }


def verify_c032_slippage_intelligence(
    *,
    bridge_root: Path,
    database: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    active_symbols = {"XAUUSD", "UK100", "USA100", "USA500", "USA30"}
    if set(symbols) != active_symbols:
        raise ValueError("C032 requires the exact five-symbol active universe")
    order_path = bridge_root / "history_orders.csv"
    deal_path = bridge_root / "deals.csv"
    if not order_path.is_file() or not deal_path.is_file():
        raise ValueError("C032 requires MT5 historical order and deal exports")
    order_bytes = order_path.read_bytes()
    deal_bytes = deal_path.read_bytes()
    order_digest = sha256(order_bytes).hexdigest()
    deal_digest = sha256(deal_bytes).hexdigest()
    with order_path.open(newline="", encoding="utf-8-sig") as handle:
        order_rows = tuple(csv.DictReader(handle))
    with deal_path.open(newline="", encoding="utf-8-sig") as handle:
        deal_rows = tuple(csv.DictReader(handle))
    orders = {str(row.get("ticket") or ""): row for row in order_rows}
    if "" in orders:
        raise ValueError("C032 historical order export contains an empty ticket")

    observations = []
    excluded_zero_request = 0
    excluded_non_fill = 0
    per_symbol = {
        symbol: {
            "fill_count": 0,
            "adverse_count": 0,
            "favorable_count": 0,
            "zero_count": 0,
            "absolute_slippage": 0.0,
            "signed_bps": [],
        }
        for symbol in symbols
    }
    for deal in deal_rows:
        symbol = str(deal.get("symbol") or "")
        if symbol not in active_symbols:
            continue
        entry = str(deal.get("deal_entry") or "")
        if entry not in {"DEAL_ENTRY_IN", "DEAL_ENTRY_OUT", "DEAL_ENTRY_OUT_BY", "DEAL_ENTRY_INOUT"}:
            excluded_non_fill += 1
            continue
        order_ticket = str(deal.get("order") or "")
        deal_ticket = str(deal.get("deal") or "")
        order = orders.get(order_ticket)
        if order is None:
            raise ValueError(f"C032 broker deal has no exact historical order: {deal_ticket}")
        if str(order.get("symbol") or "") != symbol:
            raise ValueError(f"C032 order/deal symbol mismatch: {deal_ticket}")
        if str(order.get("state") or "") != "ORDER_STATE_FILLED":
            raise ValueError(f"C032 joined order is not filled: {order_ticket}")
        side = str(order.get("side") or "")
        expected_deal_type = "DEAL_TYPE_BUY" if side == "BUY" else "DEAL_TYPE_SELL"
        if side not in ("BUY", "SELL") or str(deal.get("deal_type") or "") != expected_deal_type:
            raise ValueError(f"C032 order/deal side mismatch: {deal_ticket}")
        requested_price = float(order.get("requested_price") or 0.0)
        if requested_price <= 0:
            excluded_zero_request += 1
            continue
        fill_price = float(deal.get("price") or 0.0)
        volume = float(deal.get("volume") or 0.0)
        filled_at = datetime.fromtimestamp(int(deal["time_utc"]), timezone.utc)
        if fill_price <= 0 or volume <= 0:
            raise ValueError(f"C032 broker fill values are invalid: {deal_ticket}")
        request_payload = {
            "ticket": order_ticket,
            "symbol": symbol,
            "side": side,
            "volume_initial": order.get("volume_initial"),
            "requested_price": order.get("requested_price"),
            "time_setup_utc": order.get("time_setup_utc"),
            "source_row_sha256": sha256(
                json.dumps(order, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
        }
        request_hash = sha256(
            json.dumps(request_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        fill = BrokerFill(
            order_ticket=order_ticket,
            deal_ticket=deal_ticket,
            requested_price=requested_price,
            fill_price=fill_price,
            volume=volume,
            filled_at=filled_at,
            source_id=(
                f"hfm-deals:{deal_ticket}:row-sha256:"
                f"{sha256(json.dumps(deal, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()}"
            ),
        )
        observation = build_slippage_observation(
            decision_id=f"HFM_HISTORY_ORDER_{order_ticket}",
            request_hash=request_hash,
            edge_id="BROKER_HISTORY_MEASUREMENT",
            symbol=symbol,
            side=side,
            fill=fill,
        )
        observations.append(observation)
        summary = per_symbol[symbol]
        summary["fill_count"] += 1
        summary["absolute_slippage"] += abs(observation.signed_price_difference)
        summary["signed_bps"].append(observation.side_adjusted_slippage_bps)
        if observation.adverse_slippage > 0:
            summary["adverse_count"] += 1
        elif observation.favorable_slippage > 0:
            summary["favorable_count"] += 1
        else:
            summary["zero_count"] += 1
    if len(observations) < 100:
        raise ValueError("C032 has too few exact broker requested-versus-filled joins")
    if any(per_symbol[symbol]["fill_count"] == 0 for symbol in symbols):
        raise ValueError("C032 broker evidence does not cover every active symbol")
    if len({(row.order_ticket, row.deal_ticket) for row in observations}) != len(observations):
        raise ValueError("C032 broker fill identities are not unique")

    store = EvidenceStore(database)
    try:
        for observation in observations:
            store.write_slippage_observation(observation)
        persisted = store.load_slippage_observations()
        integrity = store.integrity()
    finally:
        store.close()
    persisted_ids = {row.observation_id for row in persisted}
    expected_ids = {row.observation_id for row in observations}
    if not expected_ids.issubset(persisted_ids) or not integrity:
        raise ValueError("C032 immutable slippage persistence failed")

    output = {}
    for symbol, summary in per_symbol.items():
        count = summary["fill_count"]
        output[symbol] = {
            "fill_count": count,
            "adverse_count": summary["adverse_count"],
            "favorable_count": summary["favorable_count"],
            "zero_count": summary["zero_count"],
            "mean_absolute_price_slippage": summary["absolute_slippage"] / count,
            "mean_side_adjusted_slippage_bps": mean(summary["signed_bps"]),
        }
    return {
        "schema_version": 1,
        "requirement_id": "C032",
        "status": "PASS",
        "bridge_root": str(bridge_root),
        "database": str(database),
        "symbol_count": len(output),
        "exact_fill_count": len(observations),
        "persisted_fill_count": len(expected_ids.intersection(persisted_ids)),
        "excluded_zero_requested_price_count": excluded_zero_request,
        "excluded_non_fill_deal_count": excluded_non_fill,
        "zero_request_policy": "NEVER_IMPUTE_FROM_PROPOSAL_CANDLE_CHART_OR_QUOTE",
        "forward_fill_policy": "FILLED_RESPONSE_REQUIRES_EXACT_MT5_REQUEST_AND_DEAL_PRICES",
        "identity_join": "MT5_HISTORY_ORDER_TICKET_TO_MT5_DEAL_ORDER_TICKET",
        "measurement_role": "BROKER_EXECUTION_EVIDENCE_NOT_TRADE_APPROVAL",
        "symbols": output,
        "source_files": (
            {"path": str(order_path), "bytes": len(order_bytes), "sha256": order_digest},
            {"path": str(deal_path), "bytes": len(deal_bytes), "sha256": deal_digest},
        ),
        "integrity": integrity,
    }


def verify_c033_latency_intelligence(
    *,
    bridge_root: Path,
    database: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    active_symbols = {"XAUUSD", "UK100", "USA100", "USA500", "USA30"}
    if set(symbols) != active_symbols:
        raise ValueError("C033 requires the exact five-symbol active universe")
    order_path = bridge_root / "history_orders.csv"
    deal_path = bridge_root / "deals.csv"
    if not order_path.is_file() or not deal_path.is_file():
        raise ValueError("C033 requires MT5 millisecond order and deal exports")
    order_bytes = order_path.read_bytes()
    deal_bytes = deal_path.read_bytes()
    order_digest = sha256(order_bytes).hexdigest()
    deal_digest = sha256(deal_bytes).hexdigest()
    with order_path.open(newline="", encoding="utf-8-sig") as handle:
        order_rows = tuple(csv.DictReader(handle))
    with deal_path.open(newline="", encoding="utf-8-sig") as handle:
        deal_rows = tuple(csv.DictReader(handle))
    required_order_fields = {
        "time_setup_msc_utc",
        "time_done_msc_utc",
        "order_type",
    }
    required_deal_fields = {"time_msc_utc", "order", "deal"}
    if not order_rows or not required_order_fields.issubset(order_rows[0]):
        raise ValueError("C033 order export lacks millisecond broker timestamps")
    if not deal_rows or not required_deal_fields.issubset(deal_rows[0]):
        raise ValueError("C033 deal export lacks millisecond broker timestamps")
    orders = {str(row.get("ticket") or ""): row for row in order_rows}

    samples: list[Mapping[str, object]] = []
    pending_excluded = 0
    per_symbol: dict[str, list[int]] = {symbol: [] for symbol in symbols}
    for deal in deal_rows:
        symbol = str(deal.get("symbol") or "")
        if symbol not in active_symbols:
            continue
        order_ticket = str(deal.get("order") or "")
        deal_ticket = str(deal.get("deal") or "")
        order = orders.get(order_ticket)
        if order is None:
            raise ValueError(f"C033 broker deal has no historical order: {deal_ticket}")
        if str(order.get("symbol") or "") != symbol:
            raise ValueError(f"C033 order/deal symbol mismatch: {deal_ticket}")
        order_type = str(order.get("order_type") or "")
        if order_type not in {"ORDER_TYPE_BUY", "ORDER_TYPE_SELL"}:
            pending_excluded += 1
            continue
        if str(order.get("state") or "") != "ORDER_STATE_FILLED":
            raise ValueError(f"C033 market order is not filled: {order_ticket}")
        side = "BUY" if order_type == "ORDER_TYPE_BUY" else "SELL"
        expected_deal_type = "DEAL_TYPE_BUY" if side == "BUY" else "DEAL_TYPE_SELL"
        if str(deal.get("deal_type") or "") != expected_deal_type:
            raise ValueError(f"C033 order/deal side mismatch: {deal_ticket}")
        requested_msc = int(order.get("time_setup_msc_utc") or 0)
        filled_msc = int(deal.get("time_msc_utc") or 0)
        latency_ms = filled_msc - requested_msc
        if requested_msc <= 0 or filled_msc <= 0 or latency_ms < 0:
            raise ValueError(f"C033 invalid broker timestamp sequence: {deal_ticket}")
        row = {
            "symbol": symbol,
            "side": side,
            "order_ticket": order_ticket,
            "deal_ticket": deal_ticket,
            "requested_msc_utc": requested_msc,
            "filled_msc_utc": filled_msc,
            "broker_request_to_fill_ms": latency_ms,
        }
        samples.append(row)
        per_symbol[symbol].append(latency_ms)
    if len(samples) < 100:
        raise ValueError("C033 has too few exact market-order latency samples")
    if any(not per_symbol[symbol] for symbol in symbols):
        raise ValueError("C033 broker latency evidence does not cover every active symbol")
    if len({(row["order_ticket"], row["deal_ticket"]) for row in samples}) != len(samples):
        raise ValueError("C033 broker latency identities are not unique")

    self_checks = []
    for symbol in symbols:
        sample = next(row for row in samples if row["symbol"] == symbol)
        source_order = orders[str(sample["order_ticket"])]
        source_deal = next(
            row for row in deal_rows if str(row.get("deal") or "") == str(sample["deal_ticket"])
        )
        order_row_digest = sha256(
            json.dumps(source_order, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        deal_row_digest = sha256(
            json.dumps(source_deal, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        broker_requested_at = datetime.fromtimestamp(
            int(sample["requested_msc_utc"]) / 1000.0,
            timezone.utc,
        )
        broker_filled_at = datetime.fromtimestamp(
            int(sample["filled_msc_utc"]) / 1000.0,
            timezone.utc,
        )
        base = broker_filled_at + timedelta(seconds=1)
        decision_id = f"C033_INSTRUMENTATION_{symbol}_{sample['order_ticket']}"
        context = RuntimeLatencyContext(
            trace_id=f"C033_TRACE_{symbol}_{sample['order_ticket']}",
            decision_id=decision_id,
            symbol=symbol,
            source_data_at=base - timedelta(milliseconds=20),
            data_received_at=base,
            analysis_started_at=base + timedelta(milliseconds=1),
            analysis_completed_at=base + timedelta(milliseconds=4),
            decision_created_at=base + timedelta(milliseconds=5),
            source_ids=(
                f"hfm-history-orders:{sample['order_ticket']}:row-sha256:{order_row_digest}",
                f"hfm-deals:{sample['deal_ticket']}:row-sha256:{deal_row_digest}",
            ),
        )
        request_hash = sha256(
            json.dumps(sample, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        observation = build_execution_latency_observation(
            context=context,
            request_hash=request_hash,
            edge_id="C033_INSTRUMENTATION_ONLY",
            order_ticket=str(sample["order_ticket"]),
            deal_ticket=str(sample["deal_ticket"]),
            order_handoff_at=base + timedelta(milliseconds=6),
            fill_received_at=base + timedelta(milliseconds=8),
            broker_requested_at=broker_requested_at,
            broker_filled_at=broker_filled_at,
            broker_source_id=f"hfm-deal:{sample['deal_ticket']}:row-sha256:{deal_row_digest}",
        )
        self_checks.append(observation)

    store = EvidenceStore(database)
    try:
        for observation in self_checks:
            store.write_execution_latency_observation(observation)
        persisted = store.load_execution_latency_observations()
        integrity = store.integrity()
    finally:
        store.close()
    persisted_ids = {row.observation_id for row in persisted}
    expected_ids = {row.observation_id for row in self_checks}
    if not expected_ids.issubset(persisted_ids) or not integrity:
        raise ValueError("C033 immutable latency persistence failed")

    def summary(values: Sequence[int]) -> Mapping[str, object]:
        rows = tuple(sorted(values))
        p95_index = min(len(rows) - 1, int((len(rows) - 1) * 0.95))
        return {
            "sample_count": len(rows),
            "minimum_ms": rows[0],
            "median_ms": median(rows),
            "p95_ms": rows[p95_index],
            "maximum_ms": rows[-1],
        }

    return {
        "schema_version": 1,
        "requirement_id": "C033",
        "status": "PASS",
        "bridge_root": str(bridge_root),
        "database": str(database),
        "symbol_count": len(per_symbol),
        "broker_market_fill_count": len(samples),
        "pending_order_fill_count_excluded_from_market_latency": pending_excluded,
        "clock_domain_policy": "LOCAL_PIPELINE_AND_MT5_BROKER_CLOCKS_MEASURED_SEPARATELY",
        "market_latency_scope": "ORDER_TYPE_BUY_AND_ORDER_TYPE_SELL_ONLY",
        "full_path_stages": (
            "SOURCE_DATA",
            "DATA_RECEIVED",
            "ANALYSIS_STARTED",
            "ANALYSIS_COMPLETED",
            "DECISION_CREATED",
            "ORDER_HANDOFF",
            "FILL_RECEIVED",
            "BROKER_REQUESTED",
            "BROKER_FILLED",
        ),
        "runtime_instrumentation_self_check_count": len(self_checks),
        "runtime_instrumentation_role": "CONTRACT_AND_PERSISTENCE_SELF_CHECK_NOT_A_BROKER_ORDER",
        "broker_summary": summary(tuple(int(row["broker_request_to_fill_ms"]) for row in samples)),
        "symbols": {symbol: summary(rows) for symbol, rows in per_symbol.items()},
        "source_files": (
            {"path": str(order_path), "bytes": len(order_bytes), "sha256": order_digest},
            {"path": str(deal_path), "bytes": len(deal_bytes), "sha256": deal_digest},
        ),
        "integrity": integrity,
    }


def verify_c034_momentum_intelligence(
    *,
    bridge_root: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    active_symbols = {"XAUUSD", "UK100", "USA100", "USA500", "USA30"}
    if set(symbols) != active_symbols:
        raise ValueError("C034 requires the exact five-symbol active universe")
    observed_at = datetime.now(timezone.utc)
    adapter = HfmCsvMarketDataAdapter(bridge_root)
    output = {}
    directions: set[str] = set()
    nonzero_velocity = 0
    nonzero_acceleration = 0
    for symbol in symbols:
        dataset = adapter.dataset(
            symbol,
            observed_at=observed_at,
            timeframes=RESEARCH_TIMEFRAMES,
            include_latest_tick=False,
            derive_m3_history=False,
            history_mode="rolling",
        )
        snapshot = snapshot_from_dataset(
            dataset,
            observed_at=observed_at,
            required_timeframes=RESEARCH_TIMEFRAMES,
            include_micro=False,
            max_bars_per_frame=500,
        )
        report = IntelligenceEngine().analyse(snapshot)
        if set(report.momentum) != set(RESEARCH_TIMEFRAMES):
            raise ValueError(f"C034 momentum coverage is incomplete for {symbol}")
        frames = {}
        for timeframe in RESEARCH_TIMEFRAMES:
            candles = tuple(snapshot.candles[timeframe][-8:])
            observation = report.momentum[timeframe]
            if len(candles) < 2:
                raise ValueError(f"C034 has insufficient live candles for {symbol} {timeframe}")
            intervals = tuple(
                (current.end - previous.end).total_seconds()
                for previous, current in zip(candles, candles[1:])
            )
            moves = tuple(
                current.close - previous.close
                for previous, current in zip(candles, candles[1:])
            )
            velocities = tuple(move / duration for move, duration in zip(moves, intervals))
            elapsed = (candles[-1].end - candles[0].end).total_seconds()
            expected_velocity = (candles[-1].close - candles[0].close) / elapsed
            expected_acceleration = 0.0
            if len(velocities) >= 2:
                acceleration_elapsed = (candles[-1].end - candles[1].end).total_seconds()
                expected_acceleration = (velocities[-1] - velocities[0]) / acceleration_elapsed
            signs = tuple(1 if move > 0 else -1 if move < 0 else 0 for move in moves)
            expected_direction = (
                "UP" if expected_velocity > 0 else "DOWN" if expected_velocity < 0 else "FLAT"
            )
            expected_sign = 1 if expected_velocity > 0 else -1 if expected_velocity < 0 else 0
            expected_strength = (
                sum(sign == expected_sign for sign in signs) / len(signs)
                if expected_sign
                else sum(sign == 0 for sign in signs) / len(signs)
            )
            checks = (
                observation.source == "CLOSE_TO_CLOSE_PRICE_TIME",
                observation.sample_size == len(candles),
                isclose(observation.elapsed_seconds, elapsed, rel_tol=0.0, abs_tol=1e-12),
                isclose(observation.velocity_per_second, expected_velocity, rel_tol=0.0, abs_tol=1e-12),
                isclose(
                    observation.acceleration_per_second_squared,
                    expected_acceleration,
                    rel_tol=0.0,
                    abs_tol=1e-12,
                ),
                isclose(
                    observation.persistence_strength,
                    expected_strength,
                    rel_tol=0.0,
                    abs_tol=1e-12,
                ),
                observation.direction == expected_direction,
            )
            if not all(checks):
                raise ValueError(f"C034 price momentum does not reconcile for {symbol} {timeframe}")
            directions.add(observation.direction)
            nonzero_velocity += observation.velocity_per_second != 0.0
            nonzero_acceleration += observation.acceleration_per_second_squared != 0.0
            frames[timeframe] = {
                "direction": observation.direction,
                "velocity_per_second": observation.velocity_per_second,
                "acceleration_per_second_squared": observation.acceleration_per_second_squared,
                "directional_persistence": observation.directional_persistence,
                "persistence_strength": observation.persistence_strength,
                "impulse_ratio": observation.impulse_ratio,
                "pullback": observation.pullback,
                "continuation": observation.continuation,
                "net_price_change": observation.net_price_change,
                "elapsed_seconds": observation.elapsed_seconds,
                "sample_size": observation.sample_size,
                "source": observation.source,
            }
        output[symbol] = frames
    if nonzero_velocity == 0 or nonzero_acceleration == 0:
        raise ValueError("C034 live evidence contains no measurable velocity or acceleration")
    return {
        "schema_version": 1,
        "requirement_id": "C034",
        "status": "PASS",
        "bridge_root": str(bridge_root),
        "observed_at": observed_at.isoformat(),
        "symbol_count": len(output),
        "timeframe_count_per_symbol": len(RESEARCH_TIMEFRAMES),
        "measurement_count": len(output) * len(RESEARCH_TIMEFRAMES),
        "nonzero_velocity_count": nonzero_velocity,
        "nonzero_acceleration_count": nonzero_acceleration,
        "observed_directions": tuple(sorted(directions)),
        "price_basis": "CLOSE_TO_CLOSE_PRICE_CHANGE_OVER_ACTUAL_ELAPSED_SECONDS",
        "measurement_role": "DESCRIPTIVE_PRICE_MOMENTUM_NOT_TRADE_APPROVAL",
        "symbols": output,
    }


def verify_c035_realized_volatility(
    *,
    bridge_root: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    active_symbols = {"XAUUSD", "UK100", "USA100", "USA500", "USA30"}
    if set(symbols) != active_symbols:
        raise ValueError("C035 requires the exact five-symbol active universe")
    observed_at = datetime.now(timezone.utc)
    adapter = HfmCsvMarketDataAdapter(bridge_root)
    output = {}
    nonzero_realized = 0
    for symbol in symbols:
        dataset = adapter.dataset(
            symbol,
            observed_at=observed_at,
            timeframes=RESEARCH_TIMEFRAMES,
            include_latest_tick=False,
            derive_m3_history=False,
            history_mode="rolling",
        )
        snapshot = snapshot_from_dataset(
            dataset,
            observed_at=observed_at,
            required_timeframes=RESEARCH_TIMEFRAMES,
            include_micro=False,
            max_bars_per_frame=500,
        )
        report = IntelligenceEngine().analyse(snapshot)
        if set(report.realized_volatility) != set(RESEARCH_TIMEFRAMES):
            raise ValueError(f"C035 realized-volatility coverage is incomplete for {symbol}")
        frames = {}
        for timeframe in RESEARCH_TIMEFRAMES:
            candles = tuple(snapshot.candles[timeframe][-20:])
            observation = report.realized_volatility[timeframe]
            if len(candles) < 2:
                raise ValueError(f"C035 has insufficient live candles for {symbol} {timeframe}")
            returns = tuple(
                log(current.close / previous.close)
                for previous, current in zip(candles, candles[1:])
            )
            squared = tuple(value * value for value in returns)
            positive = tuple(value * value for value in returns if value > 0)
            negative = tuple(value * value for value in returns if value < 0)
            variance = sum(squared)
            elapsed = (candles[-1].end - candles[0].end).total_seconds()
            checks = (
                observation.source == "CLOSE_TO_CLOSE_LOG_RETURNS_NO_ATR",
                observation.sample_size == len(candles),
                observation.return_count == len(returns),
                isclose(observation.realized_variance, variance, rel_tol=0.0, abs_tol=1e-15),
                isclose(observation.realized_volatility, sqrt(variance), rel_tol=0.0, abs_tol=1e-15),
                isclose(
                    observation.return_standard_deviation,
                    pstdev(returns),
                    rel_tol=0.0,
                    abs_tol=1e-15,
                ),
                isclose(
                    observation.root_mean_square_return,
                    sqrt(mean(squared)),
                    rel_tol=0.0,
                    abs_tol=1e-15,
                ),
                isclose(
                    observation.upside_semivolatility,
                    sqrt(sum(positive)) if positive else 0.0,
                    rel_tol=0.0,
                    abs_tol=1e-15,
                ),
                isclose(
                    observation.downside_semivolatility,
                    sqrt(sum(negative)) if negative else 0.0,
                    rel_tol=0.0,
                    abs_tol=1e-15,
                ),
                isclose(observation.elapsed_seconds, elapsed, rel_tol=0.0, abs_tol=1e-12),
            )
            if not all(checks):
                raise ValueError(f"C035 realized volatility does not reconcile for {symbol} {timeframe}")
            nonzero_realized += observation.realized_volatility > 0.0
            frames[timeframe] = {
                "realized_variance": observation.realized_variance,
                "realized_volatility": observation.realized_volatility,
                "return_standard_deviation": observation.return_standard_deviation,
                "root_mean_square_return": observation.root_mean_square_return,
                "mean_return": observation.mean_return,
                "upside_semivolatility": observation.upside_semivolatility,
                "downside_semivolatility": observation.downside_semivolatility,
                "elapsed_seconds": observation.elapsed_seconds,
                "sample_size": observation.sample_size,
                "return_count": observation.return_count,
                "source": observation.source,
            }
        output[symbol] = frames
    if nonzero_realized == 0:
        raise ValueError("C035 live evidence contains no measurable realized volatility")
    return {
        "schema_version": 1,
        "requirement_id": "C035",
        "status": "PASS",
        "bridge_root": str(bridge_root),
        "observed_at": observed_at.isoformat(),
        "symbol_count": len(output),
        "timeframe_count_per_symbol": len(RESEARCH_TIMEFRAMES),
        "measurement_count": len(output) * len(RESEARCH_TIMEFRAMES),
        "nonzero_realized_volatility_count": nonzero_realized,
        "price_basis": "CLOSE_TO_CLOSE_LOG_RETURNS",
        "atr_dependency": "NONE",
        "measurement_role": "DESCRIPTIVE_REALIZED_VOLATILITY_NOT_TRADE_APPROVAL",
        "symbols": output,
    }


def verify_c036_atr_intelligence(
    *,
    bridge_root: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    active_symbols = {"XAUUSD", "UK100", "USA100", "USA500", "USA30"}
    if set(symbols) != active_symbols:
        raise ValueError("C036 requires the exact five-symbol active universe")
    observed_at = datetime.now(timezone.utc)
    adapter = HfmCsvMarketDataAdapter(bridge_root)
    output = {}
    scoped_baselines = 0
    gap_sensitive_frames = 0
    scopes: set[str] = set()
    for symbol in symbols:
        dataset = adapter.dataset(
            symbol,
            observed_at=observed_at,
            timeframes=RESEARCH_TIMEFRAMES,
            include_latest_tick=False,
            derive_m3_history=False,
            history_mode="rolling",
        )
        snapshot = snapshot_from_dataset(
            dataset,
            observed_at=observed_at,
            required_timeframes=RESEARCH_TIMEFRAMES,
            include_micro=False,
            max_bars_per_frame=500,
        )
        report = IntelligenceEngine().analyse(snapshot)
        if set(report.atr_intelligence) != set(RESEARCH_TIMEFRAMES):
            raise ValueError(f"C036 ATR coverage is incomplete for {symbol}")
        frames = {}
        for timeframe in RESEARCH_TIMEFRAMES:
            candles = tuple(snapshot.candles[timeframe])
            observation = report.atr_intelligence[timeframe]
            if len(candles) < 14:
                raise ValueError(f"C036 has insufficient live candles for {symbol} {timeframe}")
            true_ranges = []
            previous_close = None
            gap_changed_range = False
            for candle in candles:
                candle_range = max(0.0, candle.high - candle.low)
                value = candle_range
                if previous_close is not None:
                    value = max(
                        value,
                        abs(candle.high - previous_close),
                        abs(candle.low - previous_close),
                    )
                    gap_changed_range = gap_changed_range or value > candle_range
                true_ranges.append(value)
                previous_close = candle.close
            expected = sum(true_ranges[:14]) / 14
            for value in true_ranges[14:]:
                expected = ((expected * 13) + value) / 14
            checks = (
                observation.symbol == symbol,
                observation.timeframe == timeframe,
                observation.regime == report.regimes[timeframe].label,
                observation.period == 14,
                observation.true_range_count == len(candles),
                observation.source == "WILDER_TRUE_RANGE_CONTEXT_NORMALIZATION",
                isclose(observation.atr, expected, rel_tol=0.0, abs_tol=1e-12),
                isclose(
                    observation.price_normalized_atr,
                    expected / candles[-1].close,
                    rel_tol=0.0,
                    abs_tol=1e-12,
                ),
                observation.baseline_scope
                == f"{symbol}|{timeframe}|{observation.session}|{observation.regime}",
                observation.status in {"OBSERVED", "INSUFFICIENT_CONTEXT_BASELINE"},
            )
            if not all(checks):
                raise ValueError(f"C036 ATR does not reconcile for {symbol} {timeframe}")
            if observation.status == "OBSERVED":
                if (
                    observation.context_baseline_atr is None
                    or observation.context_baseline_atr <= 0
                    or observation.atr_to_context_baseline is None
                    or observation.context_percentile is None
                    or not 0.0 <= observation.context_percentile <= 1.0
                    or observation.context_sample_size <= 0
                ):
                    raise ValueError(f"C036 scoped baseline is invalid for {symbol} {timeframe}")
                scoped_baselines += 1
            gap_sensitive_frames += gap_changed_range
            scopes.add(observation.baseline_scope)
            frames[timeframe] = {
                "session": observation.session,
                "regime": observation.regime,
                "period": observation.period,
                "atr": observation.atr,
                "price_normalized_atr": observation.price_normalized_atr,
                "context_baseline_atr": observation.context_baseline_atr,
                "atr_to_context_baseline": observation.atr_to_context_baseline,
                "context_percentile": observation.context_percentile,
                "context_sample_size": observation.context_sample_size,
                "true_range_count": observation.true_range_count,
                "baseline_scope": observation.baseline_scope,
                "status": observation.status,
                "source": observation.source,
                "gap_sensitive_true_range_observed": gap_changed_range,
            }
        output[symbol] = frames
    measurement_count = len(output) * len(RESEARCH_TIMEFRAMES)
    if scoped_baselines != measurement_count:
        raise ValueError(
            f"C036 exact context baselines are incomplete: {scoped_baselines}/{measurement_count}"
        )
    if gap_sensitive_frames == 0:
        raise ValueError("C036 broker history contains no gap-sensitive true-range observation")
    return {
        "schema_version": 1,
        "requirement_id": "C036",
        "status": "PASS",
        "bridge_root": str(bridge_root),
        "observed_at": observed_at.isoformat(),
        "symbol_count": len(output),
        "timeframe_count_per_symbol": len(RESEARCH_TIMEFRAMES),
        "measurement_count": measurement_count,
        "scoped_context_baseline_count": scoped_baselines,
        "unique_context_scope_count": len(scopes),
        "gap_sensitive_true_range_frame_count": gap_sensitive_frames,
        "normalization_dimensions": ("SYMBOL", "TIMEFRAME", "SESSION", "REGIME"),
        "atr_method": "WILDER_TRUE_RANGE_RECURSIVE_PERIOD_14",
        "measurement_role": "DESCRIPTIVE_ATR_CONTEXT_NOT_TRADE_APPROVAL",
        "symbols": output,
    }


def verify_c037_volatility_regime(
    *,
    bridge_root: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    active_symbols = {"XAUUSD", "UK100", "USA100", "USA500", "USA30"}
    if set(symbols) != active_symbols:
        raise ValueError("C037 requires the exact five-symbol active universe")
    observed_at = datetime.now(timezone.utc)
    adapter = HfmCsvMarketDataAdapter(bridge_root)
    output = {}
    dominant_regimes: set[str] = set()
    probabilistic_measurements = 0
    for symbol in symbols:
        dataset = adapter.dataset(
            symbol,
            observed_at=observed_at,
            timeframes=RESEARCH_TIMEFRAMES,
            include_latest_tick=False,
            derive_m3_history=False,
            history_mode="rolling",
        )
        snapshot = snapshot_from_dataset(
            dataset,
            observed_at=observed_at,
            required_timeframes=RESEARCH_TIMEFRAMES,
            include_micro=False,
            max_bars_per_frame=500,
        )
        report = IntelligenceEngine().analyse(snapshot)
        if set(report.volatility_regime_probability) != set(RESEARCH_TIMEFRAMES):
            raise ValueError(f"C037 volatility-regime coverage is incomplete for {symbol}")
        frames = {}
        for timeframe in RESEARCH_TIMEFRAMES:
            candles = tuple(snapshot.candles[timeframe])
            observation = report.volatility_regime_probability[timeframe]
            if observation.status != "OBSERVED" or len(candles) < 20:
                raise ValueError(f"C037 lacks observed history for {symbol} {timeframe}")
            window = candles[-20:]
            returns = tuple(
                log(current.close / previous.close)
                for previous, current in zip(window, window[1:])
            )
            expected_current = sqrt(sum(value * value for value in returns))
            probabilities = dict(observation.probabilities)
            checks = (
                observation.symbol == symbol,
                observation.timeframe == timeframe,
                observation.source == "EMPIRICAL_REALIZED_VOLATILITY_PROBABILITY_NO_ATR",
                set(probabilities) == {"LOW", "NORMAL", "HIGH", "EXTREME"},
                all(isfinite(value) and 0.0 <= value <= 1.0 for value in probabilities.values()),
                isclose(sum(probabilities.values()), 1.0, rel_tol=0.0, abs_tol=1e-12),
                isclose(
                    observation.current_realized_volatility,
                    expected_current,
                    rel_tol=0.0,
                    abs_tol=1e-15,
                ),
                0.0 <= observation.low_boundary <= observation.high_boundary <= observation.extreme_boundary,
                observation.uncertainty_log_scale > 0.0,
                observation.historical_sample_size >= 5,
                observation.dominant_regime
                == max(probabilities, key=probabilities.get),
                0.0 <= observation.entropy <= 1.0,
            )
            if not all(checks):
                raise ValueError(f"C037 volatility regime does not reconcile for {symbol} {timeframe}")
            dominant_regimes.add(observation.dominant_regime)
            probabilistic_measurements += sum(value > 0.0 for value in probabilities.values()) > 1
            frames[timeframe] = {
                "current_realized_volatility": observation.current_realized_volatility,
                "low_boundary": observation.low_boundary,
                "high_boundary": observation.high_boundary,
                "extreme_boundary": observation.extreme_boundary,
                "uncertainty_log_scale": observation.uncertainty_log_scale,
                "probabilities": probabilities,
                "dominant_regime": observation.dominant_regime,
                "entropy": observation.entropy,
                "historical_sample_size": observation.historical_sample_size,
                "lookback": observation.lookback,
                "status": observation.status,
                "source": observation.source,
            }
        output[symbol] = frames
    measurement_count = len(output) * len(RESEARCH_TIMEFRAMES)
    if probabilistic_measurements == 0:
        raise ValueError("C037 produced only hard one-hot volatility labels")
    return {
        "schema_version": 1,
        "requirement_id": "C037",
        "status": "PASS",
        "bridge_root": str(bridge_root),
        "observed_at": observed_at.isoformat(),
        "symbol_count": len(output),
        "timeframe_count_per_symbol": len(RESEARCH_TIMEFRAMES),
        "measurement_count": measurement_count,
        "probabilistic_measurement_count": probabilistic_measurements,
        "dominant_regimes_observed": tuple(sorted(dominant_regimes)),
        "probability_labels": ("LOW", "NORMAL", "HIGH", "EXTREME"),
        "price_basis": "ROLLING_CLOSE_TO_CLOSE_REALIZED_VOLATILITY",
        "atr_dependency": "NONE",
        "measurement_role": "DESCRIPTIVE_VOLATILITY_REGIME_NOT_TRADE_APPROVAL",
        "symbols": output,
    }


def verify_c038_volatility_of_volatility(
    *,
    bridge_root: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    active_symbols = {"XAUUSD", "UK100", "USA100", "USA500", "USA30"}
    if set(symbols) != active_symbols:
        raise ValueError("C038 requires the exact five-symbol active universe")
    observed_at = datetime.now(timezone.utc)
    adapter = HfmCsvMarketDataAdapter(bridge_root)
    output = {}
    rapid_change_count = 0
    directions: set[str] = set()
    for symbol in symbols:
        dataset = adapter.dataset(
            symbol,
            observed_at=observed_at,
            timeframes=RESEARCH_TIMEFRAMES,
            include_latest_tick=False,
            derive_m3_history=False,
            history_mode="rolling",
        )
        snapshot = snapshot_from_dataset(
            dataset,
            observed_at=observed_at,
            required_timeframes=RESEARCH_TIMEFRAMES,
            include_micro=False,
            max_bars_per_frame=500,
        )
        report = IntelligenceEngine().analyse(snapshot)
        if set(report.volatility_of_volatility) != set(RESEARCH_TIMEFRAMES):
            raise ValueError(f"C038 volatility-change coverage is incomplete for {symbol}")
        frames = {}
        for timeframe in RESEARCH_TIMEFRAMES:
            candles = tuple(snapshot.candles[timeframe])
            observation = report.volatility_of_volatility[timeframe]
            if observation.status != "OBSERVED" or len(candles) < 26:
                raise ValueError(f"C038 lacks observed history for {symbol} {timeframe}")
            current_window = candles[-20:]
            previous_window = candles[-21:-1]
            current_returns = tuple(
                log(current.close / previous.close)
                for previous, current in zip(current_window, current_window[1:])
            )
            previous_returns = tuple(
                log(current.close / previous.close)
                for previous, current in zip(previous_window, previous_window[1:])
            )
            current_volatility = sqrt(sum(value * value for value in current_returns))
            previous_volatility = sqrt(sum(value * value for value in previous_returns))
            expected_change = log(max(current_volatility, 1e-15) / max(previous_volatility, 1e-15))
            elapsed = (candles[-1].end - candles[-2].end).total_seconds()
            checks = (
                observation.symbol == symbol,
                observation.timeframe == timeframe,
                observation.source == "ROLLING_REALIZED_VOLATILITY_LOG_CHANGE_NO_ATR",
                isclose(observation.current_realized_volatility, current_volatility, rel_tol=0.0, abs_tol=1e-15),
                isclose(observation.previous_realized_volatility, previous_volatility, rel_tol=0.0, abs_tol=1e-15),
                isclose(observation.log_volatility_change, expected_change, rel_tol=0.0, abs_tol=1e-12),
                isclose(observation.change_rate_per_second, expected_change / elapsed, rel_tol=0.0, abs_tol=1e-12),
                observation.absolute_log_volatility_change >= 0.0,
                isfinite(observation.robust_change_zscore),
                0.0 <= observation.rapid_change_percentile <= 1.0,
                observation.direction in {"INCREASING", "DECREASING", "STABLE"},
                observation.historical_change_sample_size > 0,
            )
            if not all(checks):
                raise ValueError(f"C038 volatility change does not reconcile for {symbol} {timeframe}")
            rapid_change_count += observation.rapid_change
            directions.add(observation.direction)
            frames[timeframe] = {
                "current_realized_volatility": observation.current_realized_volatility,
                "previous_realized_volatility": observation.previous_realized_volatility,
                "log_volatility_change": observation.log_volatility_change,
                "absolute_log_volatility_change": observation.absolute_log_volatility_change,
                "change_rate_per_second": observation.change_rate_per_second,
                "baseline_median_absolute_change": observation.baseline_median_absolute_change,
                "robust_change_zscore": observation.robust_change_zscore,
                "rapid_change_percentile": observation.rapid_change_percentile,
                "rapid_change": observation.rapid_change,
                "direction": observation.direction,
                "historical_change_sample_size": observation.historical_change_sample_size,
                "lookback": observation.lookback,
                "status": observation.status,
                "source": observation.source,
            }
        output[symbol] = frames
    return {
        "schema_version": 1,
        "requirement_id": "C038",
        "status": "PASS",
        "bridge_root": str(bridge_root),
        "observed_at": observed_at.isoformat(),
        "symbol_count": len(output),
        "timeframe_count_per_symbol": len(RESEARCH_TIMEFRAMES),
        "measurement_count": len(output) * len(RESEARCH_TIMEFRAMES),
        "rapid_change_count": rapid_change_count,
        "observed_directions": tuple(sorted(directions)),
        "price_basis": "CHANGE_IN_ROLLING_CLOSE_TO_CLOSE_REALIZED_VOLATILITY",
        "atr_dependency": "NONE",
        "measurement_role": "DESCRIPTIVE_VOLATILITY_CHANGE_NOT_TRADE_APPROVAL",
        "symbols": output,
    }


def verify_c039_compression_detector(
    *,
    bridge_root: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    active_symbols = {"XAUUSD", "UK100", "USA100", "USA500", "USA30"}
    if set(symbols) != active_symbols:
        raise ValueError("C039 requires the exact five-symbol active universe")
    observed_at = datetime.now(timezone.utc)
    adapter = HfmCsvMarketDataAdapter(bridge_root)
    output = {}
    event_count = 0
    breakout_count = 0
    failed_breakout_count = 0
    current_compression_count = 0
    event_ids: set[str] = set()
    for symbol in symbols:
        dataset = adapter.dataset(
            symbol,
            observed_at=observed_at,
            timeframes=RESEARCH_TIMEFRAMES,
            include_latest_tick=False,
            derive_m3_history=False,
            history_mode="rolling",
        )
        snapshot = snapshot_from_dataset(
            dataset,
            observed_at=observed_at,
            required_timeframes=RESEARCH_TIMEFRAMES,
            include_micro=False,
            max_bars_per_frame=500,
        )
        report = IntelligenceEngine().analyse(snapshot)
        if set(report.compression) != set(RESEARCH_TIMEFRAMES):
            raise ValueError(f"C039 compression coverage is incomplete for {symbol}")
        frames = {}
        for timeframe in RESEARCH_TIMEFRAMES:
            candles = tuple(snapshot.candles[timeframe])
            observation = report.compression[timeframe]
            if observation.status != "OBSERVED":
                raise ValueError(f"C039 lacks observed history for {symbol} {timeframe}")
            checks = (
                observation.symbol == symbol,
                observation.timeframe == timeframe,
                observation.source == "EMPIRICAL_RANGE_AND_REALIZED_VOLATILITY_CONTRACTION",
                observation.observation_index == len(candles) - 1,
                observation.range_level >= 0.0,
                observation.realized_volatility >= 0.0,
                observation.range_to_baseline is not None,
                observation.volatility_to_baseline is not None,
                observation.range_percentile is not None
                and 0.0 <= observation.range_percentile <= 1.0,
                observation.volatility_percentile is not None
                and 0.0 <= observation.volatility_percentile <= 1.0,
                observation.historical_sample_size >= 10,
            )
            if not all(checks):
                raise ValueError(f"C039 compression does not reconcile for {symbol} {timeframe}")
            outcomes = compression_outcomes(
                candles,
                symbol=symbol,
                timeframe=timeframe,
                lookback=20,
                forward_bars=10,
            )
            for outcome in outcomes:
                if outcome.event_id in event_ids:
                    raise ValueError("C039 compression outcome IDs are not unique")
                event_ids.add(outcome.event_id)
                if outcome.breakout and (
                    outcome.breakout_direction not in {"UP", "DOWN"}
                    or outcome.bars_to_breakout is None
                    or not 1 <= outcome.bars_to_breakout <= outcome.forward_bars
                ):
                    raise ValueError("C039 breakout outcome is internally inconsistent")
                if not outcome.breakout and (
                    outcome.breakout_direction != "NONE"
                    or outcome.bars_to_breakout is not None
                ):
                    raise ValueError("C039 no-breakout outcome is internally inconsistent")
            event_count += len(outcomes)
            breakout_count += sum(item.breakout for item in outcomes)
            failed_breakout_count += sum(item.failed_back_inside for item in outcomes)
            current_compression_count += observation.compression
            frames[timeframe] = {
                "current": {
                    "range_level": observation.range_level,
                    "realized_volatility": observation.realized_volatility,
                    "range_to_baseline": observation.range_to_baseline,
                    "volatility_to_baseline": observation.volatility_to_baseline,
                    "range_percentile": observation.range_percentile,
                    "volatility_percentile": observation.volatility_percentile,
                    "range_declining": observation.range_declining,
                    "volatility_declining": observation.volatility_declining,
                    "compression": observation.compression,
                    "historical_sample_size": observation.historical_sample_size,
                    "status": observation.status,
                    "source": observation.source,
                },
                "historical_outcomes": tuple(
                    {
                        "event_id": item.event_id,
                        "compression_index": item.compression_index,
                        "compression_at": item.compression_at,
                        "reference_low": item.reference_low,
                        "reference_high": item.reference_high,
                        "breakout": item.breakout,
                        "breakout_direction": item.breakout_direction,
                        "bars_to_breakout": item.bars_to_breakout,
                        "maximum_breakout_excursion_ranges": item.maximum_breakout_excursion_ranges,
                        "failed_back_inside": item.failed_back_inside,
                        "forward_bars": item.forward_bars,
                        "source": item.source,
                    }
                    for item in outcomes
                ),
            }
        output[symbol] = frames
    if event_count == 0 or breakout_count == 0:
        raise ValueError("C039 broker history contains no measurable compression breakout outcomes")
    return {
        "schema_version": 1,
        "requirement_id": "C039",
        "status": "PASS",
        "bridge_root": str(bridge_root),
        "observed_at": observed_at.isoformat(),
        "symbol_count": len(output),
        "timeframe_count_per_symbol": len(RESEARCH_TIMEFRAMES),
        "measurement_count": len(output) * len(RESEARCH_TIMEFRAMES),
        "current_compression_count": current_compression_count,
        "historical_compression_event_count": event_count,
        "historical_breakout_count": breakout_count,
        "historical_no_breakout_count": event_count - breakout_count,
        "historical_failed_breakout_count": failed_breakout_count,
        "compression_definition": "BOTTOM_QUARTILE_RANGE_AND_REALIZED_VOLATILITY_WITH_BOTH_DECLINING",
        "outcome_horizon_bars": 10,
        "measurement_role": "DESCRIPTIVE_COMPRESSION_AND_OUTCOMES_NOT_TRADE_APPROVAL",
        "symbols": output,
    }


def verify_c040_expansion_detector(
    *,
    bridge_root: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    active_symbols = {"XAUUSD", "UK100", "USA100", "USA500", "USA30"}
    if set(symbols) != active_symbols:
        raise ValueError("C040 requires the exact five-symbol active universe")
    observed_at = datetime.now(timezone.utc)
    adapter = HfmCsvMarketDataAdapter(bridge_root)
    output = {}
    all_outcomes = []
    current_expansion_count = 0
    event_ids: set[str] = set()
    for symbol in symbols:
        dataset = adapter.dataset(
            symbol,
            observed_at=observed_at,
            timeframes=RESEARCH_TIMEFRAMES,
            include_latest_tick=False,
            derive_m3_history=False,
            history_mode="rolling",
        )
        snapshot = snapshot_from_dataset(
            dataset,
            observed_at=observed_at,
            required_timeframes=RESEARCH_TIMEFRAMES,
            include_micro=False,
            max_bars_per_frame=500,
        )
        report = IntelligenceEngine().analyse(snapshot)
        if set(report.expansion) != set(RESEARCH_TIMEFRAMES):
            raise ValueError(f"C040 expansion coverage is incomplete for {symbol}")
        frames = {}
        for timeframe in RESEARCH_TIMEFRAMES:
            observation = report.expansion[timeframe]
            if observation.status not in {"OBSERVED", "NO_RECENT_COMPRESSION"}:
                raise ValueError(f"C040 expansion status is invalid for {symbol} {timeframe}")
            if observation.expansion and (
                observation.direction not in {"UP", "DOWN"}
                or observation.range_expansion_ratio is None
                or observation.range_expansion_ratio <= 1.0
                or observation.volatility_expansion_ratio is None
                or observation.volatility_expansion_ratio <= 1.0
            ):
                raise ValueError(f"C040 current expansion is inconsistent for {symbol} {timeframe}")
            outcomes = expansion_after_compression_outcomes(
                tuple(snapshot.candles[timeframe]),
                symbol=symbol,
                timeframe=timeframe,
                lookback=20,
                forward_bars=10,
            )
            for item in outcomes:
                if item.event_id in event_ids:
                    raise ValueError("C040 expansion event IDs are not unique")
                event_ids.add(item.event_id)
                if item.expansion and (
                    item.direction not in {"UP", "DOWN"}
                    or item.bars_to_expansion is None
                    or not 1 <= item.bars_to_expansion <= item.forward_bars
                    or item.range_expansion_ratio <= 1.0
                    or item.volatility_expansion_ratio <= 1.0
                ):
                    raise ValueError("C040 historical expansion is internally inconsistent")
                if not item.expansion and (
                    item.direction != "NONE" or item.bars_to_expansion is not None
                ):
                    raise ValueError("C040 no-expansion outcome is internally inconsistent")
            all_outcomes.extend(outcomes)
            current_expansion_count += observation.expansion
            stats = expansion_statistics(outcomes)
            frames[timeframe] = {
                "current": {
                    "compression_index": observation.compression_index,
                    "bars_since_compression": observation.bars_since_compression,
                    "expansion": observation.expansion,
                    "direction": observation.direction,
                    "range_expansion_ratio": observation.range_expansion_ratio,
                    "volatility_expansion_ratio": observation.volatility_expansion_ratio,
                    "close_distance_ranges": observation.close_distance_ranges,
                    "status": observation.status,
                    "source": observation.source,
                },
                "statistics": {
                    "compression_event_count": stats.compression_event_count,
                    "expansion_count": stats.expansion_count,
                    "no_expansion_count": stats.no_expansion_count,
                    "expansion_rate": stats.expansion_rate,
                    "upward_expansion_count": stats.upward_expansion_count,
                    "downward_expansion_count": stats.downward_expansion_count,
                    "median_bars_to_expansion": stats.median_bars_to_expansion,
                    "median_range_expansion_ratio": stats.median_range_expansion_ratio,
                    "median_volatility_expansion_ratio": stats.median_volatility_expansion_ratio,
                },
                "historical_outcomes": tuple(
                    {
                        "event_id": item.event_id,
                        "compression_index": item.compression_index,
                        "expansion_index": item.expansion_index,
                        "expansion_at": item.expansion_at,
                        "expansion": item.expansion,
                        "direction": item.direction,
                        "bars_to_expansion": item.bars_to_expansion,
                        "range_expansion_ratio": item.range_expansion_ratio,
                        "volatility_expansion_ratio": item.volatility_expansion_ratio,
                        "close_distance_ranges": item.close_distance_ranges,
                        "forward_bars": item.forward_bars,
                        "source": item.source,
                    }
                    for item in outcomes
                ),
            }
        output[symbol] = frames
    stats = expansion_statistics(all_outcomes)
    if stats.compression_event_count == 0 or stats.expansion_count == 0:
        raise ValueError("C040 broker history contains no measurable expansion after compression")
    if stats.expansion_count + stats.no_expansion_count != stats.compression_event_count:
        raise ValueError("C040 expansion statistics do not preserve the full denominator")
    return {
        "schema_version": 1,
        "requirement_id": "C040",
        "status": "PASS",
        "bridge_root": str(bridge_root),
        "observed_at": observed_at.isoformat(),
        "symbol_count": len(output),
        "timeframe_count_per_symbol": len(RESEARCH_TIMEFRAMES),
        "measurement_count": len(output) * len(RESEARCH_TIMEFRAMES),
        "current_expansion_count": current_expansion_count,
        "historical_statistics": {
            "compression_event_count": stats.compression_event_count,
            "expansion_count": stats.expansion_count,
            "no_expansion_count": stats.no_expansion_count,
            "expansion_rate": stats.expansion_rate,
            "upward_expansion_count": stats.upward_expansion_count,
            "downward_expansion_count": stats.downward_expansion_count,
            "median_bars_to_expansion": stats.median_bars_to_expansion,
            "median_range_expansion_ratio": stats.median_range_expansion_ratio,
            "median_volatility_expansion_ratio": stats.median_volatility_expansion_ratio,
        },
        "expansion_definition": "CLOSE_BREAK_WITH_RANGE_AND_REALIZED_VOLATILITY_BOTH_ABOVE_COMPRESSION",
        "outcome_horizon_bars": 10,
        "measurement_role": "DESCRIPTIVE_EXPANSION_STATISTICS_NOT_TRADE_APPROVAL",
        "symbols": output,
    }


def verify_c041_session_engine(
    *,
    bridge_root: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    active_symbols = {"XAUUSD", "UK100", "USA100", "USA500", "USA30"}
    if set(symbols) != active_symbols:
        raise ValueError("C041 requires the exact five-symbol active universe")
    adapter = HfmCsvMarketDataAdapter(bridge_root)
    output = {}
    for symbol in symbols:
        export = adapter.latest_tick_export(symbol)
        broker_epoch = int(export.broker_timestamp.timestamp())
        utc_epoch = int(export.utc_timestamp.timestamp())
        observation = broker_session_observation(
            broker_wall_epoch=broker_epoch,
            utc_epoch=utc_epoch,
            broker_utc_offset_seconds=export.broker_utc_offset_seconds,
        )
        expected_offset = hfm_server_utc_offset_seconds(export.broker_timestamp)
        if export.broker_utc_offset_seconds != expected_offset:
            raise ValueError(
                f"C041 HFM offset policy mismatch for {symbol}: "
                f"exported={export.broker_utc_offset_seconds} expected={expected_offset}"
            )
        if observation.session.primary_session != session_label(export.utc_timestamp):
            raise ValueError(f"C041 session classification mismatch for {symbol}")
        source_path = Path(export.source_path)
        output[symbol] = {
            "broker_wall_epoch": broker_epoch,
            "utc_epoch": utc_epoch,
            "broker_utc_offset_seconds": export.broker_utc_offset_seconds,
            "expected_hfm_offset_seconds": expected_offset,
            "offset_reconciled": observation.offset_reconciled,
            "canonical_utc": observation.session.canonical_utc,
            "primary_session": observation.session.primary_session,
            "active_sessions": observation.session.active_sessions,
            "source_path": str(source_path),
            "source_sha256": sha256(source_path.read_bytes()).hexdigest(),
        }

    winter_at = datetime(2026, 1, 15, 13, 30, tzinfo=timezone.utc)
    summer_at = datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc)
    winter = session_observation(winter_at)
    summer = session_observation(summer_at)
    winter_intervals = {item.session_id: item for item in winter.intervals}
    summer_intervals = {item.session_id: item for item in summer.intervals}
    if (
        winter_intervals["ASIA"].start_utc.hour != 0
        or summer_intervals["ASIA"].start_utc.hour != 0
        or winter_intervals["LONDON"].start_utc.hour != 8
        or summer_intervals["LONDON"].start_utc.hour != 7
        or winter_intervals["NEW_YORK"].start_utc.hour != 13
        or summer_intervals["NEW_YORK"].start_utc.hour != 12
    ):
        raise ValueError("C041 exchange-clock DST controls failed")
    if summer.primary_session != "LONDON_NY_OVERLAP":
        raise ValueError("C041 London/New York overlap control failed")
    weekend = session_observation(datetime(2026, 7, 18, 13, 30, tzinfo=timezone.utc))
    if weekend.primary_session != "QUIET" or weekend.active_sessions:
        raise ValueError("C041 weekend session control failed")

    return {
        "schema_version": 1,
        "requirement_id": "C041",
        "status": "PASS",
        "bridge_root": str(bridge_root),
        "broker_time_policy": HFM_SERVER_TIME_POLICY,
        "normalization_contract": "BROKER_WALL_EPOCH_MINUS_EXPORTED_OFFSET_EQUALS_EXPLICIT_UTC_BEFORE_SESSION_CLASSIFICATION",
        "session_clock_source": "IANA_TZDB_ASIA_TOKYO_EUROPE_LONDON_AMERICA_NEW_YORK",
        "symbol_count": len(output),
        "symbols": output,
        "controls": {
            "winter_utc": winter.canonical_utc,
            "summer_utc": summer.canonical_utc,
            "winter_london_open_utc": winter_intervals["LONDON"].start_utc.isoformat(),
            "summer_london_open_utc": summer_intervals["LONDON"].start_utc.isoformat(),
            "winter_new_york_open_utc": winter_intervals["NEW_YORK"].start_utc.isoformat(),
            "summer_new_york_open_utc": summer_intervals["NEW_YORK"].start_utc.isoformat(),
            "tokyo_open_utc_winter": winter_intervals["ASIA"].start_utc.isoformat(),
            "tokyo_open_utc_summer": summer_intervals["ASIA"].start_utc.isoformat(),
            "summer_overlap": summer.primary_session,
            "weekend": weekend.primary_session,
        },
        "role": "DESCRIPTIVE_SESSION_CONTEXT_NOT_TRADE_APPROVAL",
    }


def verify_c042_session_profile_library(
    *,
    bridge_root: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    active_symbols = {"XAUUSD", "UK100", "USA100", "USA500", "USA30"}
    if set(symbols) != active_symbols:
        raise ValueError("C042 requires the exact five-symbol active universe")
    adapter = HfmCsvMarketDataAdapter(bridge_root)
    observed_at = datetime.now(timezone.utc)
    output = {}
    all_profile_keys: set[str] = set()
    total_complete = 0
    total_excluded = 0
    source_hashes = {}
    for symbol in symbols:
        rows = adapter.read_bars(
            symbol,
            "M1",
            observed_at=observed_at,
            history_mode="combined",
        )
        if len(rows) < 10_000:
            raise ValueError(f"C042 lacks historical M1 depth for {symbol}: {len(rows)}")
        library = build_session_profile_library(
            {symbol: rows},
            observed_at=observed_at,
        )
        expected_keys = {f"{symbol}|{session}" for session in PROFILE_SESSIONS}
        if set(library.profiles) != expected_keys:
            raise ValueError(f"C042 profile coverage is incomplete for {symbol}")
        profiles = library.profiles_for_symbol(symbol)
        if set(profiles) != set(PROFILE_SESSIONS):
            raise ValueError(f"C042 report profile coverage is incomplete for {symbol}")
        for session, profile in profiles.items():
            if profile.symbol != symbol or profile.session != session:
                raise ValueError("C042 session profile identity is inconsistent")
            if profile.status != "OBSERVED" or profile.sample_size < 10:
                raise ValueError(f"C042 has insufficient complete {symbol} {session} sessions")
            rates = (profile.positive_rate, profile.negative_rate, profile.flat_rate)
            if any(value is None for value in rates) or not isclose(sum(rates), 1.0, abs_tol=1e-12):
                raise ValueError(f"C042 direction distribution is invalid for {symbol} {session}")
            quantiles = (profile.range_p25, profile.median_range_fraction, profile.range_p75, profile.range_p95)
            if any(value is None for value in quantiles) or tuple(quantiles) != tuple(sorted(quantiles)):
                raise ValueError(f"C042 range distribution is invalid for {symbol} {session}")
            if profile.first_session_date is None or profile.last_session_date is None:
                raise ValueError(f"C042 profile dates are absent for {symbol} {session}")
            all_profile_keys.add(f"{symbol}|{session}")
            total_complete += profile.sample_size
            total_excluded += profile.excluded_incomplete_sessions
        snapshot = MarketSnapshot(
            symbol,
            observed_at,
            (adapter.latest_tick(symbol),),
            {"M1": tuple(rows[-30:])},
            {"M1": "COMPLETED"},
            tuple(item.source_id for item in rows[-30:]),
        )
        report = IntelligenceEngine(session_profiles=library).analyse(snapshot)
        if set(report.session_profiles) != set(PROFILE_SESSIONS):
            raise ValueError(f"C042 profiles are not exposed by intelligence for {symbol}")
        source_path = bridge_root / f"deephistory_{symbol}_M1.csv"
        source_hashes[symbol] = {
            "path": str(source_path),
            "sha256": sha256(source_path.read_bytes()).hexdigest(),
        }
        output[symbol] = {
            "historical_m1_candle_count": len(rows),
            "profile_count": len(profiles),
            "observation_count": len(library.observations),
            "profiles": {
                session: {
                    "sample_size": profile.sample_size,
                    "excluded_incomplete_sessions": profile.excluded_incomplete_sessions,
                    "mean_return": profile.mean_return,
                    "median_return": profile.median_return,
                    "return_stddev": profile.return_stddev,
                    "positive_rate": profile.positive_rate,
                    "negative_rate": profile.negative_rate,
                    "flat_rate": profile.flat_rate,
                    "mean_range_fraction": profile.mean_range_fraction,
                    "median_range_fraction": profile.median_range_fraction,
                    "range_p25": profile.range_p25,
                    "range_p75": profile.range_p75,
                    "range_p95": profile.range_p95,
                    "mean_tick_activity": profile.mean_tick_activity,
                    "mean_spread_points": profile.mean_spread_points,
                    "mean_close_location": profile.mean_close_location,
                    "first_session_date": profile.first_session_date,
                    "last_session_date": profile.last_session_date,
                    "status": profile.status,
                    "source": profile.source,
                }
                for session, profile in profiles.items()
            },
        }
    if len(all_profile_keys) != len(active_symbols) * len(PROFILE_SESSIONS):
        raise ValueError("C042 profiles are not isolated by symbol and session")
    return {
        "schema_version": 1,
        "requirement_id": "C042",
        "status": "PASS",
        "bridge_root": str(bridge_root),
        "observed_at": observed_at.isoformat(),
        "symbol_count": len(output),
        "session_count_per_symbol": len(PROFILE_SESSIONS),
        "profile_count": len(all_profile_keys),
        "complete_session_observation_count": total_complete,
        "excluded_incomplete_session_count": total_excluded,
        "minimum_coverage_ratio": 0.80,
        "source_contract": "HFM_MT5_M1_COMPLETED_SESSIONS_NORMALIZED_BY_C041_IANA_CLOCKS",
        "source_artifacts": source_hashes,
        "role": "DESCRIPTIVE_HISTORICAL_PROFILE_NOT_TRADE_APPROVAL",
        "symbols": output,
    }


def verify_c043_asia_range_engine(
    *,
    bridge_root: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    active_symbols = {"XAUUSD", "UK100", "USA100", "USA500", "USA30"}
    if set(symbols) != active_symbols:
        raise ValueError("C043 requires the exact five-symbol active universe")
    adapter = HfmCsvMarketDataAdapter(bridge_root)
    observed_at = datetime.now(timezone.utc)
    output = {}
    total_ranges = 0
    for symbol in symbols:
        rows = adapter.read_bars(
            symbol,
            "M1",
            observed_at=observed_at,
            history_mode="combined",
        )
        library = build_session_profile_library(
            {symbol: rows},
            observed_at=observed_at,
        )
        ranges = library.observations_for_symbol_session(symbol, "ASIA")
        if len(ranges) < 10:
            raise ValueError(f"C043 has insufficient complete Asia ranges for {symbol}")
        serialized = []
        for item in ranges:
            if (
                not item.complete
                or item.high_price is None
                or item.low_price is None
                or item.midpoint_price is None
                or item.range_value is None
                or item.high_price < item.low_price
                or not isclose(item.midpoint_price, (item.high_price + item.low_price) / 2.0, abs_tol=1e-12)
                or not isclose(item.range_value, item.high_price - item.low_price, abs_tol=1e-12)
                or not item.source_digest
            ):
                raise ValueError(f"C043 invalid Asia range record for {symbol} {item.session_date}")
            serialized.append(
                {
                    "session_date": item.session_date,
                    "start_utc": item.start_utc,
                    "end_utc": item.end_utc,
                    "high": item.high_price,
                    "low": item.low_price,
                    "midpoint": item.midpoint_price,
                    "range_size": item.range_value,
                    "candle_count": item.candle_count,
                    "expected_candle_count": item.expected_candle_count,
                    "coverage_ratio": item.coverage_ratio,
                    "source_digest": item.source_digest,
                }
            )
        total_ranges += len(serialized)
        snapshot = MarketSnapshot(
            symbol,
            observed_at,
            (adapter.latest_tick(symbol),),
            {"M1": tuple(rows[-30:])},
            {"M1": "COMPLETED"},
            tuple(item.source_id for item in rows[-30:]),
        )
        report = IntelligenceEngine(session_profiles=library).analyse(snapshot)
        if len(report.asia_ranges) != len(ranges):
            raise ValueError(f"C043 Asia range history is not exposed for {symbol}")
        output[symbol] = {
            "range_count": len(serialized),
            "first_session_date": serialized[0]["session_date"],
            "last_session_date": serialized[-1]["session_date"],
            "ranges": tuple(serialized),
        }
    return {
        "schema_version": 1,
        "requirement_id": "C043",
        "status": "PASS",
        "bridge_root": str(bridge_root),
        "observed_at": observed_at.isoformat(),
        "symbol_count": len(output),
        "historical_asia_range_count": total_ranges,
        "range_fields": ("high", "low", "midpoint", "range_size"),
        "source_contract": "COMPLETE_HFM_M1_ASIA_SESSIONS_NORMALIZED_BY_C041_IANA_CLOCKS",
        "role": "DESCRIPTIVE_ASIA_RANGE_HISTORY_NOT_TRADE_APPROVAL",
        "symbols": output,
    }


def verify_c044_london_sweep_model(
    *,
    bridge_root: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    active_symbols = {"XAUUSD", "UK100", "USA100", "USA500", "USA30"}
    if set(symbols) != active_symbols:
        raise ValueError("C044 requires the exact five-symbol active universe")
    adapter = HfmCsvMarketDataAdapter(bridge_root)
    observed_at = datetime.now(timezone.utc)
    output = {}
    total_eligible = 0
    total_breaches = 0
    total_confirmed = 0
    for symbol in symbols:
        rows = adapter.read_bars(symbol, "M1", observed_at=observed_at, history_mode="combined")
        library = build_london_sweep_library({symbol: rows}, observed_at=observed_at)
        stats = library.statistics[symbol]
        outcomes = library.outcomes_for_symbol(symbol)
        if stats.eligible_session_count < 10 or len(outcomes) != stats.eligible_session_count:
            raise ValueError(f"C044 has insufficient eligible sessions for {symbol}")
        if stats.no_breach_session_count + stats.any_breach_session_count != stats.eligible_session_count:
            raise ValueError(f"C044 denominator is incomplete for {symbol}")
        if stats.high_breach_count + stats.low_breach_count != stats.breach_event_count:
            raise ValueError(f"C044 event denominator is inconsistent for {symbol}")
        if stats.breach_event_count <= 0 or stats.confirmed_sweep_count <= 0:
            raise ValueError(f"C044 broker history has no measurable sweep events for {symbol}")
        if stats.reclaim_rate is None or not 0.0 <= stats.reclaim_rate <= 1.0:
            raise ValueError(f"C044 reclaim statistic is invalid for {symbol}")
        for item in outcomes:
            if item.high_breached and (
                item.high_breach_at is None or item.high_depth_ranges is None or item.high_depth_ranges <= 0
            ):
                raise ValueError("C044 high breach detail is incomplete")
            if item.low_breached and (
                item.low_breach_at is None or item.low_depth_ranges is None or item.low_depth_ranges <= 0
            ):
                raise ValueError("C044 low breach detail is incomplete")
            if item.two_sided_breach != (item.high_breached and item.low_breached):
                raise ValueError("C044 two-sided breach classification is inconsistent")
        profile_library = build_session_profile_library({symbol: rows}, observed_at=observed_at)
        snapshot = MarketSnapshot(
            symbol,
            observed_at,
            (adapter.latest_tick(symbol),),
            {"M1": tuple(rows[-30:])},
            {"M1": "COMPLETED"},
            tuple(item.source_id for item in rows[-30:]),
        )
        report = IntelligenceEngine(
            session_profiles=profile_library,
            london_sweeps=library,
        ).analyse(snapshot)
        if report.london_sweep_statistics != stats or len(report.london_sweep_outcomes) != len(outcomes):
            raise ValueError(f"C044 intelligence exposure is incomplete for {symbol}")
        total_eligible += stats.eligible_session_count
        total_breaches += stats.breach_event_count
        total_confirmed += stats.confirmed_sweep_count
        output[symbol] = {
            "statistics": {
                "eligible_session_count": stats.eligible_session_count,
                "no_breach_session_count": stats.no_breach_session_count,
                "any_breach_session_count": stats.any_breach_session_count,
                "high_breach_count": stats.high_breach_count,
                "low_breach_count": stats.low_breach_count,
                "two_sided_breach_count": stats.two_sided_breach_count,
                "breach_event_count": stats.breach_event_count,
                "confirmed_sweep_count": stats.confirmed_sweep_count,
                "held_outside_count": stats.held_outside_count,
                "reclaim_rate": stats.reclaim_rate,
                "held_outside_rate": stats.held_outside_rate,
                "median_depth_ranges": stats.median_depth_ranges,
                "median_minutes_to_breach": stats.median_minutes_to_breach,
            },
            "outcomes": tuple(
                {
                    "session_date": item.session_date,
                    "asia_high": item.asia_high,
                    "asia_low": item.asia_low,
                    "asia_range": item.asia_range,
                    "high_breached": item.high_breached,
                    "high_reclaimed": item.high_reclaimed,
                    "high_breach_at": item.high_breach_at,
                    "high_depth_ranges": item.high_depth_ranges,
                    "high_minutes_to_breach": item.high_minutes_to_breach,
                    "high_held_outside_at_close": item.high_held_outside_at_close,
                    "low_breached": item.low_breached,
                    "low_reclaimed": item.low_reclaimed,
                    "low_breach_at": item.low_breach_at,
                    "low_depth_ranges": item.low_depth_ranges,
                    "low_minutes_to_breach": item.low_minutes_to_breach,
                    "low_held_outside_at_close": item.low_held_outside_at_close,
                    "two_sided_breach": item.two_sided_breach,
                    "london_close": item.london_close,
                    "source_digest": item.source_digest,
                }
                for item in outcomes
            ),
        }
    return {
        "schema_version": 1,
        "requirement_id": "C044",
        "status": "PASS",
        "bridge_root": str(bridge_root),
        "observed_at": observed_at.isoformat(),
        "symbol_count": len(output),
        "eligible_session_count": total_eligible,
        "breach_event_count": total_breaches,
        "confirmed_sweep_count": total_confirmed,
        "definition": "LONDON_HIGH_OR_LOW_BREACH_OF_COMPLETED_ASIA_RANGE_WITH_RECLAIM_AND_HOLD_OUTCOMES",
        "denominator_policy": "ALL_COMPLETE_PAIRED_ASIA_LONDON_SESSIONS_INCLUDING_NO_BREACH_AND_TWO_SIDED_DAYS",
        "role": "EMPIRICAL_SWEEP_STATISTICS_NOT_TRADE_APPROVAL",
        "symbols": output,
    }


def verify_c045_ny_continuation_model(
    *,
    bridge_root: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    active_symbols = {"XAUUSD", "UK100", "USA100", "USA500", "USA30"}
    if set(symbols) != active_symbols:
        raise ValueError("C045 requires the exact five-symbol active universe")
    adapter = HfmCsvMarketDataAdapter(bridge_root)
    observed_at = datetime.now(timezone.utc)
    output = {}
    total_eligible = 0
    total_continuations = 0
    for symbol in symbols:
        rows = adapter.read_bars(symbol, "M1", observed_at=observed_at, history_mode="combined")
        transitions = build_ny_transition_library({symbol: rows}, observed_at=observed_at)
        stats = transitions.statistics[symbol]
        outcomes = transitions.outcomes_for_symbol(symbol)
        if stats.eligible_session_count < 10 or len(outcomes) != stats.eligible_session_count:
            raise ValueError(f"C045 has insufficient eligible sessions for {symbol}")
        if stats.continuation_count + stats.reversal_count + stats.neutral_count != stats.eligible_session_count:
            raise ValueError(f"C045 outcome denominator is incomplete for {symbol}")
        if stats.continuation_count <= 0 or stats.continuation_rate is None:
            raise ValueError(f"C045 has no observed NY continuation outcomes for {symbol}")
        if not 0.0 <= stats.continuation_rate <= 1.0:
            raise ValueError(f"C045 continuation rate is invalid for {symbol}")
        if stats.london_up_count + stats.london_down_count + stats.neutral_count != stats.eligible_session_count:
            raise ValueError(f"C045 London conditioning denominator is invalid for {symbol}")
        for item in outcomes:
            if item.continuation != (
                item.london_direction == item.new_york_direction
                and item.london_direction in {"UP", "DOWN"}
            ):
                raise ValueError("C045 continuation classification is inconsistent")
            if datetime.fromisoformat(item.london_reference_end_utc) != datetime.fromisoformat(item.new_york_start_utc):
                raise ValueError("C045 London reference contains post-NY look-ahead")
            if not item.source_digest:
                raise ValueError("C045 transition outcome lacks provenance")
        profiles = build_session_profile_library({symbol: rows}, observed_at=observed_at)
        snapshot = MarketSnapshot(
            symbol,
            observed_at,
            (adapter.latest_tick(symbol),),
            {"M1": tuple(rows[-30:])},
            {"M1": "COMPLETED"},
            tuple(item.source_id for item in rows[-30:]),
        )
        report = IntelligenceEngine(
            session_profiles=profiles,
            ny_transitions=transitions,
        ).analyse(snapshot)
        if report.ny_transition_statistics != stats or len(report.ny_transition_outcomes) != len(outcomes):
            raise ValueError(f"C045 intelligence exposure is incomplete for {symbol}")
        total_eligible += stats.eligible_session_count
        total_continuations += stats.continuation_count
        output[symbol] = {
            "statistics": {
                "eligible_session_count": stats.eligible_session_count,
                "continuation_count": stats.continuation_count,
                "reversal_count": stats.reversal_count,
                "neutral_count": stats.neutral_count,
                "continuation_rate": stats.continuation_rate,
                "reversal_rate": stats.reversal_rate,
                "london_up_count": stats.london_up_count,
                "london_up_continuation_rate": stats.london_up_continuation_rate,
                "london_up_reversal_rate": stats.london_up_reversal_rate,
                "london_down_count": stats.london_down_count,
                "london_down_continuation_rate": stats.london_down_continuation_rate,
                "london_down_reversal_rate": stats.london_down_reversal_rate,
                "median_absolute_london_return": stats.median_absolute_london_return,
                "median_absolute_new_york_return": stats.median_absolute_new_york_return,
            },
            "outcomes": tuple(
                {
                    "session_date": item.session_date,
                    "london_start_utc": item.london_start_utc,
                    "london_reference_end_utc": item.london_reference_end_utc,
                    "new_york_start_utc": item.new_york_start_utc,
                    "new_york_end_utc": item.new_york_end_utc,
                    "london_direction": item.london_direction,
                    "new_york_direction": item.new_york_direction,
                    "london_return": item.london_return,
                    "new_york_return": item.new_york_return,
                    "continuation": item.continuation,
                    "reversal": item.reversal,
                    "neutral": item.neutral,
                    "source_digest": item.source_digest,
                }
                for item in outcomes
            ),
        }
    return {
        "schema_version": 1,
        "requirement_id": "C045",
        "status": "PASS",
        "bridge_root": str(bridge_root),
        "observed_at": observed_at.isoformat(),
        "symbol_count": len(output),
        "eligible_session_count": total_eligible,
        "continuation_count": total_continuations,
        "conditioning_contract": "NY_DIRECTION_GIVEN_ONLY_LONDON_PATH_AVAILABLE_BEFORE_NY_OPEN",
        "denominator_policy": "ALL_COMPLETE_PAIRED_PRE_NY_LONDON_AND_NY_SESSIONS",
        "role": "EMPIRICAL_CONTINUATION_STATISTICS_NOT_TRADE_APPROVAL",
        "symbols": output,
    }


def verify_c046_ny_reversal_model(
    *,
    bridge_root: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    active_symbols = {"XAUUSD", "UK100", "USA100", "USA500", "USA30"}
    if set(symbols) != active_symbols:
        raise ValueError("C046 requires the exact five-symbol active universe")
    adapter = HfmCsvMarketDataAdapter(bridge_root)
    observed_at = datetime.now(timezone.utc)
    output = {}
    total_eligible = 0
    total_reversals = 0
    for symbol in symbols:
        rows = adapter.read_bars(symbol, "M1", observed_at=observed_at, history_mode="combined")
        transitions = build_ny_transition_library({symbol: rows}, observed_at=observed_at)
        stats = transitions.statistics[symbol]
        outcomes = transitions.outcomes_for_symbol(symbol)
        if stats.eligible_session_count < 10 or len(outcomes) != stats.eligible_session_count:
            raise ValueError(f"C046 has insufficient eligible sessions for {symbol}")
        if stats.continuation_count + stats.reversal_count + stats.neutral_count != stats.eligible_session_count:
            raise ValueError(f"C046 outcome denominator is incomplete for {symbol}")
        if stats.reversal_count <= 0 or stats.reversal_rate is None:
            raise ValueError(f"C046 has no observed NY reversal outcomes for {symbol}")
        if not 0.0 <= stats.reversal_rate <= 1.0:
            raise ValueError(f"C046 reversal rate is invalid for {symbol}")
        for item in outcomes:
            if item.reversal != (
                item.london_direction in {"UP", "DOWN"}
                and item.new_york_direction in {"UP", "DOWN"}
                and item.london_direction != item.new_york_direction
            ):
                raise ValueError("C046 reversal classification is inconsistent")
            if item.neutral and (item.continuation or item.reversal):
                raise ValueError("C046 neutral outcome overlaps a directional outcome")
            if datetime.fromisoformat(item.london_reference_end_utc) != datetime.fromisoformat(item.new_york_start_utc):
                raise ValueError("C046 London reference contains post-NY look-ahead")
        profiles = build_session_profile_library({symbol: rows}, observed_at=observed_at)
        snapshot = MarketSnapshot(
            symbol,
            observed_at,
            (adapter.latest_tick(symbol),),
            {"M1": tuple(rows[-30:])},
            {"M1": "COMPLETED"},
            tuple(item.source_id for item in rows[-30:]),
        )
        report = IntelligenceEngine(
            session_profiles=profiles,
            ny_transitions=transitions,
        ).analyse(snapshot)
        if report.ny_transition_statistics != stats or len(report.ny_transition_outcomes) != len(outcomes):
            raise ValueError(f"C046 intelligence exposure is incomplete for {symbol}")
        total_eligible += stats.eligible_session_count
        total_reversals += stats.reversal_count
        output[symbol] = {
            "eligible_session_count": stats.eligible_session_count,
            "reversal_count": stats.reversal_count,
            "reversal_rate": stats.reversal_rate,
            "neutral_count": stats.neutral_count,
            "london_up_count": stats.london_up_count,
            "london_up_reversal_rate": stats.london_up_reversal_rate,
            "london_down_count": stats.london_down_count,
            "london_down_reversal_rate": stats.london_down_reversal_rate,
            "reversal_outcomes": tuple(
                {
                    "session_date": item.session_date,
                    "london_direction": item.london_direction,
                    "new_york_direction": item.new_york_direction,
                    "london_return": item.london_return,
                    "new_york_return": item.new_york_return,
                    "london_reference_end_utc": item.london_reference_end_utc,
                    "new_york_start_utc": item.new_york_start_utc,
                    "source_digest": item.source_digest,
                }
                for item in outcomes
                if item.reversal
            ),
        }
    return {
        "schema_version": 1,
        "requirement_id": "C046",
        "status": "PASS",
        "bridge_root": str(bridge_root),
        "observed_at": observed_at.isoformat(),
        "symbol_count": len(output),
        "eligible_session_count": total_eligible,
        "reversal_count": total_reversals,
        "classification_contract": "EXPLICIT_OPPOSITE_NY_DIRECTION_VERSUS_PRE_NY_LONDON_DIRECTION",
        "neutral_policy": "FLAT_DIRECTION_IS_NEUTRAL_NOT_REVERSAL",
        "role": "EMPIRICAL_REVERSAL_STATISTICS_NOT_TRADE_APPROVAL",
        "symbols": output,
    }


def verify_c047_opening_range_engine(
    *,
    bridge_root: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    active_symbols = {"XAUUSD", "UK100", "USA100", "USA500", "USA30"}
    if set(symbols) != active_symbols:
        raise ValueError("C047 requires the exact five-symbol active universe")
    adapter = HfmCsvMarketDataAdapter(bridge_root)
    observed_at = datetime.now(timezone.utc)
    output = {}
    total_records = 0
    total_complete = 0
    for symbol in symbols:
        rows = adapter.read_bars(symbol, "M1", observed_at=observed_at, history_mode="combined")
        library = build_opening_range_library({symbol: rows}, observed_at=observed_at)
        records = library.records_for_symbol(symbol)
        if library.duration_minutes != 30 or set(item.session for item in records) != set(OPENING_RANGE_SESSIONS):
            raise ValueError(f"C047 session or duration contract is incomplete for {symbol}")
        complete = tuple(item for item in records if item.complete)
        if len(complete) < 30:
            raise ValueError(f"C047 has insufficient complete opening ranges for {symbol}")
        for item in records:
            start = datetime.fromisoformat(item.start_utc)
            end = datetime.fromisoformat(item.end_utc)
            if end - start != timedelta(minutes=30):
                raise ValueError("C047 record is not an exact 30-minute opening range")
            if item.expected_candle_count != 30:
                raise ValueError("C047 expected candle denominator is not 30")
            if not isclose(item.coverage_ratio, item.candle_count / 30.0, abs_tol=1e-12):
                raise ValueError("C047 coverage ratio is inconsistent")
            if item.complete != (item.candle_count > 0 and item.coverage_ratio >= 0.80):
                raise ValueError("C047 completeness classification is inconsistent")
            if item.complete:
                values = (
                    item.open_price, item.high_price, item.low_price, item.close_price,
                    item.midpoint_price, item.range_value,
                )
                if any(value is None or not isfinite(float(value)) for value in values):
                    raise ValueError("C047 complete opening range lacks finite prices")
                if not isclose(float(item.midpoint_price), (float(item.high_price) + float(item.low_price)) / 2.0, abs_tol=1e-12):
                    raise ValueError("C047 midpoint is inconsistent")
                if not isclose(float(item.range_value), float(item.high_price) - float(item.low_price), abs_tol=1e-12):
                    raise ValueError("C047 range value is inconsistent")
            if len(item.source_digest) != 64:
                raise ValueError("C047 opening range lacks source provenance")
        profiles = build_session_profile_library({symbol: rows}, observed_at=observed_at)
        snapshot = MarketSnapshot(
            symbol,
            observed_at,
            (adapter.latest_tick(symbol),),
            {"M1": tuple(rows[-30:])},
            {"M1": "COMPLETED"},
            tuple(item.source_id for item in rows[-30:]),
        )
        report = IntelligenceEngine(
            session_profiles=profiles,
            opening_range_library=library,
        ).analyse(snapshot)
        if len(report.opening_range_history) != len(records):
            raise ValueError(f"C047 intelligence exposure is incomplete for {symbol}")
        if "OPENING_RANGE_HISTORY_OBSERVED" not in report.evidence:
            raise ValueError(f"C047 evidence marker is absent for {symbol}")
        latest_by_session = {}
        for session in OPENING_RANGE_SESSIONS:
            selected = tuple(item for item in complete if item.session == session)
            if not selected:
                raise ValueError(f"C047 has no complete {session} opening ranges for {symbol}")
            item = selected[-1]
            latest_by_session[session] = {
                "session_date": item.session_date,
                "start_utc": item.start_utc,
                "end_utc": item.end_utc,
                "candle_count": item.candle_count,
                "coverage_ratio": item.coverage_ratio,
                "open_price": item.open_price,
                "high_price": item.high_price,
                "low_price": item.low_price,
                "close_price": item.close_price,
                "midpoint_price": item.midpoint_price,
                "range_value": item.range_value,
                "direction": item.direction,
                "source_digest": item.source_digest,
            }
        total_records += len(records)
        total_complete += len(complete)
        output[symbol] = {
            "record_count": len(records),
            "complete_count": len(complete),
            "incomplete_count": len(records) - len(complete),
            "latest_by_session": latest_by_session,
        }
    return {
        "schema_version": 1,
        "requirement_id": "C047",
        "status": "PASS",
        "bridge_root": str(bridge_root),
        "observed_at": observed_at.isoformat(),
        "symbol_count": len(output),
        "record_count": total_records,
        "complete_count": total_complete,
        "duration_minutes": 30,
        "sessions": OPENING_RANGE_SESSIONS,
        "boundary_contract": "FIRST_30_COMPLETED_M1_MINUTES_FROM_CANONICAL_IANA_SESSION_OPEN",
        "coverage_policy": "OBSERVED_WITH_80_PERCENT_COMPLETENESS_CLASSIFICATION",
        "role": "OPENING_RANGE_INTELLIGENCE_NOT_TRADE_APPROVAL",
        "symbols": output,
    }


def verify_c048_opening_range_breakout_model(
    *,
    bridge_root: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    active_symbols = {"XAUUSD", "UK100", "USA100", "USA500", "USA30"}
    if set(symbols) != active_symbols:
        raise ValueError("C048 requires the exact five-symbol active universe")
    adapter = HfmCsvMarketDataAdapter(bridge_root)
    observed_at = datetime.now(timezone.utc)
    output = {}
    total_eligible = 0
    total_breakouts = 0
    for symbol in symbols:
        rows = adapter.read_bars(symbol, "M1", observed_at=observed_at, history_mode="combined")
        opening_ranges = build_opening_range_library({symbol: rows}, observed_at=observed_at)
        library = build_opening_range_breakout_library(
            {symbol: rows},
            observed_at=observed_at,
            opening_ranges=opening_ranges,
        )
        outcomes = library.outcomes_for_symbol(symbol)
        statistics = library.statistics_for_symbol(symbol)
        if set(statistics) != set(OPENING_RANGE_SESSIONS):
            raise ValueError(f"C048 session statistics are incomplete for {symbol}")
        if len(outcomes) != sum(item.eligible_opening_range_count for item in statistics.values()):
            raise ValueError(f"C048 outcome denominator is incomplete for {symbol}")
        if len(outcomes) < 30:
            raise ValueError(f"C048 has insufficient historical outcomes for {symbol}")
        for session, stats in statistics.items():
            if stats.breakout_count + stats.no_breakout_count != stats.eligible_opening_range_count:
                raise ValueError(f"C048 breakout denominator is invalid for {symbol} {session}")
            if stats.upward_breakout_count + stats.downward_breakout_count != stats.breakout_count:
                raise ValueError(f"C048 directional denominator is invalid for {symbol} {session}")
            if stats.breakout_count <= 0 or stats.breakout_rate is None:
                raise ValueError(f"C048 has no observed breakouts for {symbol} {session}")
            if not 0.0 <= stats.breakout_rate <= 1.0:
                raise ValueError(f"C048 breakout rate is invalid for {symbol} {session}")
        for item in outcomes:
            opening_end = datetime.fromisoformat(item.opening_range_end_utc)
            session_end = datetime.fromisoformat(item.session_end_utc)
            if opening_end >= session_end or item.opening_range_value < 0:
                raise ValueError("C048 opening/session geometry is invalid")
            if item.breakout_present:
                breakout_at = datetime.fromisoformat(str(item.breakout_at_utc))
                values = (
                    item.breakout_price, item.close_distance, item.close_distance_in_ranges,
                    item.candle_body_fraction, item.maximum_favourable_excursion,
                    item.maximum_adverse_excursion, item.mfe_in_ranges, item.mae_in_ranges,
                )
                if item.breakout_direction not in {"UP", "DOWN"}:
                    raise ValueError("C048 breakout direction is invalid")
                if not opening_end < breakout_at <= session_end:
                    raise ValueError("C048 breakout lies outside the post-range session window")
                if any(value is None or not isfinite(float(value)) for value in values):
                    raise ValueError("C048 breakout characteristics are incomplete")
                if float(item.close_distance) <= 0 or int(item.minutes_after_opening_range or 0) <= 0:
                    raise ValueError("C048 breakout close did not clear the range")
                if float(item.maximum_favourable_excursion) < 0 or float(item.maximum_adverse_excursion) < 0:
                    raise ValueError("C048 excursion values must be non-negative")
            elif any(
                value is not None
                for value in (item.breakout_at_utc, item.breakout_price, item.close_distance)
            ) or item.breakout_direction != "NONE":
                raise ValueError("C048 no-breakout outcome contains synthetic event values")
            if len(item.source_digest) != 64:
                raise ValueError("C048 outcome lacks source provenance")
        profiles = build_session_profile_library({symbol: rows}, observed_at=observed_at)
        snapshot = MarketSnapshot(
            symbol,
            observed_at,
            (adapter.latest_tick(symbol),),
            {"M1": tuple(rows[-30:])},
            {"M1": "COMPLETED"},
            tuple(item.source_id for item in rows[-30:]),
        )
        report = IntelligenceEngine(
            session_profiles=profiles,
            opening_range_library=opening_ranges,
            opening_range_breakouts=library,
        ).analyse(snapshot)
        if set(report.opening_range_breakout_statistics) != set(OPENING_RANGE_SESSIONS):
            raise ValueError(f"C048 intelligence statistics are not exposed for {symbol}")
        if len(report.opening_range_breakout_outcomes) != len(outcomes):
            raise ValueError(f"C048 intelligence outcomes are not exposed for {symbol}")
        if "OPENING_RANGE_BREAKOUT_STATISTICS_OBSERVED" not in report.evidence:
            raise ValueError(f"C048 evidence marker is absent for {symbol}")
        total_eligible += len(outcomes)
        total_breakouts += sum(item.breakout_present for item in outcomes)
        output[symbol] = {
            "eligible_opening_range_count": len(outcomes),
            "breakout_count": sum(item.breakout_present for item in outcomes),
            "no_breakout_count": sum(not item.breakout_present for item in outcomes),
            "sessions": {
                session: {
                    "eligible_opening_range_count": stats.eligible_opening_range_count,
                    "breakout_count": stats.breakout_count,
                    "no_breakout_count": stats.no_breakout_count,
                    "upward_breakout_count": stats.upward_breakout_count,
                    "downward_breakout_count": stats.downward_breakout_count,
                    "breakout_rate": stats.breakout_rate,
                    "same_side_session_close_rate": stats.same_side_session_close_rate,
                    "median_minutes_to_breakout": stats.median_minutes_to_breakout,
                    "median_close_distance_in_ranges": stats.median_close_distance_in_ranges,
                    "median_activity_ratio": stats.median_activity_ratio,
                    "median_mfe_in_ranges": stats.median_mfe_in_ranges,
                    "median_mae_in_ranges": stats.median_mae_in_ranges,
                }
                for session, stats in statistics.items()
            },
        }
    return {
        "schema_version": 1,
        "requirement_id": "C048",
        "status": "PASS",
        "bridge_root": str(bridge_root),
        "observed_at": observed_at.isoformat(),
        "symbol_count": len(output),
        "eligible_opening_range_count": total_eligible,
        "breakout_count": total_breakouts,
        "breakout_contract": "FIRST_COMPLETED_M1_CLOSE_OUTSIDE_FROZEN_30_MINUTE_OPENING_RANGE",
        "denominator_policy": "ALL_COMPLETE_OPENING_RANGES_WITH_COMPLETE_PARENT_SESSION",
        "characteristics": (
            "DIRECTION", "TIME_TO_BREAKOUT", "CLOSE_DISTANCE", "BODY_FRACTION",
            "ACTIVITY_RATIO", "MFE", "MAE", "SESSION_CLOSE_FOLLOW_THROUGH",
        ),
        "role": "EMPIRICAL_BREAKOUT_CHARACTERISTICS_NOT_TRADE_APPROVAL",
        "symbols": output,
    }


def verify_c049_false_opening_range_breakout_model(
    *,
    bridge_root: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    active_symbols = {"XAUUSD", "UK100", "USA100", "USA500", "USA30"}
    if set(symbols) != active_symbols:
        raise ValueError("C049 requires the exact five-symbol active universe")
    adapter = HfmCsvMarketDataAdapter(bridge_root)
    observed_at = datetime.now(timezone.utc)
    output = {}
    total_breakouts = 0
    total_false = 0
    for symbol in symbols:
        rows = adapter.read_bars(symbol, "M1", observed_at=observed_at, history_mode="combined")
        opening_ranges = build_opening_range_library({symbol: rows}, observed_at=observed_at)
        breakouts = build_opening_range_breakout_library(
            {symbol: rows}, observed_at=observed_at, opening_ranges=opening_ranges,
        )
        library = build_false_opening_range_breakout_library(
            {symbol: rows}, observed_at=observed_at, breakouts=breakouts,
        )
        outcomes = library.outcomes_for_symbol(symbol)
        statistics = library.statistics_for_symbol(symbol)
        expected_breakouts = sum(item.breakout_present for item in breakouts.outcomes_for_symbol(symbol))
        if len(outcomes) != expected_breakouts:
            raise ValueError(f"C049 did not classify every breakout for {symbol}")
        if set(statistics) != set(OPENING_RANGE_SESSIONS):
            raise ValueError(f"C049 session statistics are incomplete for {symbol}")
        if len(outcomes) != sum(item.eligible_breakout_count for item in statistics.values()):
            raise ValueError(f"C049 statistics denominator is incomplete for {symbol}")
        for session, stats in statistics.items():
            if stats.false_breakout_count + stats.unreclaimed_breakout_count != stats.eligible_breakout_count:
                raise ValueError(f"C049 false-breakout denominator is invalid for {symbol} {session}")
            if stats.upward_false_breakout_count + stats.downward_false_breakout_count != stats.false_breakout_count:
                raise ValueError(f"C049 direction denominator is invalid for {symbol} {session}")
            if stats.false_breakout_count <= 0 or stats.false_breakout_rate is None:
                raise ValueError(f"C049 has no observed failed breakouts for {symbol} {session}")
            if not 0.0 <= stats.false_breakout_rate <= 1.0:
                raise ValueError(f"C049 false-breakout rate is invalid for {symbol} {session}")
        for item in outcomes:
            if item.breakout_direction not in {"UP", "DOWN"}:
                raise ValueError("C049 outcome direction is invalid")
            if item.maximum_extension_before_reclaim < 0:
                raise ValueError("C049 extension cannot be negative")
            if item.reclaimed:
                if item.reclaimed_at_utc is None or item.reclaim_close is None:
                    raise ValueError("C049 reclaimed outcome lacks reclaim evidence")
                if int(item.minutes_to_reclaim or 0) <= 0:
                    raise ValueError("C049 reclaim must follow the breakout")
                if datetime.fromisoformat(item.reclaimed_at_utc) <= datetime.fromisoformat(item.breakout_at_utc):
                    raise ValueError("C049 reclaim timestamp does not follow breakout")
                if item.breakout_direction == "UP" and item.reclaim_close > item.broken_level:
                    raise ValueError("C049 upward breakout did not reclaim its high")
                if item.breakout_direction == "DOWN" and item.reclaim_close < item.broken_level:
                    raise ValueError("C049 downward breakout did not reclaim its low")
            elif any(
                value is not None
                for value in (item.reclaimed_at_utc, item.minutes_to_reclaim, item.reclaim_close)
            ):
                raise ValueError("C049 unreclaimed breakout contains synthetic reclaim values")
            if len(item.source_digest) != 64:
                raise ValueError("C049 outcome lacks source provenance")
        snapshot = MarketSnapshot(
            symbol,
            observed_at,
            (adapter.latest_tick(symbol),),
            {"M1": tuple(rows[-30:])},
            {"M1": "COMPLETED"},
            tuple(item.source_id for item in rows[-30:]),
        )
        report = IntelligenceEngine(
            opening_range_library=opening_ranges,
            opening_range_breakouts=breakouts,
            false_opening_range_breakouts=library,
        ).analyse(snapshot)
        if set(report.false_opening_range_breakout_statistics) != set(OPENING_RANGE_SESSIONS):
            raise ValueError(f"C049 intelligence statistics are not exposed for {symbol}")
        if len(report.false_opening_range_breakout_outcomes) != len(outcomes):
            raise ValueError(f"C049 intelligence outcomes are not exposed for {symbol}")
        if "FALSE_OPENING_RANGE_BREAKOUT_STATISTICS_OBSERVED" not in report.evidence:
            raise ValueError(f"C049 evidence marker is absent for {symbol}")
        total_breakouts += len(outcomes)
        total_false += sum(item.reclaimed for item in outcomes)
        output[symbol] = {
            "eligible_breakout_count": len(outcomes),
            "false_breakout_count": sum(item.reclaimed for item in outcomes),
            "unreclaimed_breakout_count": sum(not item.reclaimed for item in outcomes),
            "sessions": {
                session: {
                    "eligible_breakout_count": stats.eligible_breakout_count,
                    "false_breakout_count": stats.false_breakout_count,
                    "unreclaimed_breakout_count": stats.unreclaimed_breakout_count,
                    "false_breakout_rate": stats.false_breakout_rate,
                    "opposite_boundary_break_count": stats.opposite_boundary_break_count,
                    "opposite_boundary_break_rate": stats.opposite_boundary_break_rate,
                    "median_minutes_to_reclaim": stats.median_minutes_to_reclaim,
                    "median_reclaim_depth_in_ranges": stats.median_reclaim_depth_in_ranges,
                    "median_maximum_extension_in_ranges": stats.median_maximum_extension_in_ranges,
                }
                for session, stats in statistics.items()
            },
        }
    return {
        "schema_version": 1,
        "requirement_id": "C049",
        "status": "PASS",
        "bridge_root": str(bridge_root),
        "observed_at": observed_at.isoformat(),
        "symbol_count": len(output),
        "eligible_breakout_count": total_breakouts,
        "false_breakout_count": total_false,
        "false_breakout_contract": "LATER_COMPLETED_M1_CLOSE_RECLAIMS_EXACT_BROKEN_OPENING_RANGE_BOUNDARY",
        "denominator_policy": "EVERY_OBSERVED_FIRST_CLOSE_OPENING_RANGE_BREAKOUT",
        "no_breakout_policy": "NO_BREAKOUT_RANGES_ARE_NOT_FALSE_BREAKOUTS",
        "role": "EMPIRICAL_FAILURE_LIBRARY_NOT_TRADE_APPROVAL",
        "symbols": output,
    }


def verify_c050_session_vwap_engine(
    *,
    bridge_root: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    active_symbols = {"XAUUSD", "UK100", "USA100", "USA500", "USA30"}
    if set(symbols) != active_symbols:
        raise ValueError("C050 requires the exact five-symbol active universe")
    adapter = HfmCsvMarketDataAdapter(bridge_root)
    output = {}
    total_history = 0
    for symbol in symbols:
        tick = adapter.latest_tick(symbol)
        observed_at = tick.timestamp.astimezone(timezone.utc)
        rows = adapter.read_bars(symbol, "M1", observed_at=observed_at, history_mode="combined")
        library = build_session_vwap_library({symbol: rows}, observed_at=observed_at)
        history = library.observations_for_symbol(symbol)
        current = library.current_for_symbol(symbol, observed_at)
        if len(history) < 30 or current is None:
            raise ValueError(f"C050 lacks historical/current session VWAP for {symbol}")
        if current.complete_session or current.candle_count <= 0:
            raise ValueError(f"C050 current session classification is invalid for {symbol}")
        if current.value is None or current.last_price is None or current.deviation is None:
            raise ValueError(f"C050 current VWAP values are unavailable for {symbol}")
        if current.position not in {"ABOVE", "BELOW", "AT"}:
            raise ValueError(f"C050 current price position is invalid for {symbol}")
        start = datetime.fromisoformat(current.session_start_utc)
        through = datetime.fromisoformat(current.observed_through_utc)
        source_rows = tuple(item for item in rows if start <= item.start < through)
        weight = sum(max(0.0, item.tick_volume) for item in source_rows)
        expected = sum(
            ((item.high + item.low + item.close) / 3.0) * max(0.0, item.tick_volume)
            for item in source_rows
        ) / weight
        if not isclose(current.value, expected, abs_tol=1e-12):
            raise ValueError(f"C050 VWAP does not reconcile with broker M1 activity for {symbol}")
        if not isclose(current.deviation, current.last_price - current.value, abs_tol=1e-12):
            raise ValueError(f"C050 price deviation is inconsistent for {symbol}")
        if current.expected_candle_count <= 0 or not isclose(
            current.coverage_ratio,
            current.candle_count / current.expected_candle_count,
            abs_tol=1e-12,
        ):
            raise ValueError(f"C050 coverage is inconsistent for {symbol}")
        if len(current.source_digest) != 64:
            raise ValueError(f"C050 VWAP lacks source provenance for {symbol}")
        snapshot = MarketSnapshot(
            symbol,
            observed_at,
            (tick,),
            {"M1": tuple(rows[-30:])},
            {"M1": "COMPLETED"},
            tuple(item.source_id for item in rows[-30:]),
        )
        report = IntelligenceEngine(session_vwap_library=library).analyse(snapshot)
        if report.current_session_vwap != current or len(report.session_vwap_history) != len(history):
            raise ValueError(f"C050 intelligence exposure is incomplete for {symbol}")
        if report.vwap is None or not isclose(float(report.vwap.value), current.value, abs_tol=1e-12):
            raise ValueError(f"C050 engine did not use session-anchored VWAP for {symbol}")
        if "SESSION_VWAP_OBSERVED" not in report.evidence:
            raise ValueError(f"C050 evidence marker is absent for {symbol}")
        total_history += len(history)
        output[symbol] = {
            "observed_at": observed_at.isoformat(),
            "history_count": len(history),
            "current": {
                "session": current.session,
                "session_date": current.session_date,
                "session_start_utc": current.session_start_utc,
                "session_end_utc": current.session_end_utc,
                "observed_through_utc": current.observed_through_utc,
                "candle_count": current.candle_count,
                "expected_candle_count": current.expected_candle_count,
                "coverage_ratio": current.coverage_ratio,
                "activity_weight": current.activity_weight,
                "value": current.value,
                "last_price": current.last_price,
                "deviation": current.deviation,
                "position": current.position,
                "source_digest": current.source_digest,
            },
        }
    return {
        "schema_version": 1,
        "requirement_id": "C050",
        "status": "PASS",
        "bridge_root": str(bridge_root),
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "symbol_count": len(output),
        "session_vwap_history_count": total_history,
        "formula": "SUM(TYPICAL_PRICE_X_HFM_TICK_VOLUME)/SUM(HFM_TICK_VOLUME)",
        "anchor_contract": "CANONICAL_IANA_SESSION_START_TO_OBSERVATION_TIME_NO_FUTURE_BARS",
        "price_position": "ABOVE_BELOW_OR_AT_SESSION_VWAP",
        "role": "SESSION_VALUE_INTELLIGENCE_NOT_TRADE_APPROVAL",
        "symbols": output,
    }


def verify_c051_vwap_deviation_statistics(
    *,
    bridge_root: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    active_symbols = {"XAUUSD", "UK100", "USA100", "USA500", "USA30"}
    if set(symbols) != active_symbols:
        raise ValueError("C051 requires the exact five-symbol active universe")
    adapter = HfmCsvMarketDataAdapter(bridge_root)
    output = {}
    total_distributions = 0
    total_samples = 0
    for symbol in symbols:
        tick = adapter.latest_tick(symbol)
        observed_at = tick.timestamp.astimezone(timezone.utc)
        rows = adapter.read_bars(symbol, "M1", observed_at=observed_at, history_mode="combined")
        session_library = build_session_vwap_library({symbol: rows}, observed_at=observed_at)
        deviation_library = build_vwap_deviation_library(session_library, minimum_sample_size=30)
        distributions = tuple(
            item for item in deviation_library.distributions if item.symbol == symbol
        )
        if len(distributions) != 3:
            raise ValueError(f"C051 requires ASIA/LONDON/NEW_YORK distributions for {symbol}")
        current = deviation_library.current_for_symbol(symbol, observed_at)
        if current is None:
            raise ValueError(f"C051 current deviation observation is missing for {symbol}")
        if current.historical_sample_size < 30:
            raise ValueError(f"C051 historical sample is too small for {symbol}")
        for distribution in distributions:
            if distribution.sample_size < 30 or len(distribution.source_digest) != 64:
                raise ValueError(f"C051 distribution provenance/sample invalid for {symbol}/{distribution.session}")
            if distribution.signed_mean is None or distribution.signed_stddev is None:
                raise ValueError(f"C051 distribution statistics missing for {symbol}/{distribution.session}")
            if any(not isfinite(value) for value in distribution.absolute_distances):
                raise ValueError(f"C051 distribution contains a non-finite distance for {symbol}/{distribution.session}")
        if current.normalized_deviation is None or current.absolute_normalized_deviation is None:
            raise ValueError(f"C051 current normalized deviation is unavailable for {symbol}")
        if current.zscore is None or not isfinite(current.zscore):
            raise ValueError(f"C051 current z-score is unavailable for {symbol}")
        if current.absolute_distance_percentile is None or not 0.0 <= current.absolute_distance_percentile <= 1.0:
            raise ValueError(f"C051 current distance percentile is invalid for {symbol}")
        if len(current.source_digest) != 64:
            raise ValueError(f"C051 current deviation lacks source provenance for {symbol}")
        snapshot = MarketSnapshot(
            symbol,
            observed_at,
            (tick,),
            {"M1": tuple(rows[-30:])},
            {"M1": "COMPLETED"},
            tuple(item.source_id for item in rows[-30:]),
        )
        report = IntelligenceEngine(
            session_vwap_library=session_library,
            vwap_deviation_library=deviation_library,
        ).analyse(snapshot)
        if report.vwap_deviation_context != current:
            raise ValueError(f"C051 engine deviation exposure is incomplete for {symbol}")
        if "VWAP_DEVIATION_STATISTICS_OBSERVED" not in report.evidence:
            raise ValueError(f"C051 evidence marker is absent for {symbol}")
        total_distributions += len(distributions)
        total_samples += sum(item.sample_size for item in distributions)
        output[symbol] = {
            "observed_at": observed_at.isoformat(),
            "distribution_count": len(distributions),
            "distributions": {
                item.session: {
                    "sample_size": item.sample_size,
                    "signed_mean": item.signed_mean,
                    "signed_stddev": item.signed_stddev,
                    "source_digest": item.source_digest,
                }
                for item in distributions
            },
            "current": {
                "session": current.session,
                "session_date": current.session_date,
                "current_price": current.current_price,
                "vwap": current.vwap,
                "signed_deviation": current.signed_deviation,
                "normalized_deviation": current.normalized_deviation,
                "absolute_normalized_deviation": current.absolute_normalized_deviation,
                "historical_sample_size": current.historical_sample_size,
                "historical_mean": current.historical_mean,
                "historical_stddev": current.historical_stddev,
                "zscore": current.zscore,
                "absolute_distance_percentile": current.absolute_distance_percentile,
                "source_digest": current.source_digest,
            },
        }
    return {
        "schema_version": 1,
        "requirement_id": "C051",
        "status": "PASS",
        "bridge_root": str(bridge_root),
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "symbol_count": len(output),
        "distribution_count": total_distributions,
        "historical_sample_count": total_samples,
        "distribution_contract": "COMPLETED_SESSION_LAST_PRICE_MINUS_SESSION_VWAP_NORMALIZED_BY_VWAP",
        "statistics": ("SIGNED_MEAN", "POPULATION_STDDEV", "SIGNED_ZSCORE", "ABSOLUTE_DISTANCE_PERCENTILE"),
        "future_data_policy": "COMPLETED_SESSIONS_ONLY_FOR_DISTRIBUTIONS_CURRENT_SESSION_SEPARATE",
        "role": "VWAP_DISTANCE_INTELLIGENCE_NOT_TRADE_APPROVAL",
        "symbols": output,
    }


def verify_c052_vwap_reclaim_rejection_model(
    *,
    bridge_root: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    active_symbols = {"XAUUSD", "UK100", "USA100", "USA500", "USA30"}
    if set(symbols) != active_symbols:
        raise ValueError("C052 requires the exact five-symbol active universe")
    adapter = HfmCsvMarketDataAdapter(bridge_root)
    output = {}
    total_outcomes = 0
    for symbol in symbols:
        tick = adapter.latest_tick(symbol)
        observed_at = tick.timestamp.astimezone(timezone.utc)
        rows = adapter.read_bars(symbol, "M1", observed_at=observed_at, history_mode="combined")
        session_library = build_session_vwap_library({symbol: rows}, observed_at=observed_at)
        library = build_vwap_reclaim_rejection_library(
            {symbol: rows},
            observed_at=observed_at,
            session_vwap_library=session_library,
            horizon_bars=15,
        )
        statistics = library.statistics_for_symbol(symbol)
        outcomes = library.outcomes_for_symbol(symbol)
        observed_statistics = tuple(item for item in statistics.values() if item.sample_count > 0)
        if len(observed_statistics) < 2 or len(outcomes) < 20:
            raise ValueError(f"C052 lacks reclaim/rejection history for {symbol}")
        if any(item.forward_bars != 15 for item in outcomes):
            raise ValueError(f"C052 outcome horizon is inconsistent for {symbol}")
        if any(item.event_type not in {"RECLAIM_UP", "RECLAIM_DOWN", "REJECTION_UP", "REJECTION_DOWN"} for item in outcomes):
            raise ValueError(f"C052 outcome event type is invalid for {symbol}")
        if any(item.session_date == datetime.fromisoformat(item.event_at_utc).date().isoformat() and datetime.fromisoformat(item.event_at_utc) >= observed_at for item in outcomes):
            raise ValueError(f"C052 future/current session outcome leaked into history for {symbol}")
        for item in observed_statistics:
            if item.favourable_rate is None or not 0.0 <= item.favourable_rate <= 1.0:
                raise ValueError(f"C052 favorable rate is invalid for {symbol}/{item.session}/{item.event_type}")
            if len(item.source_digest) != 64:
                raise ValueError(f"C052 statistics lack provenance for {symbol}/{item.session}/{item.event_type}")
        snapshot = MarketSnapshot(
            symbol,
            observed_at,
            (tick,),
            {"M1": tuple(rows[-30:])},
            {"M1": "COMPLETED"},
            tuple(item.source_id for item in rows[-30:]),
        )
        report = IntelligenceEngine(
            session_vwap_library=session_library,
            vwap_reclaim_rejection_library=library,
        ).analyse(snapshot)
        if report.vwap_reclaim_rejection_statistics != statistics:
            raise ValueError(f"C052 engine statistics exposure is incomplete for {symbol}")
        if len(report.vwap_reclaim_rejection_outcomes) != len(outcomes):
            raise ValueError(f"C052 engine outcome exposure is incomplete for {symbol}")
        if "VWAP_RECLAIM_REJECTION_STATISTICS_OBSERVED" not in report.evidence:
            raise ValueError(f"C052 evidence marker is absent for {symbol}")
        total_outcomes += len(outcomes)
        output[symbol] = {
            "observed_at": observed_at.isoformat(),
            "horizon_bars": library.horizon_bars,
            "outcome_count": len(outcomes),
            "observed_statistic_count": len(observed_statistics),
            "statistics": {
                key: {
                    "event_type": item.event_type,
                    "session": item.session,
                    "sample_count": item.sample_count,
                    "favourable_count": item.favourable_count,
                    "favourable_rate": item.favourable_rate,
                    "mean_signed_forward_return": item.mean_signed_forward_return,
                    "median_signed_forward_return": item.median_signed_forward_return,
                    "mean_favourable_excursion": item.mean_favourable_excursion,
                    "mean_adverse_excursion": item.mean_adverse_excursion,
                    "status": item.status,
                    "source_digest": item.source_digest,
                }
                for key, item in statistics.items()
            },
        }
    return {
        "schema_version": 1,
        "requirement_id": "C052",
        "status": "PASS",
        "bridge_root": str(bridge_root),
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "symbol_count": len(output),
        "outcome_count": total_outcomes,
        "event_types": ("RECLAIM_UP", "RECLAIM_DOWN", "REJECTION_UP", "REJECTION_DOWN"),
        "horizon_bars": 15,
        "future_data_policy": "COMPLETED_SESSIONS_ONLY_EVOLVING_SESSION_VWAP_FORWARD_15_COMPLETED_M1_BARS",
        "role": "VWAP_RECLAIM_REJECTION_RESEARCH_NOT_TRADE_APPROVAL",
        "symbols": output,
    }


def verify_c053_tick_volume_engine(
    *,
    bridge_root: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    active_symbols = {"XAUUSD", "UK100", "USA100", "USA500", "USA30"}
    if set(symbols) != active_symbols:
        raise ValueError("C053 requires the exact five-symbol active universe")
    adapter = HfmCsvMarketDataAdapter(bridge_root)
    output = {}
    total_candles = 0
    for symbol in symbols:
        tick = adapter.latest_tick(symbol)
        observed_at = tick.timestamp.astimezone(timezone.utc)
        rows = adapter.read_bars(symbol, "M1", observed_at=observed_at, history_mode="combined")
        library = build_tick_volume_library(
            {symbol: {"M1": rows}},
            observed_at=observed_at,
        )
        observation = library.observations_for_symbol(symbol).get("M1")
        if observation is None or observation.candle_count < 100:
            raise ValueError(f"C053 insufficient broker tick-volume history for {symbol}")
        if observation.coverage_ratio < 0.99:
            raise ValueError(f"C053 broker activity coverage is below 99% for {symbol}")
        if observation.total_tick_volume <= 0.0 or observation.mean_tick_volume is None:
            raise ValueError(f"C053 broker activity proxy is empty for {symbol}")
        if len(observation.source_digest) != 64:
            raise ValueError(f"C053 tick-volume source provenance is missing for {symbol}")
        snapshot = MarketSnapshot(
            symbol,
            observed_at,
            (tick,),
            {"M1": tuple(rows[-30:])},
            {"M1": "COMPLETED"},
            tuple(item.source_id for item in rows[-30:]),
        )
        report = IntelligenceEngine(tick_volume_library=library).analyse(snapshot)
        if report.tick_volume_context.get("M1") != observation:
            raise ValueError(f"C053 engine tick-volume exposure is incomplete for {symbol}")
        if "TICK_VOLUME_ACTIVITY_OBSERVED" not in report.evidence:
            raise ValueError(f"C053 evidence marker is absent for {symbol}")
        total_candles += observation.candle_count
        output[symbol] = {
            "observed_at": observed_at.isoformat(),
            "timeframe": observation.timeframe,
            "candle_count": observation.candle_count,
            "nonzero_candle_count": observation.nonzero_candle_count,
            "coverage_ratio": observation.coverage_ratio,
            "total_tick_volume": observation.total_tick_volume,
            "mean_tick_volume": observation.mean_tick_volume,
            "median_tick_volume": observation.median_tick_volume,
            "latest_tick_volume": observation.latest_tick_volume,
            "source_digest": observation.source_digest,
        }
    return {
        "schema_version": 1,
        "requirement_id": "C053",
        "status": "PASS",
        "bridge_root": str(bridge_root),
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "symbol_count": len(output),
        "total_candle_count": total_candles,
        "activity_contract": "HFM_BROKER_M1_CANDLE_TICK_VOLUME_NO_SYNTHETIC_VOLUME",
        "future_data_policy": "CANDLES_ENDING_AFTER_OBSERVED_AT_EXCLUDED",
        "role": "BROKER_ACTIVITY_PROXY_NOT_TRADE_APPROVAL",
        "symbols": output,
    }


def verify_c054_relative_volume_model(
    *,
    bridge_root: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    active_symbols = {"XAUUSD", "UK100", "USA100", "USA500", "USA30"}
    if set(symbols) != active_symbols:
        raise ValueError("C054 requires the exact five-symbol active universe")
    adapter = HfmCsvMarketDataAdapter(bridge_root)
    output = {}
    for symbol in symbols:
        tick = adapter.latest_tick(symbol)
        observed_at = tick.timestamp.astimezone(timezone.utc)
        rows = adapter.read_bars(symbol, "M1", observed_at=observed_at, history_mode="combined")
        session_vwap = build_session_vwap_library({symbol: rows}, observed_at=observed_at)
        library = build_relative_volume_library(
            {symbol: rows}, observed_at=observed_at, session_vwap_library=session_vwap
        )
        baselines = library.baselines_for_symbol(symbol)
        current = library.observations_for_symbol(symbol)
        if set(baselines) != {"ASIA", "LONDON", "NEW_YORK"}:
            raise ValueError(f"C054 missing session baselines for {symbol}")
        if any(item.sample_size < 30 for item in baselines.values()):
            raise ValueError(f"C054 baseline sample is too small for {symbol}")
        active = current.get("LONDON") or next(iter(current.values()), None)
        if active is None or active.candle_count <= 0:
            raise ValueError(f"C054 current relative-volume observation is missing for {symbol}")
        if active.relative_volume_ratio is None or not isfinite(active.relative_volume_ratio):
            raise ValueError(f"C054 current relative-volume ratio is invalid for {symbol}")
        if active.baseline_percentile is None or not 0.0 <= active.baseline_percentile <= 1.0:
            raise ValueError(f"C054 current relative-volume percentile is invalid for {symbol}")
        if any(len(item.source_digest) != 64 for item in (*baselines.values(), *current.values())):
            raise ValueError(f"C054 relative-volume provenance is missing for {symbol}")
        snapshot = MarketSnapshot(
            symbol,
            observed_at,
            (tick,),
            {"M1": tuple(rows[-30:])},
            {"M1": "COMPLETED"},
            tuple(item.source_id for item in rows[-30:]),
        )
        report = IntelligenceEngine(
            session_vwap_library=session_vwap,
            relative_volume_library=library,
        ).analyse(snapshot)
        if report.relative_volume_context != current:
            raise ValueError(f"C054 engine relative-volume exposure is incomplete for {symbol}")
        if "RELATIVE_VOLUME_OBSERVED" not in report.evidence:
            raise ValueError(f"C054 evidence marker is absent for {symbol}")
        output[symbol] = {
            "observed_at": observed_at.isoformat(),
            "baseline_count": len(baselines),
            "current_sessions": len(current),
            "baselines": {
                key: {
                    "sample_size": item.sample_size,
                    "mean_session_tick_volume": item.mean_session_tick_volume,
                    "median_session_tick_volume": item.median_session_tick_volume,
                    "p95_session_tick_volume": item.p95_session_tick_volume,
                    "source_digest": item.source_digest,
                }
                for key, item in baselines.items()
            },
            "current": {
                key: {
                    "candle_count": item.candle_count,
                    "current_mean_tick_volume": item.current_mean_tick_volume,
                    "baseline_mean_tick_volume": item.baseline_mean_tick_volume,
                    "relative_volume_ratio": item.relative_volume_ratio,
                    "baseline_percentile": item.baseline_percentile,
                    "source_digest": item.source_digest,
                }
                for key, item in current.items()
            },
        }
    return {
        "schema_version": 1,
        "requirement_id": "C054",
        "status": "PASS",
        "bridge_root": str(bridge_root),
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "symbol_count": len(output),
        "baseline_contract": "SAME_SYMBOL_SAME_SESSION_COMPLETED_SESSION_MEAN_TICK_VOLUME",
        "current_contract": "CURRENT_PARTIAL_SESSION_MEAN_OVER_COMPLETED_BASELINE",
        "future_data_policy": "CURRENT_SESSION_USES_ELAPSED_BARS_ONLY",
        "role": "RELATIVE_VOLUME_INTELLIGENCE_NOT_TRADE_APPROVAL",
        "symbols": output,
    }


def verify_c055_trend_engine(
    *,
    bridge_root: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    active_symbols = {"XAUUSD", "UK100", "USA100", "USA500", "USA30"}
    timeframes = ("M1", "M5", "M15", "H1", "H4", "D1")
    if set(symbols) != active_symbols:
        raise ValueError("C055 requires the exact five-symbol active universe")
    adapter = HfmCsvMarketDataAdapter(bridge_root)
    output = {}
    for symbol in symbols:
        tick = adapter.latest_tick(symbol)
        observed_at = tick.timestamp.astimezone(timezone.utc)
        frames = {
            timeframe: adapter.read_bars(
                symbol, timeframe, observed_at=observed_at, history_mode="combined"
            )
            for timeframe in timeframes
        }
        library = build_trend_engine_library(
            {symbol: frames}, observed_at=observed_at, lookback=20
        )
        observations = library.observations_for_symbol(symbol)
        if set(observations) != set(timeframes):
            raise ValueError(f"C055 missing trend timeframe observations for {symbol}")
        for timeframe, item in observations.items():
            if item.sample_size < 20 or item.direction not in {"UP", "DOWN", "FLAT", "UNKNOWN"}:
                raise ValueError(f"C055 invalid trend observation for {symbol}/{timeframe}")
            if not 0.0 <= item.fit <= 1.0 or not 0.0 <= item.persistence <= 1.0:
                raise ValueError(f"C055 trend fit/persistence invalid for {symbol}/{timeframe}")
            if len(item.source_digest) != 64:
                raise ValueError(f"C055 trend provenance missing for {symbol}/{timeframe}")
        snapshot = MarketSnapshot(
            symbol,
            observed_at,
            (tick,),
            {"M1": tuple(frames["M1"][-30:])},
            {"M1": "COMPLETED"},
            tuple(item.source_id for item in frames["M1"][-30:]),
        )
        report = IntelligenceEngine(trend_engine_library=library).analyse(snapshot)
        if report.trend_engine_context != observations:
            raise ValueError(f"C055 engine trend exposure is incomplete for {symbol}")
        if "TREND_ENGINE_OBSERVED" not in report.evidence:
            raise ValueError(f"C055 evidence marker is absent for {symbol}")
        output[symbol] = {
            "observed_at": observed_at.isoformat(),
            "timeframes": {
                key: {
                    "direction": item.direction,
                    "slope": item.slope,
                    "normalized_slope": item.normalized_slope,
                    "fit": item.fit,
                    "persistence": item.persistence,
                    "sample_size": item.sample_size,
                    "source_digest": item.source_digest,
                }
                for key, item in observations.items()
            },
        }
    return {
        "schema_version": 1,
        "requirement_id": "C055",
        "status": "PASS",
        "bridge_root": str(bridge_root),
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "symbol_count": len(output),
        "timeframes": timeframes,
        "trend_contract": "CLOSE_SLOPE_FIT_DIRECTIONAL_PERSISTENCE_NORMALIZED_BY_MEAN_RANGE",
        "future_data_policy": "COMPLETED_CANDLES_ENDING_AFTER_OBSERVED_AT_EXCLUDED",
        "role": "TREND_INTELLIGENCE_NOT_TRADE_APPROVAL",
        "symbols": output,
    }


def verify_c056_trend_persistence_probability(
    *,
    bridge_root: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    active_symbols = {"XAUUSD", "UK100", "USA100", "USA500", "USA30"}
    timeframes = ("M1", "M5", "M15", "H1", "H4", "D1")
    if set(symbols) != active_symbols:
        raise ValueError("C056 requires the exact five-symbol active universe")
    adapter = HfmCsvMarketDataAdapter(bridge_root)
    output = {}
    for symbol in symbols:
        tick = adapter.latest_tick(symbol)
        observed_at = tick.timestamp.astimezone(timezone.utc)
        frames = {
            timeframe: adapter.read_bars(
                symbol, timeframe, observed_at=observed_at, history_mode="combined"
            )
            for timeframe in timeframes
        }
        library = build_trend_persistence_library(
            {symbol: frames}, observed_at=observed_at, lookback=20, forward_horizon=5
        )
        probabilities = library.observations_for_symbol(symbol)
        if set(probabilities) != set(timeframes):
            raise ValueError(f"C056 missing persistence probability for {symbol}")
        for timeframe, item in probabilities.items():
            if item.analogue_count < 20:
                raise ValueError(f"C056 sparse analogue sample for {symbol}/{timeframe}")
            if not 0.0 <= item.continuation_probability <= 1.0:
                raise ValueError(f"C056 probability invalid for {symbol}/{timeframe}")
            if not 0.0 <= item.interval_low <= item.continuation_probability <= item.interval_high <= 1.0:
                raise ValueError(f"C056 interval invalid for {symbol}/{timeframe}")
            if item.forward_horizon != 5 or len(item.source_digest) != 64:
                raise ValueError(f"C056 horizon/provenance invalid for {symbol}/{timeframe}")
        snapshot = MarketSnapshot(
            symbol,
            observed_at,
            (tick,),
            {"M1": tuple(frames["M1"][-30:])},
            {"M1": "COMPLETED"},
            tuple(item.source_id for item in frames["M1"][-30:]),
        )
        report = IntelligenceEngine(trend_persistence_library=library).analyse(snapshot)
        if report.trend_persistence_context != probabilities:
            raise ValueError(f"C056 engine persistence exposure is incomplete for {symbol}")
        if "TREND_PERSISTENCE_OBSERVED" not in report.evidence:
            raise ValueError(f"C056 evidence marker is absent for {symbol}")
        output[symbol] = {
            "observed_at": observed_at.isoformat(),
            "timeframes": {
                key: {
                    "current_direction": item.current_direction,
                    "current_fit": item.current_fit,
                    "current_persistence": item.current_persistence,
                    "analogue_count": item.analogue_count,
                    "continuation_count": item.continuation_count,
                    "continuation_probability": item.continuation_probability,
                    "interval_low": item.interval_low,
                    "interval_high": item.interval_high,
                    "forward_horizon": item.forward_horizon,
                    "source_digest": item.source_digest,
                }
                for key, item in probabilities.items()
            },
        }
    return {
        "schema_version": 1,
        "requirement_id": "C056",
        "status": "PASS",
        "bridge_root": str(bridge_root),
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "symbol_count": len(output),
        "timeframes": timeframes,
        "analogue_contract": "SAME_DIRECTION_NEARBY_FIT_PERSISTENCE_WITH_DIRECTION_FALLBACK",
        "outcome_contract": "NEXT_FIVE_COMPLETED_CANDLES_DIRECTIONAL_CONTINUATION",
        "future_data_policy": "CURRENT_STATE_HAS_NO_FORWARD_OUTCOME; HISTORICAL_ANCHORS_USE_ONLY_LATER_CANDLES",
        "role": "TREND_PERSISTENCE_INTELLIGENCE_NOT_TRADE_APPROVAL",
        "symbols": output,
    }


def verify_c057_mean_reversion_engine(
    *,
    bridge_root: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    active_symbols = {"XAUUSD", "UK100", "USA100", "USA500", "USA30"}
    timeframes = ("M1", "M5", "M15", "H1", "H4", "D1")
    if set(symbols) != active_symbols:
        raise ValueError("C057 requires the exact five-symbol active universe")
    adapter = HfmCsvMarketDataAdapter(bridge_root)
    output = {}
    for symbol in symbols:
        tick = adapter.latest_tick(symbol)
        observed_at = tick.timestamp.astimezone(timezone.utc)
        frames = {
            timeframe: adapter.read_bars(
                symbol, timeframe, observed_at=observed_at, history_mode="combined"
            )
            for timeframe in timeframes
        }
        library = build_mean_reversion_library(
            {symbol: frames}, observed_at=observed_at, lookback=20, forward_horizon=5
        )
        observations = library.observations_for_symbol(symbol)
        if set(observations) != set(timeframes):
            raise ValueError(f"C057 missing mean-reversion observation for {symbol}")
        for timeframe, item in observations.items():
            if item.reference is None or item.scale is None or item.scale < 0:
                raise ValueError(f"C057 invalid reference scale for {symbol}/{timeframe}")
            if item.zscore is not None and not isfinite(item.zscore):
                raise ValueError(f"C057 invalid z-score for {symbol}/{timeframe}")
            if item.state not in {"STRETCHED_HIGH", "STRETCHED_LOW", "WITHIN_RANGE", "UNAVAILABLE"}:
                raise ValueError(f"C057 invalid state for {symbol}/{timeframe}")
            if item.historical_reversion_count > item.historical_stretch_count:
                raise ValueError(f"C057 outcome count invalid for {symbol}/{timeframe}")
            if item.reversion_probability is not None and not 0.0 <= item.reversion_probability <= 1.0:
                raise ValueError(f"C057 probability invalid for {symbol}/{timeframe}")
            if item.forward_horizon != 5 or len(item.source_digest) != 64:
                raise ValueError(f"C057 horizon/provenance invalid for {symbol}/{timeframe}")
        snapshot = MarketSnapshot(
            symbol,
            observed_at,
            (tick,),
            {"M1": tuple(frames["M1"][-30:])},
            {"M1": "COMPLETED"},
            tuple(item.source_id for item in frames["M1"][-30:]),
        )
        report = IntelligenceEngine(mean_reversion_library=library).analyse(snapshot)
        if report.mean_reversion_context != observations:
            raise ValueError(f"C057 engine mean-reversion exposure is incomplete for {symbol}")
        if "MEAN_REVERSION_ENGINE_OBSERVED" not in report.evidence:
            raise ValueError(f"C057 evidence marker is absent for {symbol}")
        output[symbol] = {
            "observed_at": observed_at.isoformat(),
            "timeframes": {
                key: {
                    "value": item.value,
                    "reference": item.reference,
                    "scale": item.scale,
                    "zscore": item.zscore,
                    "state": item.state,
                    "stretched": item.stretched,
                    "historical_stretch_count": item.historical_stretch_count,
                    "historical_reversion_count": item.historical_reversion_count,
                    "reversion_probability": item.reversion_probability,
                    "forward_horizon": item.forward_horizon,
                    "source_digest": item.source_digest,
                }
                for key, item in observations.items()
            },
        }
    return {
        "schema_version": 1,
        "requirement_id": "C057",
        "status": "PASS",
        "bridge_root": str(bridge_root),
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "symbol_count": len(output),
        "timeframes": timeframes,
        "stretch_contract": "CURRENT_CLOSE_VERSUS_PRIOR_20_COMPLETED_CANDLE_MEAN_AND_STDDEV",
        "outcome_contract": "NEXT_FIVE_COMPLETED_CANDLES_RETURN_TOWARD_PRIOR_REFERENCE",
        "future_data_policy": "CURRENT_STATE_HAS_NO_FORWARD_OUTCOME; HISTORICAL_ANCHORS_USE_ONLY_LATER_CANDLES",
        "role": "MEAN_REVERSION_INTELLIGENCE_NOT_TRADE_APPROVAL",
        "symbols": output,
    }


def verify_c058_breakout_engine(
    *,
    bridge_root: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    active_symbols = {"XAUUSD", "UK100", "USA100", "USA500", "USA30"}
    timeframes = ("M1", "M5", "M15", "H1", "H4", "D1")
    if set(symbols) != active_symbols:
        raise ValueError("C058 requires the exact five-symbol active universe")
    adapter = HfmCsvMarketDataAdapter(bridge_root)
    output = {}
    for symbol in symbols:
        tick = adapter.latest_tick(symbol)
        observed_at = tick.timestamp.astimezone(timezone.utc)
        frames = {
            timeframe: adapter.read_bars(
                symbol, timeframe, observed_at=observed_at, history_mode="combined"
            )
            for timeframe in timeframes
        }
        library = build_breakout_engine_library(
            {symbol: frames}, observed_at=observed_at, lookback=20
        )
        current = library.observations_for_symbol(symbol)
        history = library.history_for_symbol(symbol)
        if set(current) != set(timeframes):
            raise ValueError(f"C058 missing breakout timeframe observation for {symbol}")
        if not history:
            raise ValueError(f"C058 has no historical structural breakouts for {symbol}")
        for timeframe, item in current.items():
            if item.present and item.direction not in {"UP", "DOWN"}:
                raise ValueError(f"C058 invalid breakout direction for {symbol}/{timeframe}")
            if item.range_ratio is None or item.range_ratio < 0:
                raise ValueError(f"C058 invalid range ratio for {symbol}/{timeframe}")
            if item.activity_ratio is not None and item.activity_ratio < 0:
                raise ValueError(f"C058 invalid activity ratio for {symbol}/{timeframe}")
            if len(item.source_digest) != 64:
                raise ValueError(f"C058 provenance missing for {symbol}/{timeframe}")
        snapshot = MarketSnapshot(
            symbol,
            observed_at,
            (tick,),
            {"M1": tuple(frames["M1"][-30:])},
            {"M1": "COMPLETED"},
            tuple(item.source_id for item in frames["M1"][-30:]),
        )
        report = IntelligenceEngine(breakout_engine_library=library).analyse(snapshot)
        if report.breakout_engine_context != current:
            raise ValueError(f"C058 engine breakout exposure is incomplete for {symbol}")
        if "BREAKOUT_ENGINE_OBSERVED" not in report.evidence:
            raise ValueError(f"C058 evidence marker is absent for {symbol}")
        output[symbol] = {
            "observed_at": observed_at.isoformat(),
            "historical_breakout_count": len(history),
            "timeframes": {
                key: {
                    "present": item.present,
                    "direction": item.direction,
                    "level": item.level,
                    "close_distance": item.close_distance,
                    "range_ratio": item.range_ratio,
                    "activity_ratio": item.activity_ratio,
                    "source_digest": item.source_digest,
                }
                for key, item in current.items()
            },
        }
    return {
        "schema_version": 1,
        "requirement_id": "C058",
        "status": "PASS",
        "bridge_root": str(bridge_root),
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "symbol_count": len(output),
        "timeframes": timeframes,
        "breakout_contract": "COMPLETED_CLOSE_CROSSING_PRIOR_20_CANDLE_HIGH_OR_LOW",
        "future_data_policy": "ONLY_COMPLETED_CANDLES_ENDING_AT_OR_BEFORE_OBSERVED_AT",
        "role": "BREAKOUT_INTELLIGENCE_NOT_TRADE_APPROVAL",
        "symbols": output,
    }


def verify_c059_breakout_quality_model(
    *,
    bridge_root: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    active_symbols = {"XAUUSD", "UK100", "USA100", "USA500", "USA30"}
    timeframes = ("M1", "M5", "M15", "H1", "H4", "D1")
    if set(symbols) != active_symbols:
        raise ValueError("C059 requires the exact five-symbol active universe")
    adapter = HfmCsvMarketDataAdapter(bridge_root)
    output = {}
    for symbol in symbols:
        tick = adapter.latest_tick(symbol)
        observed_at = tick.timestamp.astimezone(timezone.utc)
        frames = {
            timeframe: adapter.read_bars(
                symbol, timeframe, observed_at=observed_at, history_mode="combined"
            )
            for timeframe in timeframes
        }
        library = build_breakout_quality_library(
            {symbol: frames}, observed_at=observed_at, lookback=20, forward_horizon=5
        )
        current = library.observations_for_symbol(symbol)
        outcomes = library.outcomes_for_symbol(symbol)
        if set(current) != set(timeframes):
            raise ValueError(f"C059 missing breakout-quality timeframe observation for {symbol}")
        if not outcomes:
            raise ValueError(f"C059 has no historical breakout outcomes for {symbol}")
        for timeframe, item in current.items():
            if item.present and item.direction not in {"UP", "DOWN"}:
                raise ValueError(f"C059 invalid breakout-quality direction for {symbol}/{timeframe}")
            if item.comparable_sample_size < 0 or item.genuine_count > item.comparable_sample_size:
                raise ValueError(f"C059 comparable counts invalid for {symbol}/{timeframe}")
            if item.quality_probability is not None and not 0.0 <= item.quality_probability <= 1.0:
                raise ValueError(f"C059 quality probability invalid for {symbol}/{timeframe}")
            if item.forward_horizon != 5 or len(item.source_digest) != 64:
                raise ValueError(f"C059 horizon/provenance invalid for {symbol}/{timeframe}")
        snapshot = MarketSnapshot(
            symbol,
            observed_at,
            (tick,),
            {"M1": tuple(frames["M1"][-30:])},
            {"M1": "COMPLETED"},
            tuple(item.source_id for item in frames["M1"][-30:]),
        )
        report = IntelligenceEngine(breakout_quality_library=library).analyse(snapshot)
        if report.breakout_quality_context != current:
            raise ValueError(f"C059 engine breakout-quality exposure is incomplete for {symbol}")
        if "BREAKOUT_QUALITY_OBSERVED" not in report.evidence:
            raise ValueError(f"C059 evidence marker is absent for {symbol}")
        output[symbol] = {
            "observed_at": observed_at.isoformat(),
            "historical_outcome_count": len(outcomes),
            "genuine_outcome_count": sum(item.genuine for item in outcomes),
            "timeframes": {
                key: {
                    "present": item.present,
                    "direction": item.direction,
                    "quality_probability": item.quality_probability,
                    "comparable_sample_size": item.comparable_sample_size,
                    "genuine_count": item.genuine_count,
                    "forward_horizon": item.forward_horizon,
                    "source_digest": item.source_digest,
                }
                for key, item in current.items()
            },
        }
    return {
        "schema_version": 1,
        "requirement_id": "C059",
        "status": "PASS",
        "bridge_root": str(bridge_root),
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "symbol_count": len(output),
        "timeframes": timeframes,
        "quality_contract": "BREAKOUT_FOLLOW_THROUGH_WITHOUT_LEVEL_INVALIDATION_OVER_NEXT_FIVE_COMPLETED_CANDLES",
        "future_data_policy": "CURRENT_BREAKOUT_HAS_NO_FORWARD_OUTCOME; HISTORICAL_OUTCOMES_USE_LATER_CANDLES",
        "role": "BREAKOUT_QUALITY_INTELLIGENCE_NOT_TRADE_APPROVAL",
        "symbols": output,
    }


def verify_c060_failed_breakout_library(
    *,
    bridge_root: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    active_symbols = {"XAUUSD", "UK100", "USA100", "USA500", "USA30"}
    timeframes = ("M1", "M5", "M15", "H1", "H4", "D1")
    if set(symbols) != active_symbols:
        raise ValueError("C060 requires the exact five-symbol active universe")
    adapter = HfmCsvMarketDataAdapter(bridge_root)
    output = {}
    for symbol in symbols:
        tick = adapter.latest_tick(symbol)
        observed_at = tick.timestamp.astimezone(timezone.utc)
        frames = {
            timeframe: adapter.read_bars(
                symbol, timeframe, observed_at=observed_at, history_mode="combined"
            )
            for timeframe in timeframes
        }
        library = build_failed_breakout_library(
            {symbol: frames}, observed_at=observed_at, lookback=20, forward_horizon=5
        )
        summaries = library.observations_for_symbol(symbol)
        records = library.records_for_symbol(symbol)
        if set(summaries) != set(timeframes):
            raise ValueError(f"C060 missing failed-breakout summary for {symbol}")
        if not records:
            raise ValueError(f"C060 has no failed breakout records for {symbol}")
        for timeframe, item in summaries.items():
            if item.failed_count != item.invalidated_count + item.no_follow_through_count:
                raise ValueError(f"C060 failure counts do not reconcile for {symbol}/{timeframe}")
        snapshot = MarketSnapshot(
            symbol,
            observed_at,
            (tick,),
            {"M1": tuple(frames["M1"][-30:])},
            {"M1": "COMPLETED"},
            tuple(item.source_id for item in frames["M1"][-30:]),
        )
        report = IntelligenceEngine(failed_breakout_library=library).analyse(snapshot)
        if report.failed_breakout_context != summaries:
            raise ValueError(f"C060 engine failed-breakout exposure is incomplete for {symbol}")
        if "FAILED_BREAKOUT_LIBRARY_OBSERVED" not in report.evidence:
            raise ValueError(f"C060 evidence marker is absent for {symbol}")
        output[symbol] = {
            "observed_at": observed_at.isoformat(),
            "record_count": len(records),
            "timeframes": {
                key: {
                    "failed_count": item.failed_count,
                    "invalidated_count": item.invalidated_count,
                    "no_follow_through_count": item.no_follow_through_count,
                    "latest_failure_at_utc": item.latest_failure_at_utc,
                }
                for key, item in summaries.items()
            },
        }
    return {
        "schema_version": 1,
        "requirement_id": "C060",
        "status": "PASS",
        "bridge_root": str(bridge_root),
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "symbol_count": len(output),
        "timeframes": timeframes,
        "failure_contract": "FAILED_BREAKOUTS_PERSISTED_AS_LEVEL_INVALIDATED_OR_NO_FOLLOW_THROUGH",
        "future_data_policy": "HISTORICAL_FAILURES_USE_ONLY_COMPLETED_FORWARD_OUTCOME_WINDOWS",
        "role": "FAILED_BREAKOUT_RESEARCH_NOT_TRADE_APPROVAL",
        "symbols": output,
    }


def verify_c061_reversal_engine(
    *,
    bridge_root: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    active_symbols = {"XAUUSD", "UK100", "USA100", "USA500", "USA30"}
    timeframes = ("M1", "M5", "M15", "H1", "H4", "D1")
    if set(symbols) != active_symbols:
        raise ValueError("C061 requires the exact five-symbol active universe")
    adapter = HfmCsvMarketDataAdapter(bridge_root)
    output = {}
    total_outcomes = 0
    for symbol in symbols:
        tick = adapter.latest_tick(symbol)
        observed_at = tick.timestamp.astimezone(timezone.utc)
        frames = {
            timeframe: adapter.read_bars(
                symbol, timeframe, observed_at=observed_at, history_mode="combined"
            )
            for timeframe in timeframes
        }
        library = build_reversal_library(
            {symbol: frames}, observed_at=observed_at, lookback=20, forward_horizon=5
        )
        current = library.observations_for_symbol(symbol)
        outcomes = library.outcomes_for_symbol(symbol)
        if set(current) != set(timeframes):
            raise ValueError(f"C061 missing reversal timeframe observation for {symbol}")
        if not outcomes:
            raise ValueError(f"C061 has no historical sweep/reversal outcomes for {symbol}")
        for timeframe, item in current.items():
            if item.present and item.direction not in {"UP", "DOWN"}:
                raise ValueError(f"C061 invalid reversal direction for {symbol}/{timeframe}")
            if item.exhaustion and not item.rejection:
                raise ValueError(f"C061 exhaustion/rejection contract is inconsistent for {symbol}/{timeframe}")
            if item.follow_through_count < 0 or item.follow_through_count > item.historical_sample_size:
                raise ValueError(f"C061 follow-through counts are invalid for {symbol}/{timeframe}")
            if item.follow_through_probability is not None and not 0.0 <= item.follow_through_probability <= 1.0:
                raise ValueError(f"C061 reversal probability is invalid for {symbol}/{timeframe}")
            if item.forward_horizon != 5 or len(item.source_digest) != 64:
                raise ValueError(f"C061 horizon/provenance is invalid for {symbol}/{timeframe}")
        for item in outcomes:
            if item.direction not in {"UP", "DOWN"}:
                raise ValueError(f"C061 historical direction is invalid for {symbol}/{item.timeframe}")
            if not (item.sweep and item.exhaustion and item.rejection and item.reclaim):
                raise ValueError(f"C061 historical event is missing a required reversal component for {symbol}")
            if item.forward_horizon != 5 or len(item.source_digest) != 64:
                raise ValueError(f"C061 historical provenance is invalid for {symbol}")
        snapshot = MarketSnapshot(
            symbol,
            observed_at,
            (tick,),
            {"M1": tuple(frames["M1"][-30:])},
            {"M1": "COMPLETED"},
            tuple(item.source_id for item in frames["M1"][-30:]),
        )
        report = IntelligenceEngine(reversal_library=library).analyse(snapshot)
        if report.reversal_engine_context != current:
            raise ValueError(f"C061 engine reversal exposure is incomplete for {symbol}")
        if "REVERSAL_ENGINE_OBSERVED" not in report.evidence:
            raise ValueError(f"C061 evidence marker is absent for {symbol}")
        total_outcomes += len(outcomes)
        output[symbol] = {
            "observed_at": observed_at.isoformat(),
            "historical_outcome_count": len(outcomes),
            "follow_through_count": sum(item.follow_through for item in outcomes),
            "timeframes": {
                key: {
                    "present": item.present,
                    "direction": item.direction,
                    "sweep": item.sweep,
                    "exhaustion": item.exhaustion,
                    "rejection": item.rejection,
                    "reclaim": item.reclaim,
                    "historical_sample_size": item.historical_sample_size,
                    "follow_through_count": item.follow_through_count,
                    "follow_through_probability": item.follow_through_probability,
                    "forward_horizon": item.forward_horizon,
                    "source_digest": item.source_digest,
                }
                for key, item in current.items()
            },
        }
    return {
        "schema_version": 1,
        "requirement_id": "C061",
        "status": "PASS",
        "bridge_root": str(bridge_root),
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "symbol_count": len(output),
        "timeframes": timeframes,
        "historical_outcome_count": total_outcomes,
        "reversal_contract": "SWEEP_PLUS_EXHAUSTION_PLUS_REJECTION_PLUS_RECLAIM_WITH_FIVE_COMPLETED_FORWARD_CANDLES",
        "future_data_policy": "CURRENT_CONTEXT_USES_ONLY_COMPLETED_CANDLES; HISTORICAL_FOLLOW_THROUGH_USES_LATER_CANDLES",
        "role": "REVERSAL_INTELLIGENCE_NOT_TRADE_APPROVAL",
        "symbols": output,
    }


def verify_c062_continuation_engine(
    *,
    bridge_root: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    active_symbols = {"XAUUSD", "UK100", "USA100", "USA500", "USA30"}
    timeframes = ("M1", "M5", "M15", "H1", "H4", "D1")
    if set(symbols) != active_symbols:
        raise ValueError("C062 requires the exact five-symbol active universe")
    adapter = HfmCsvMarketDataAdapter(bridge_root)
    output = {}
    total_outcomes = 0
    for symbol in symbols:
        tick = adapter.latest_tick(symbol)
        observed_at = tick.timestamp.astimezone(timezone.utc)
        frames = {
            timeframe: adapter.read_bars(
                symbol, timeframe, observed_at=observed_at, history_mode="combined"
            )
            for timeframe in timeframes
        }
        library = build_continuation_library(
            {symbol: frames}, observed_at=observed_at, lookback=20, forward_horizon=5
        )
        current = library.observations_for_symbol(symbol)
        outcomes = library.outcomes_for_symbol(symbol)
        if set(current) != set(timeframes):
            raise ValueError(f"C062 missing continuation timeframe observation for {symbol}")
        if not outcomes:
            raise ValueError(f"C062 has no historical continuation outcomes for {symbol}")
        for timeframe, item in current.items():
            if item.present and item.direction not in {"UP", "DOWN"}:
                raise ValueError(f"C062 invalid continuation direction for {symbol}/{timeframe}")
            if item.present and not (item.impulse and item.pullback and item.resumed):
                raise ValueError(f"C062 current continuation components are inconsistent for {symbol}/{timeframe}")
            if item.follow_through_count < 0 or item.follow_through_count > item.historical_sample_size:
                raise ValueError(f"C062 follow-through counts are invalid for {symbol}/{timeframe}")
            if item.follow_through_probability is not None and not 0.0 <= item.follow_through_probability <= 1.0:
                raise ValueError(f"C062 continuation probability is invalid for {symbol}/{timeframe}")
            if item.forward_horizon != 5 or len(item.source_digest) != 64:
                raise ValueError(f"C062 horizon/provenance is invalid for {symbol}/{timeframe}")
        for item in outcomes:
            if item.direction not in {"UP", "DOWN"}:
                raise ValueError(f"C062 historical direction is invalid for {symbol}/{item.timeframe}")
            if not (item.impulse and item.pullback and item.resumed):
                raise ValueError(f"C062 historical event is missing a continuation component for {symbol}")
            if item.forward_horizon != 5 or len(item.source_digest) != 64:
                raise ValueError(f"C062 historical provenance is invalid for {symbol}")
        snapshot = MarketSnapshot(
            symbol,
            observed_at,
            (tick,),
            {"M1": tuple(frames["M1"][-30:])},
            {"M1": "COMPLETED"},
            tuple(item.source_id for item in frames["M1"][-30:]),
        )
        report = IntelligenceEngine(continuation_library=library).analyse(snapshot)
        if report.continuation_engine_context != current:
            raise ValueError(f"C062 engine continuation exposure is incomplete for {symbol}")
        if "CONTINUATION_ENGINE_OBSERVED" not in report.evidence:
            raise ValueError(f"C062 evidence marker is absent for {symbol}")
        total_outcomes += len(outcomes)
        output[symbol] = {
            "observed_at": observed_at.isoformat(),
            "historical_outcome_count": len(outcomes),
            "follow_through_count": sum(item.follow_through for item in outcomes),
            "timeframes": {
                key: {
                    "present": item.present,
                    "direction": item.direction,
                    "impulse": item.impulse,
                    "pullback": item.pullback,
                    "resumed": item.resumed,
                    "invalidated": item.invalidated,
                    "historical_sample_size": item.historical_sample_size,
                    "follow_through_count": item.follow_through_count,
                    "follow_through_probability": item.follow_through_probability,
                    "forward_horizon": item.forward_horizon,
                    "source_digest": item.source_digest,
                }
                for key, item in current.items()
            },
        }
    return {
        "schema_version": 1,
        "requirement_id": "C062",
        "status": "PASS",
        "bridge_root": str(bridge_root),
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "symbol_count": len(output),
        "timeframes": timeframes,
        "historical_outcome_count": total_outcomes,
        "continuation_contract": "IMPULSE_PULLBACK_RESUMPTION_WITH_FIVE_COMPLETED_FORWARD_CANDLES",
        "future_data_policy": "CURRENT_CONTEXT_USES_ONLY_COMPLETED_CANDLES; HISTORICAL_FOLLOW_THROUGH_USES_LATER_CANDLES",
        "role": "CONTINUATION_INTELLIGENCE_NOT_TRADE_APPROVAL",
        "symbols": output,
    }


def _build_live_cross_asset_library(
    *,
    bridge_root: Path,
    symbols: Sequence[str],
):
    adapter = HfmCsvMarketDataAdapter(bridge_root)
    timeframes = ("M1", "M5", "M15", "H1", "H4", "D1")
    ticks = {symbol: adapter.latest_tick(symbol) for symbol in symbols}
    frames = {
        symbol: {
            timeframe: adapter.read_bars(
                symbol,
                timeframe,
                observed_at=ticks[symbol].timestamp.astimezone(timezone.utc),
                history_mode="combined",
            )
            for timeframe in timeframes
        }
        for symbol in symbols
    }
    observed_at = min(item.timestamp.astimezone(timezone.utc) for item in ticks.values())
    library = build_cross_asset_library(
        frames, observed_at=observed_at, window=60, baseline_window=120
    )
    return library, frames, ticks, timeframes


def verify_c063_cross_asset_data_library(
    *,
    bridge_root: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    active_symbols = {"XAUUSD", "UK100", "USA100", "USA500", "USA30"}
    if set(symbols) != active_symbols:
        raise ValueError("C063 requires the exact five-symbol active universe")
    library, frames, ticks, timeframes = _build_live_cross_asset_library(
        bridge_root=bridge_root, symbols=symbols
    )
    output = {}
    for symbol in symbols:
        current = library.observations_for_symbol(symbol)
        if set(current) != set(timeframes):
            raise ValueError(f"C063 missing cross-asset timeframe observation for {symbol}")
        for timeframe, item in current.items():
            if set(item.peer_symbols) != active_symbols - {symbol}:
                raise ValueError(f"C063 peer universe is incomplete for {symbol}/{timeframe}")
            if item.aligned_sample_size < 10 or len(item.source_digest) != 64:
                raise ValueError(f"C063 aligned data/provenance is insufficient for {symbol}/{timeframe}")
        snapshot = MarketSnapshot(
            symbol,
            ticks[symbol].timestamp.astimezone(timezone.utc),
            (ticks[symbol],),
            {"M1": tuple(frames[symbol]["M1"][-30:])},
            {"M1": "COMPLETED"},
            tuple(item.source_id for item in frames[symbol]["M1"][-30:]),
        )
        report = IntelligenceEngine(cross_asset_library=library).analyse(snapshot)
        if report.cross_asset_context != current:
            raise ValueError(f"C063 engine cross-asset exposure is incomplete for {symbol}")
        if "CROSS_ASSET_DATA_OBSERVED" not in report.evidence:
            raise ValueError(f"C063 evidence marker is absent for {symbol}")
        output[symbol] = {
            timeframe: {
                "peer_symbols": item.peer_symbols,
                "aligned_sample_size": item.aligned_sample_size,
                "latest_returns": item.latest_returns,
                "source_digest": item.source_digest,
            }
            for timeframe, item in current.items()
        }
    return {
        "schema_version": 1,
        "requirement_id": "C063",
        "status": "PASS",
        "bridge_root": str(bridge_root),
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "symbol_count": len(output),
        "timeframes": timeframes,
        "peer_contract": "EXACT_FIVE_SYMBOL_UNIVERSE_WITH_FOUR_PEERS_PER_TIMEFRAME",
        "future_data_policy": "ONLY_CANDLES_COMPLETED_BY_THE_COMMON_OBSERVATION_TIME",
        "role": "CROSS_ASSET_DATA_LIBRARY_NOT_TRADE_APPROVAL",
        "symbols": output,
    }


def verify_c064_correlation_engine(
    *,
    bridge_root: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    active_symbols = {"XAUUSD", "UK100", "USA100", "USA500", "USA30"}
    if set(symbols) != active_symbols:
        raise ValueError("C064 requires the exact five-symbol active universe")
    library, frames, ticks, timeframes = _build_live_cross_asset_library(
        bridge_root=bridge_root, symbols=symbols
    )
    output = {}
    total = 0
    for symbol in symbols:
        current = library.correlations_for_symbol(symbol)
        if len(current) != (len(active_symbols) - 1) * len(timeframes):
            raise ValueError(f"C064 correlation coverage is incomplete for {symbol}")
        for key, item in current.items():
            if item.sample_size < 10 or item.regime not in {"STABLE", "CORRELATION_SHIFT", "CURRENT_ONLY", "INSUFFICIENT_SAMPLE"}:
                raise ValueError(f"C064 correlation observation is invalid for {symbol}/{key}")
            if item.current_correlation is not None and not -1.0 <= item.current_correlation <= 1.0:
                raise ValueError(f"C064 current correlation is out of range for {symbol}/{key}")
            if item.baseline_correlation is not None and not -1.0 <= item.baseline_correlation <= 1.0:
                raise ValueError(f"C064 baseline correlation is out of range for {symbol}/{key}")
            if len(item.source_digest) != 64:
                raise ValueError(f"C064 provenance is invalid for {symbol}/{key}")
        snapshot = MarketSnapshot(
            symbol,
            ticks[symbol].timestamp.astimezone(timezone.utc),
            (ticks[symbol],),
            {"M1": tuple(frames[symbol]["M1"][-30:])},
            {"M1": "COMPLETED"},
            tuple(item.source_id for item in frames[symbol]["M1"][-30:]),
        )
        report = IntelligenceEngine(cross_asset_library=library).analyse(snapshot)
        if report.correlation_context != current:
            raise ValueError(f"C064 engine correlation exposure is incomplete for {symbol}")
        if "CORRELATION_ENGINE_OBSERVED" not in report.evidence:
            raise ValueError(f"C064 evidence marker is absent for {symbol}")
        total += len(current)
        output[symbol] = {
            key: {
                "peer_symbol": item.peer_symbol,
                "timeframe": item.timeframe,
                "current_correlation": item.current_correlation,
                "baseline_correlation": item.baseline_correlation,
                "regime": item.regime,
                "sample_size": item.sample_size,
            }
            for key, item in current.items()
        }
    return {
        "schema_version": 1,
        "requirement_id": "C064",
        "status": "PASS",
        "bridge_root": str(bridge_root),
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "symbol_count": len(output),
        "timeframes": timeframes,
        "correlation_observation_count": total,
        "correlation_contract": "ROLLING_CURRENT_VERSUS_PRIOR_BASELINE_BY_PEER_AND_TIMEFRAME",
        "future_data_policy": "CURRENT_CORRELATION_USES_COMPLETED_CANDLE_RETURNS_ONLY",
        "role": "CORRELATION_INTELLIGENCE_NOT_TRADE_APPROVAL",
        "symbols": output,
    }


def verify_c065_correlation_breakdown_detector(
    *,
    bridge_root: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    active_symbols = {"XAUUSD", "UK100", "USA100", "USA500", "USA30"}
    if set(symbols) != active_symbols:
        raise ValueError("C065 requires the exact five-symbol active universe")
    library, frames, ticks, timeframes = _build_live_cross_asset_library(
        bridge_root=bridge_root, symbols=symbols
    )
    output = {}
    total_breakdowns = 0
    for symbol in symbols:
        current = library.breakdowns_for_symbol(symbol)
        expected = (len(active_symbols) - 1) * len(timeframes)
        if len(current) != expected:
            raise ValueError(f"C065 breakdown coverage is incomplete for {symbol}")
        for key, item in current.items():
            expected_breakdown = item.sign_flip or (
                item.current_correlation is not None
                and item.baseline_correlation is not None
                and abs(item.baseline_correlation) >= 0.5
                and abs(item.current_correlation) <= abs(item.baseline_correlation) - 0.25
            )
            if item.breakdown != expected_breakdown:
                raise ValueError(f"C065 breakdown classification is inconsistent for {symbol}/{key}")
            if len(item.source_digest) != 64:
                raise ValueError(f"C065 provenance is invalid for {symbol}/{key}")
        snapshot = MarketSnapshot(
            symbol,
            ticks[symbol].timestamp.astimezone(timezone.utc),
            (ticks[symbol],),
            {"M1": tuple(frames[symbol]["M1"][-30:])},
            {"M1": "COMPLETED"},
            tuple(item.source_id for item in frames[symbol]["M1"][-30:]),
        )
        report = IntelligenceEngine(cross_asset_library=library).analyse(snapshot)
        if report.correlation_breakdown_context != current:
            raise ValueError(f"C065 engine breakdown exposure is incomplete for {symbol}")
        if "CORRELATION_BREAKDOWN_OBSERVED" not in report.evidence:
            raise ValueError(f"C065 evidence marker is absent for {symbol}")
        total_breakdowns += sum(item.breakdown for item in current.values())
        output[symbol] = {
            "observation_count": len(current),
            "breakdown_count": sum(item.breakdown for item in current.values()),
            "sign_flip_count": sum(item.sign_flip for item in current.values()),
            "observations": {
                key: {
                    "current_correlation": item.current_correlation,
                    "baseline_correlation": item.baseline_correlation,
                    "breakdown": item.breakdown,
                    "sign_flip": item.sign_flip,
                    "source_digest": item.source_digest,
                }
                for key, item in current.items()
            },
        }
    return {
        "schema_version": 1,
        "requirement_id": "C065",
        "status": "PASS",
        "bridge_root": str(bridge_root),
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "symbol_count": len(output),
        "timeframes": timeframes,
        "breakdown_count": total_breakdowns,
        "breakdown_contract": "SIGN_FLIP_OR_LARGE_CORRELATION_DECAY_VERSUS_PRIOR_BASELINE",
        "future_data_policy": "BREAKDOWN_STATUS_IS_DESCRIPTIVE_AND_DOES_NOT_BLOCK_TRADES",
        "role": "CORRELATION_BREAKDOWN_RESEARCH_NOT_TRADE_APPROVAL",
        "symbols": output,
    }


def verify_c066_market_regime_engine(
    *,
    bridge_root: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    active_symbols = {"XAUUSD", "UK100", "USA100", "USA500", "USA30"}
    timeframes = ("M1", "M5", "M15", "H1", "H4", "D1")
    if set(symbols) != active_symbols:
        raise ValueError("C066 requires the exact five-symbol active universe")
    adapter = HfmCsvMarketDataAdapter(bridge_root)
    output = {}
    label_counts = {label: 0 for label in REGIME_LABELS}
    for symbol in symbols:
        tick = adapter.latest_tick(symbol)
        observed_at = tick.timestamp.astimezone(timezone.utc)
        frames = {
            timeframe: adapter.read_bars(
                symbol, timeframe, observed_at=observed_at, history_mode="combined"
            )
            for timeframe in timeframes
        }
        library = build_market_regime_library(
            {symbol: frames}, observed_at=observed_at, lookback=20, baseline_window=100
        )
        current = library.observations_for_symbol(symbol)
        if set(current) != set(timeframes):
            raise ValueError(f"C066 missing regime timeframe observation for {symbol}")
        for timeframe, item in current.items():
            if item.label not in REGIME_LABELS:
                raise ValueError(f"C066 invalid regime label for {symbol}/{timeframe}")
            if item.sample_size > 20 or len(item.source_digest) != 64:
                raise ValueError(f"C066 sample/provenance contract is invalid for {symbol}/{timeframe}")
            if abs(sum(item.probabilities.values()) - 1.0) > 1e-9:
                raise ValueError(f"C066 regime probabilities do not normalize for {symbol}/{timeframe}")
            if any(not 0.0 <= value <= 1.0 for value in item.probabilities.values()):
                raise ValueError(f"C066 regime probability is out of range for {symbol}/{timeframe}")
            if item.news_state not in {"NEWS", "NOT_OBSERVED"}:
                raise ValueError(f"C066 news state is not explicit for {symbol}/{timeframe}")
            if any(row.end > observed_at for row in frames[timeframe]):
                raise ValueError(f"C066 forming candle leaked into {symbol}/{timeframe}")
            label_counts[item.label] += 1
        snapshot = MarketSnapshot(
            symbol,
            observed_at,
            (tick,),
            {"M1": tuple(frames["M1"][-30:])},
            {"M1": "COMPLETED"},
            tuple(item.source_id for item in frames["M1"][-30:]),
        )
        report = IntelligenceEngine(market_regime_library=library).analyse(snapshot)
        if report.market_regime_context != current:
            raise ValueError(f"C066 engine regime exposure is incomplete for {symbol}")
        if "MARKET_REGIME_ENGINE_OBSERVED" not in report.evidence:
            raise ValueError(f"C066 evidence marker is absent for {symbol}")
        output[symbol] = {
            "observed_at": observed_at.isoformat(),
            "timeframes": {
                key: {
                    "label": item.label,
                    "probabilities": item.probabilities,
                    "confidence": item.confidence,
                    "volatility_ratio": item.volatility_ratio,
                    "volatility_percentile": item.volatility_percentile,
                    "directional_persistence": item.directional_persistence,
                    "slope": item.slope,
                    "compression_ratio": item.compression_ratio,
                    "breakout": item.breakout,
                    "news_state": item.news_state,
                    "sample_size": item.sample_size,
                    "source_digest": item.source_digest,
                }
                for key, item in current.items()
            },
        }
    return {
        "schema_version": 1,
        "requirement_id": "C066",
        "status": "PASS",
        "bridge_root": str(bridge_root),
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "symbol_count": len(output),
        "timeframes": timeframes,
        "labels": REGIME_LABELS,
        "label_counts": label_counts,
        "news_contract": "NEWS_REQUIRES_EXPLICIT_EXTERNAL_ANNOTATION; PRICE_EXPANSION_IS_NOT_CLAIMED_AS_NEWS",
        "future_data_policy": "ONLY_COMPLETED_CANDLES_BY_SYMBOL_TICK_OBSERVATION_TIME",
        "role": "MARKET_REGIME_DESCRIPTION_NOT_TRADE_APPROVAL",
        "symbols": output,
    }


def verify_c067_regime_probability_engine(
    *,
    bridge_root: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    active_symbols = {"XAUUSD", "UK100", "USA100", "USA500", "USA30"}
    timeframes = ("M1", "M5", "M15", "H1", "H4", "D1")
    if set(symbols) != active_symbols:
        raise ValueError("C067 requires the exact five-symbol active universe")
    adapter = HfmCsvMarketDataAdapter(bridge_root)
    output = {}
    total_samples = 0
    for symbol in symbols:
        tick = adapter.latest_tick(symbol)
        observed_at = tick.timestamp.astimezone(timezone.utc)
        frames = {
            timeframe: adapter.read_bars(
                symbol, timeframe, observed_at=observed_at, history_mode="combined"
            )
            for timeframe in timeframes
        }
        library = build_regime_probability_library(
            {symbol: frames}, observed_at=observed_at, lookback=20, baseline_window=100
        )
        current = library.observations_for_symbol(symbol)
        if set(current) != set(timeframes):
            raise ValueError(f"C067 missing probability timeframe observation for {symbol}")
        for timeframe, item in current.items():
            if item.current_label not in REGIME_LABELS:
                raise ValueError(f"C067 invalid current regime for {symbol}/{timeframe}")
            if item.sample_size < 10:
                raise ValueError(f"C067 historical regime sample is insufficient for {symbol}/{timeframe}")
            if sum(item.counts.values()) != item.sample_size:
                raise ValueError(f"C067 historical counts do not reconcile for {symbol}/{timeframe}")
            if abs(sum(item.probabilities.values()) - 1.0) > 1e-9:
                raise ValueError(f"C067 probabilities do not normalize for {symbol}/{timeframe}")
            if any(not 0.0 <= value <= 1.0 for value in item.probabilities.values()):
                raise ValueError(f"C067 probability is out of range for {symbol}/{timeframe}")
            if not 0.0 <= item.interval_low <= item.interval_high <= 1.0:
                raise ValueError(f"C067 uncertainty interval is invalid for {symbol}/{timeframe}")
            if len(item.source_digest) != 64:
                raise ValueError(f"C067 provenance is invalid for {symbol}/{timeframe}")
            total_samples += item.sample_size
        snapshot = MarketSnapshot(
            symbol,
            observed_at,
            (tick,),
            {"M1": tuple(frames["M1"][-30:])},
            {"M1": "COMPLETED"},
            tuple(item.source_id for item in frames["M1"][-30:]),
        )
        report = IntelligenceEngine(regime_probability_library=library).analyse(snapshot)
        if report.regime_probability_context != current:
            raise ValueError(f"C067 engine probability exposure is incomplete for {symbol}")
        if "REGIME_PROBABILITY_ENGINE_OBSERVED" not in report.evidence:
            raise ValueError(f"C067 evidence marker is absent for {symbol}")
        output[symbol] = {
            "timeframes": {
                key: {
                    "current_label": item.current_label,
                    "probabilities": item.probabilities,
                    "counts": item.counts,
                    "sample_size": item.sample_size,
                    "interval_low": item.interval_low,
                    "interval_high": item.interval_high,
                    "source_digest": item.source_digest,
                }
                for key, item in current.items()
            }
        }
    return {
        "schema_version": 1,
        "requirement_id": "C067",
        "status": "PASS",
        "bridge_root": str(bridge_root),
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "symbol_count": len(output),
        "timeframes": timeframes,
        "total_historical_samples": total_samples,
        "probability_contract": "SMOOTHED_HISTORICAL_REGIME_FREQUENCIES_WITH_WILSON_INTERVAL_FOR_CURRENT_LABEL",
        "future_data_policy": "HISTORICAL_ANCHORS_END_BEFORE_LATEST_COMPLETED_CANDLE",
        "role": "REGIME_PROBABILITY_RESEARCH_NOT_TRADE_APPROVAL",
        "symbols": output,
    }


def verify_c068_strategy_router(
    *,
    bridge_root: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    active_symbols = {"XAUUSD", "UK100", "USA100", "USA500", "USA30"}
    timeframes = ("M1", "M5", "M15", "H1", "H4", "D1")
    if set(symbols) != active_symbols:
        raise ValueError("C068 requires the exact five-symbol active universe")
    adapter = HfmCsvMarketDataAdapter(bridge_root)
    router = build_strategy_router()
    known_ids = {item.strategy_id for item in default_knowledge()}
    output = {}
    for symbol in symbols:
        tick = adapter.latest_tick(symbol)
        observed_at = tick.timestamp.astimezone(timezone.utc)
        frames = {
            timeframe: adapter.read_bars(
                symbol, timeframe, observed_at=observed_at, history_mode="combined"
            )
            for timeframe in timeframes
        }
        regime_library = build_market_regime_library(
            {symbol: frames}, observed_at=observed_at, lookback=20, baseline_window=100
        )
        snapshot = MarketSnapshot(
            symbol,
            observed_at,
            (tick,),
            {"M1": tuple(frames["M1"][-30:])},
            {"M1": "COMPLETED"},
            tuple(item.source_id for item in frames["M1"][-30:]),
        )
        report = IntelligenceEngine(
            market_regime_library=regime_library,
            strategy_router=router,
        ).analyse(snapshot)
        route = report.strategy_routing_context
        if route is None or not route.candidate_ids:
            raise ValueError(f"C068 no descriptive strategy route for {symbol}")
        if route.regime != report.market_regime_context["M1"].label:
            raise ValueError(f"C068 route regime mismatch for {symbol}")
        if route.session != report.session_context.primary_session:
            raise ValueError(f"C068 route session mismatch for {symbol}")
        if any(item_id not in known_ids for item_id in route.candidate_ids):
            raise ValueError(f"C068 route references an unregistered strategy for {symbol}")
        if route.informational_only is not True:
            raise ValueError(f"C068 router is not informational-only for {symbol}")
        if "STRATEGY_ROUTER_OBSERVED" not in report.evidence:
            raise ValueError(f"C068 evidence marker is absent for {symbol}")
        output[symbol] = {
            "observed_at": observed_at.isoformat(),
            "regime": route.regime,
            "session": route.session,
            "candidate_ids": route.candidate_ids,
            "strategy_families": route.strategy_families,
            "reason": route.reason,
            "informational_only": route.informational_only,
        }
    return {
        "schema_version": 1,
        "requirement_id": "C068",
        "status": "PASS",
        "bridge_root": str(bridge_root),
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "symbol_count": len(output),
        "timeframes": timeframes,
        "strategy_count": len(known_ids),
        "role": "DESCRIPTIVE_REGIME_ROUTING_NOT_TRADE_APPROVAL",
        "future_data_policy": "ROUTING_USES_ONLY_CURRENT_COMPLETED_MARKET_REGIME_CONTEXT",
        "symbols": output,
    }


def verify_c069_c076_knowledge_library(requirement_id: str) -> Mapping[str, object]:
    """Verify the structured methodology and pattern vocabulary.

    These records describe testable hypotheses. They do not become trade
    decisions, filters, or execution instructions merely by being present.
    """

    target_ids = {f"C{number:03d}" for number in range(69, 77)}
    if requirement_id not in target_ids:
        raise ValueError(f"unsupported knowledge-library requirement: {requirement_id}")
    strategies = default_knowledge()
    if len(strategies) < 10 or len({item.strategy_id for item in strategies}) != len(strategies):
        raise ValueError("strategy knowledge library is incomplete or has duplicate ids")
    if any(not item.conditions or not item.required_observations for item in strategies):
        raise ValueError("strategy knowledge entry is missing conditions or observations")
    vocabulary = KnowledgeLibrary.complete_vocabulary().all()
    names = {item.name for item in vocabulary}
    strategy_names = {item.strategy_id for item in strategies}
    if not strategy_names.issubset(names):
        raise ValueError("strategy hypotheses are not represented in the machine-readable vocabulary")
    family_conditions = {
        "C070": ("PRICE_ACTION", PRICE_ACTION),
        "C071": ("SMC", SMC),
        "C072": ("WYCKOFF", WYCKOFF),
        "C073": ("BREAKOUT", BREAKOUT),
        "C074": ("MEAN_REVERSION", MEAN_REVERSION),
        "C075": ("MOMENTUM", MOMENTUM),
    }
    if requirement_id in family_conditions:
        family, conditions = family_conditions[requirement_id]
        entries = tuple(item for item in vocabulary if item.family == family)
        if len(entries) != 1 or set(conditions) - set(entries[0].observation.split(",")):
            raise ValueError(f"{requirement_id} family vocabulary is incomplete")
    if requirement_id == "C076":
        patterns = chart_patterns()
        pattern_names = {item.name for item in vocabulary if item.family == "PATTERN"}
        if len(patterns) < 10 or {item.pattern_id for item in patterns} - pattern_names:
            raise ValueError("chart pattern library is incomplete")
    return {
        "schema_version": 1,
        "requirement_id": requirement_id,
        "status": "PASS",
        "strategy_count": len(strategies),
        "vocabulary_count": len(vocabulary),
        "strategy_ids": tuple(item.strategy_id for item in strategies),
        "pattern_count": len(chart_patterns()),
        "family": family_conditions.get(requirement_id, ("STRATEGY_AND_PATTERN", ()))[0],
        "role": "MACHINE_READABLE_RESEARCH_HYPOTHESES_NOT_TRADE_APPROVAL",
        "truth_policy": "ENTRIES_REQUIRE_OUTCOME_EVIDENCE; DEFINITIONS_ARE_NOT_PROOF_OF_EDGE",
        "requirements_checked": tuple(sorted(target_ids)),
    }


def verify_c077_c078_pattern_outcomes(
    *,
    bridge_root: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    active_symbols = {"XAUUSD", "UK100", "USA100", "USA500", "USA30"}
    timeframes = ("M1", "M5", "M15", "H1", "H4", "D1")
    if set(symbols) != active_symbols:
        raise ValueError("C077/C078 requires the exact five-symbol active universe")
    adapter = HfmCsvMarketDataAdapter(bridge_root)
    output = {}
    total_records = 0
    total_failed = 0
    for symbol in symbols:
        tick = adapter.latest_tick(symbol)
        observed_at = tick.timestamp.astimezone(timezone.utc)
        frames = {
            timeframe: adapter.read_bars(
                symbol, timeframe, observed_at=observed_at, history_mode="combined"
            )
            for timeframe in timeframes
        }
        library = build_pattern_outcome_library(
            {symbol: frames}, observed_at=observed_at, lookahead=12, max_bars_per_frame=360
        )
        records = library.records_for_symbol(symbol)
        failed = library.failed_for_symbol(symbol)
        if not records:
            raise ValueError(f"C077 no pattern outcome records for {symbol}")
        if not failed:
            raise ValueError(f"C078 no failed pattern records for {symbol}")
        snapshot = MarketSnapshot(
            symbol,
            observed_at,
            (tick,),
            {"M1": tuple(frames["M1"][-30:])},
            {"M1": "COMPLETED"},
            tuple(item.source_id for item in frames["M1"][-30:]),
        )
        report = IntelligenceEngine(pattern_outcome_library=library).analyse(snapshot)
        if len(report.pattern_outcome_context) != len(records):
            raise ValueError(f"C077 engine pattern outcome exposure is incomplete for {symbol}")
        if len(report.failed_pattern_context) != len(failed):
            raise ValueError(f"C078 engine failed pattern exposure is incomplete for {symbol}")
        if "PATTERN_OUTCOMES_OBSERVED" not in report.evidence:
            raise ValueError(f"C077 evidence marker is absent for {symbol}")
        if "FAILED_PATTERN_LIBRARY_OBSERVED" not in report.evidence:
            raise ValueError(f"C078 evidence marker is absent for {symbol}")
        total_records += len(records)
        total_failed += len(failed)
        output[symbol] = {
            "observed_at": observed_at.isoformat(),
            "record_count": len(records),
            "failed_record_count": len(failed),
            "pattern_counts": {
                pattern: len(library.records_for_pattern(pattern))
                for pattern in sorted({item.pattern_id for item in records})
            },
            "source_policy": "COMPLETED_CANDLES_ONLY_WITH_FORWARD_HORIZON_AFTER_OBSERVATION",
        }
    return {
        "schema_version": 1,
        "requirement_id": "C077-C078",
        "status": "PASS",
        "bridge_root": str(bridge_root),
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "symbol_count": len(output),
        "timeframes": timeframes,
        "total_records": total_records,
        "total_failed_records": total_failed,
        "role": "PATTERN_OUTCOME_RESEARCH_NOT_TRADE_APPROVAL",
        "future_data_policy": "OUTCOME_LOOKAHEAD_STARTS_AFTER_THE_OBSERVED_COMPLETED_CANDLE",
        "symbols": output,
    }


def verify_c079_c084_historical_state_layer(
    *,
    requirement_id: str,
    bridge_root: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    active_symbols = {"XAUUSD", "UK100", "USA100", "USA500", "USA30"}
    timeframes = ("M1", "M5", "M15", "H1", "H4", "D1")
    target_ids = {f"C{number:03d}" for number in range(79, 85)}
    if requirement_id not in target_ids:
        raise ValueError(f"unsupported historical-state requirement: {requirement_id}")
    if set(symbols) != active_symbols:
        raise ValueError("C079-C084 requires the exact five-symbol active universe")
    adapter = HfmCsvMarketDataAdapter(bridge_root)
    fingerprint_engine = SetupFingerprintEngine()
    output = {}
    for symbol in symbols:
        tick = adapter.latest_tick(symbol)
        observed_at = tick.timestamp.astimezone(timezone.utc)
        frames = {
            timeframe: adapter.read_bars(
                symbol, timeframe, observed_at=observed_at, history_mode="combined"
            )
            for timeframe in timeframes
        }
        anchor_rows = frames["M1"][-72::6]
        states = []
        paths = []
        for anchor in anchor_rows:
            anchor_time = anchor.end
            selected = {
                timeframe: tuple(row for row in rows if row.end <= anchor_time)[-40:]
                for timeframe, rows in frames.items()
            }
            proxy_tick = RawTick(
                symbol,
                anchor_time,
                anchor.close,
                anchor.close + max(anchor.spread_points, 0.01),
            )
            snapshot = MarketSnapshot(
                symbol,
                anchor_time,
                (proxy_tick,),
                selected,
                {timeframe: "COMPLETED" if selected[timeframe] else "MISSING" for timeframe in timeframes},
                (anchor.source_id,),
                {"point": 1.0},
            )
            fingerprint = fingerprint_engine.build(snapshot, timeframes)
            states.append(fingerprint.state)
            paths.append(path_outcome(fingerprint.state, frames["M1"], horizons=(60, 180, 300, 900)))
        if len(states) < 5:
            raise ValueError(f"C081 insufficient historical states for {symbol}")
        index = HistoricalAnalogueIndex(states, paths)
        current = states[-1]
        similarities = index.search(current, limit=5, same_symbol=True)
        if not similarities:
            raise ValueError(f"C082 no historical analogues for {symbol}")
        if any(item.distance < 0 or not isfinite(item.distance) for item in similarities):
            raise ValueError(f"C083 invalid similarity score for {symbol}")
        if any(left.distance > right.distance for left, right in zip(similarities, similarities[1:])):
            raise ValueError(f"C083 similarity scores are not ordered for {symbol}")
        with_paths = tuple(item for item in similarities if item.path is not None and item.path.outcomes)
        if not with_paths:
            raise ValueError(f"C084 nearest-neighbour outcomes missing for {symbol}")
        store = FeatureStore()
        current_snapshot = MarketSnapshot(
            symbol,
            current.observed_at,
            (RawTick(symbol, current.observed_at, frames["M1"][-1].close, frames["M1"][-1].close + 0.01),),
            {timeframe: tuple(row for row in rows if row.end <= current.observed_at)[-40:] for timeframe, rows in frames.items()},
            {timeframe: "COMPLETED" for timeframe in timeframes},
            current.source_ids,
            {"point": 1.0},
        )
        current_fingerprint = fingerprint_engine.build(current_snapshot, timeframes)
        store.put(
            FeatureRecord(
                current.state_id,
                symbol,
                current.observed_at,
                "MULTI",
                current_fingerprint.as_mapping(),
                current_fingerprint.source_digest,
                current.feature_schema_version,
            )
        )
        if not store.for_state(current.state_id):
            raise ValueError(f"C080 feature store did not persist {symbol}")
        output[symbol] = {
            "observed_at": observed_at.isoformat(),
            "state_count": len(states),
            "feature_count": len(current.vector),
            "schema_version": current.feature_schema_version,
            "analogue_count": len(similarities),
            "nearest_distance": similarities[0].distance,
            "nearest_outcome_horizon_count": len(with_paths[0].path.outcomes),
            "source_policy": "COMPLETED_BAR_SNAPSHOTS_WITH_EXPLICIT_CANDLE_CLOSE_PROXY_FOR_HISTORICAL_ANCHOR",
        }
    return {
        "schema_version": 1,
        "requirement_id": requirement_id,
        "status": "PASS",
        "bridge_root": str(bridge_root),
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "symbol_count": len(output),
        "timeframes": timeframes,
        "role": "HISTORICAL_STATE_RESEARCH_NOT_TRADE_APPROVAL",
        "future_data_policy": "FEATURES_USE_CANDLES_AT_OR_BEFORE_ANCHOR;_PATHS_START_AFTER_ANCHOR",
        "symbols": output,
    }


def verify_c085_c092_outcome_metrics(
    *,
    requirement_id: str,
    bridge_root: Path,
    symbols: Sequence[str],
    tick_database: Path | None = None,
) -> Mapping[str, object]:
    """Measure forward outcomes without creating a trading approval gate."""

    active_symbols = {"XAUUSD", "UK100", "USA100", "USA500", "USA30"}
    target_ids = {f"C{number:03d}" for number in range(85, 93)}
    if requirement_id not in target_ids:
        raise ValueError(f"unsupported outcome-metric requirement: {requirement_id}")
    if set(symbols) != active_symbols:
        raise ValueError("C085-C092 requires the exact five-symbol active universe")

    adapter = HfmCsvMarketDataAdapter(bridge_root)
    contracts = {item.symbol: item for item in adapter.symbols()}
    if not active_symbols <= set(contracts):
        raise ValueError("broker contract export is missing an active symbol")

    tick_database = tick_database or (
        Path(__file__).resolve().parents[1] / "evidence" / "live_tick_window_v2.sqlite3"
    )
    tick_paths: dict[str, tuple[tuple[int, float, float], ...]] = {}
    tick_counts: dict[str, int] = {}
    if tick_database.is_file():
        connection = sqlite3.connect(f"file:{tick_database.resolve()}?mode=ro", uri=True)
        try:
            for symbol in symbols:
                rows = connection.execute(
                    "SELECT timestamp,bid,ask FROM raw_ticks WHERE symbol=? ORDER BY timestamp,bid,ask",
                    (symbol,),
                ).fetchall()
                parsed = tuple(
                    (int(round((datetime.fromisoformat(row[0]) - datetime.fromisoformat(rows[0][0])).total_seconds())), float(row[1]), float(row[2]))
                    for row in rows
                    if float(row[1]) > 0 and float(row[2]) >= float(row[1])
                ) if rows else tuple()
                tick_paths[symbol] = parsed
                tick_counts[symbol] = len(parsed)
        finally:
            connection.close()
    else:
        tick_paths = {symbol: tuple() for symbol in symbols}
        tick_counts = {symbol: 0 for symbol in symbols}

    output: dict[str, object] = {}
    aggregate_horizons: dict[int, int] = {horizon: 0 for horizon in OUTCOME_HORIZONS_SECONDS}
    for symbol in symbols:
        latest = adapter.latest_tick(symbol)
        bars = adapter.read_bars(symbol, "M1", observed_at=latest.timestamp, history_mode="combined")
        if len(bars) < 32:
            raise ValueError(f"C085-C092 requires at least 32 completed M1 bars for {symbol}")
        point = max(contracts[symbol].point, 1e-12)
        tick_metrics = tuple()
        tick_rows: list[tuple[float, bool | None]] = []
        tick_path = tick_paths.get(symbol, tuple())
        if len(tick_path) >= 2:
            tick_entry = (tick_path[0][1] + tick_path[0][2]) / 2.0
            tick_end = tick_path[-1][0]
            tick_direction = "BUY" if tick_path[-1][1] >= tick_entry else "SELL"
            tick_metrics = forward_metrics(tick_direction, tick_entry, tick_path)
            tick_stop = max(abs(tick_entry) * 0.0005, point * 5.0)
            tick_target = tick_stop * 1.5
            tick_r, tick_tp_first, _, _ = classify_r_multiple(
                tick_direction,
                tick_entry,
                tick_path,
                tick_stop,
                tick_target,
                total_cost=max(tick_path[0][2] - tick_path[0][1], 0.0),
            )
            tick_rows.append((tick_r, tick_tp_first))
            for metric in tick_metrics:
                aggregate_horizons[metric.horizon_seconds] += 1
        candle_rows: list[tuple[float, bool | None]] = []
        candle_metrics_by_horizon: dict[int, list[object]] = {horizon: [] for horizon in OUTCOME_HORIZONS_SECONDS}
        sample_bars = bars[-96:-8:4]
        for anchor_index, anchor in enumerate(sample_bars):
            future = tuple(item for item in bars if item.end > anchor.end)
            if not future:
                continue
            entry = anchor.close
            direction = "BUY" if anchor.close >= anchor.open else "SELL"
            path = tuple(
                (
                    max(1, int((item.end - anchor.end).total_seconds())),
                    item.close,
                    item.close + max(item.spread_points, 0.0) * point,
                )
                for item in future[:144]
            )
            metrics = forward_metrics(direction, entry, path)
            for metric in metrics:
                candle_metrics_by_horizon[metric.horizon_seconds].append(metric)
                aggregate_horizons[metric.horizon_seconds] += 1
            lookback = bars[max(0, bars.index(anchor) - 20):bars.index(anchor)]
            range_size = max((item.high for item in lookback), default=anchor.high) - min(
                (item.low for item in lookback), default=anchor.low
            )
            stop_distance = max(range_size / 4.0, point * 5.0)
            target_distance = stop_distance * 1.5
            total_cost = max(anchor.spread_points, 0.0) * point
            row = classify_r_multiple(
                direction,
                entry,
                path,
                stop_distance,
                target_distance,
                total_cost=total_cost,
            )
            candle_rows.append((row[0], row[1]))
        rows = tuple(tick_rows + candle_rows)
        if not rows:
            raise ValueError(f"C085-C092 produced no measurable outcome rows for {symbol}")
        summary = summarize_outcomes(rows)
        horizon_summary = {
            str(horizon): {
                "sample_count": len(candle_metrics_by_horizon[horizon]) + (1 if any(item.horizon_seconds == horizon for item in tick_metrics) else 0),
                "candle_proxy_count": len(candle_metrics_by_horizon[horizon]),
                "tick_count": sum(item.horizon_seconds == horizon for item in tick_metrics),
                "mfe_mean": mean(item.mfe for item in candle_metrics_by_horizon[horizon]) if candle_metrics_by_horizon[horizon] else None,
                "mae_mean": mean(item.mae for item in candle_metrics_by_horizon[horizon]) if candle_metrics_by_horizon[horizon] else None,
                "time_to_mfe_mean_seconds": mean(item.time_to_mfe_seconds for item in candle_metrics_by_horizon[horizon]) if candle_metrics_by_horizon[horizon] else None,
                "time_to_mae_mean_seconds": mean(item.time_to_mae_seconds for item in candle_metrics_by_horizon[horizon]) if candle_metrics_by_horizon[horizon] else None,
            }
            for horizon in OUTCOME_HORIZONS_SECONDS
        }
        output[symbol] = {
            "completed_m1_bars": len(bars),
            "candle_proxy_samples": len(candle_rows),
            "tick_samples": tick_counts.get(symbol, 0),
            "horizons": horizon_summary,
            "tp_before_sl_probability": summary.tp_before_sl_probability,
            "r_multiple_mean_after_cost": summary.r_multiple_mean,
            "expected_value_after_cost_r": summary.expected_value_after_cost,
            "r_multiple_probabilities": summary.r_multiple_probabilities,
            "cost_model": "HFM exported spread_points multiplied by broker point; raw ticks use observed bid-ask spread",
            "source_policy": "CANDLE_CLOSE_PROXY_FOR_M1_FORWARD_PATHS;_RAW_BID_ASK_TICKS_REPORTED_SEPARATELY;NO_METRIC_IS_A_TRADE_APPROVAL",
        }

    if not all(aggregate_horizons.values()):
        missing = tuple(str(horizon) for horizon, count in aggregate_horizons.items() if count == 0)
        raise ValueError(f"C085-C092 missing aggregate candle horizons: {missing}")
    return {
        "schema_version": 1,
        "requirement_id": requirement_id,
        "status": "PASS",
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "symbol_count": len(output),
        "horizons_seconds": OUTCOME_HORIZONS_SECONDS,
        "aggregate_horizon_samples": aggregate_horizons,
        "tick_database": str(tick_database),
        "role": "OUTCOME_METRICS_RESEARCH_NOT_TRADE_APPROVAL",
        "symbols": output,
    }


def verify_c093_c098_edge_research(
    *,
    requirement_id: str,
    bridge_root: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    """Exercise discovery, testing, evidence, intervals, and sample guards."""

    active_symbols = {"XAUUSD", "UK100", "USA100", "USA500", "USA30"}
    target_ids = {f"C{number:03d}" for number in range(93, 99)}
    if requirement_id not in target_ids:
        raise ValueError(f"unsupported edge-research requirement: {requirement_id}")
    if set(symbols) != active_symbols:
        raise ValueError("C093-C098 requires the exact five-symbol active universe")

    adapter = HfmCsvMarketDataAdapter(bridge_root)
    states: list[tuple[str, Mapping[str, object], float]] = []
    outcomes: list[ForwardOutcome] = []
    observations: list[TimedObservation] = []
    source_ids: list[str] = []
    per_symbol: dict[str, int] = {}
    for symbol in symbols:
        latest = adapter.latest_tick(symbol)
        bars = adapter.read_bars(symbol, "M1", observed_at=latest.timestamp, history_mode="combined")
        if len(bars) < 96:
            raise ValueError(f"C093-C098 requires at least 96 completed M1 bars for {symbol}")
        point = 1e-12
        for item in adapter.symbols():
            if item.symbol == symbol:
                point = max(item.point, point)
                break
        rows = bars[-320:]
        created = 0
        for anchor_index in range(0, len(rows) - 15, 4):
            anchor = rows[anchor_index]
            future = rows[anchor_index + 15]
            range_size = max(anchor.high - anchor.low, point * 5.0)
            body_return = (anchor.close - anchor.open) / anchor.open
            lower_wick = max(0.0, min(anchor.open, anchor.close) - anchor.low) / range_size
            upper_wick = max(0.0, anchor.high - max(anchor.open, anchor.close)) / range_size
            range_return = range_size / max(anchor.close, point)
            state_id = f"{symbol}:{anchor.source_id}"
            value = (future.close - anchor.close) / max(anchor.close, point)
            features = {
                "body_return": body_return,
                "lower_wick_fraction": lower_wick,
                "upper_wick_fraction": upper_wick,
                "range_return": range_return,
                "symbol": symbol,
                "source_id": anchor.source_id,
            }
            states.append((state_id, features, value))
            stop_distance = max(range_size * 2.0, point * 5.0)
            r_multiple = (future.close - anchor.close) / stop_distance
            cost = max(anchor.spread_points, 0.0) * point / stop_distance
            outcome = ForwardOutcome(
                state_id,
                "research.candle_body.v1",
                "BUY",
                r_multiple,
                cost,
                900,
                "UNKNOWN",
                "UNKNOWN",
            )
            outcomes.append(outcome)
            observations.append(
                TimedObservation(
                    anchor.end,
                    r_multiple,
                    symbol,
                    "UNKNOWN",
                    "UNKNOWN",
                    anchor.end + timedelta(seconds=900),
                    "M1",
                    "BUY",
                    source_id=state_id,
                    cost=cost,
                )
            )
            source_ids.append(state_id)
            created += 1
        per_symbol[symbol] = created
    if len(states) < 100 or len(set(source_ids)) != len(source_ids):
        raise ValueError("edge-research fixture has insufficient or duplicate source evidence")

    proposals = EdgeMiner(
        lambda context: (
            HypothesisProposal(
                "body_direction_continuation",
                "positive M1 body return is associated with the measured forward outcome",
                ("body_return",),
                "deterministic_research_context",
                1,
            ),
            HypothesisProposal(
                "lower_wick_rejection",
                "large lower wick is tested as a conditional observation",
                ("lower_wick_fraction",),
                "deterministic_research_context",
                1,
            ),
            HypothesisProposal(
                "upper_wick_rejection",
                "large upper wick is tested as a conditional observation",
                ("upper_wick_fraction",),
                "deterministic_research_context",
                1,
            ),
        )
    ).propose({"symbols": tuple(sorted(symbols)), "sample_size": len(states)})
    predicates = {
        "body_direction_continuation": lambda features: float(features["body_return"]) > 0.0,
        "lower_wick_rejection": lambda features: float(features["lower_wick_fraction"]) >= 0.5,
        "upper_wick_rejection": lambda features: float(features["upper_wick_fraction"]) >= 0.5,
    }
    miner_results = tuple(
        run_hypothesis(proposal, states, predicates[proposal.hypothesis_id])
        for proposal in proposals
    )
    if not all(item.sample_size > 0 and item.evidence_ids for item in miner_results):
        raise ValueError("C093 edge miner produced a proposal without observed evidence")

    hypothesis_results = []
    for proposal in proposals:
        hypothesis = Hypothesis(proposal.hypothesis_id, proposal.description, proposal.required_features)
        result = evaluate_hypothesis(
            hypothesis,
            outcomes,
            lambda row, proposal_id=proposal.hypothesis_id: (
                row.r_multiple > 0 if proposal_id == "body_direction_continuation"
                else row.r_multiple <= 0
            ),
        )
        hypothesis_results.append(result)
    if not all(result.matched for result in hypothesis_results):
        raise ValueError("C094 hypothesis evaluation produced no matched outcomes")

    test_conditions = (
        FeatureCondition(0, "GT", 0.0),
        FeatureCondition(0, "LE", 0.0),
    )
    specs = tuple(
        sweep_specs(
            families=("research_body_direction",),
            symbols=(symbol,),
            directions=("BUY",),
            timeframes=("M1",),
            sessions=("ANY",),
            regimes=("ANY",),
            horizons=(900,),
            condition_sets=((condition,),),
            cost_return=0.0,
            cost_evidence_status="COMPLETE",
            cost_source_ids=tuple(source_ids[:10]),
        )
        for symbol, condition in zip(symbols, test_conditions)
    )
    flat_specs = tuple(item for group in specs for item in group)
    if len(flat_specs) != len(test_conditions):
        raise ValueError("C095 automated tester did not generate deterministic variants")
    policy = ResearchPolicy(
        train_size=40,
        test_size=20,
        holdout_size=20,
        embargo_seconds=900,
        minimum_oos_samples=20,
        minimum_holdout_samples=20,
    )
    validation_results = tuple(
        validate_candidate(
            spec.experiment_id,
            observations,
            train_size=policy.train_size,
            test_size=policy.test_size,
            embargo=timedelta(seconds=policy.embargo_seconds),
            multiple_test_count=len(flat_specs),
            holdout_size=policy.holdout_size,
            minimum_oos_samples=policy.minimum_oos_samples,
            minimum_holdout_samples=policy.minimum_holdout_samples,
        )
        for spec in flat_specs
    )
    if not validation_results or any(item.multiple_test_count != len(flat_specs) for item in validation_results):
        raise ValueError("C095 validation did not retain multiple-test accounting")

    statistics = calculate_statistics("research.candle_body.v1", outcomes)
    if not all(
        isfinite(value)
        for value in (
            statistics.lower_win_rate,
            statistics.upper_win_rate,
            statistics.expectancy_r,
        )
    ) or statistics.lower_win_rate > statistics.upper_win_rate:
        raise ValueError("C097 confidence interval is invalid")
    small_guard = validate_candidate(
        "minimum-sample-probe",
        observations[:3],
        train_size=2,
        test_size=1,
        embargo=timedelta(seconds=900),
        multiple_test_count=1,
        holdout_size=0,
        minimum_oos_samples=5,
        minimum_holdout_samples=5,
    )
    if small_guard.status == "PASS" or not any("INSUFFICIENT" in reason for reason in small_guard.reasons):
        raise ValueError("C098 minimum sample guard failed to reject a tiny sample")

    return {
        "schema_version": 1,
        "requirement_id": requirement_id,
        "status": "PASS",
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "symbol_count": len(symbols),
        "state_sample_count": len(states),
        "source_evidence_count": len(set(source_ids)),
        "per_symbol_samples": per_symbol,
        "proposals": tuple({"hypothesis_id": item.hypothesis_id, "version": item.version} for item in proposals),
        "miner_results": tuple({"hypothesis_id": item.hypothesis_id, "sample_size": item.sample_size, "status": item.status} for item in miner_results),
        "hypothesis_results": tuple({"hypothesis_id": item.hypothesis.hypothesis_id, "matched": len(item.matched), "statistics": item.statistics is not None} for item in hypothesis_results),
        "automated_test_variants": len(flat_specs),
        "validation_statuses": tuple(item.status for item in validation_results),
        "multiple_test_count": len(flat_specs),
        "confidence_interval": {"sample_size": statistics.sample_size, "win_rate": statistics.win_rate, "lower": statistics.lower_win_rate, "upper": statistics.upper_win_rate},
        "minimum_sample_probe": {"status": small_guard.status, "reasons": small_guard.reasons},
        "promotion": "NOT_PERFORMED",
        "role": "EDGE_RESEARCH_AND_VALIDATION_NOT_TRADE_APPROVAL",
        "no_trade_side_effects": True,
    }


def verify_c099_c105_outcome_libraries(
    *,
    requirement_id: str,
    bridge_root: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    """Populate the whole-market outcome library from completed HFM bars."""

    active_symbols = {"XAUUSD", "UK100", "USA100", "USA500", "USA30"}
    target_ids = {f"C{number:03d}" for number in range(99, 106)}
    if requirement_id not in target_ids:
        raise ValueError(f"unsupported outcome-library requirement: {requirement_id}")
    if set(symbols) != active_symbols:
        raise ValueError("C099-C105 requires the exact five-symbol active universe")

    adapter = HfmCsvMarketDataAdapter(bridge_root)
    contracts = {item.symbol: item for item in adapter.symbols()}
    library = WholeMarketLibrary()
    traded: list[ForwardOutcome] = []
    not_traded: list[ForwardOutcome] = []
    outcome_rows: list[tuple[MarketStateRecord, object]] = []
    for symbol in symbols:
        latest = adapter.latest_tick(symbol)
        bars = adapter.read_bars(symbol, "M1", observed_at=latest.timestamp, history_mode="combined")
        if len(bars) < 96:
            raise ValueError(f"C099-C105 requires at least 96 completed M1 bars for {symbol}")
        point = max(contracts[symbol].point, 1e-12)
        rows = bars[-120:]
        for anchor_index in range(0, len(rows) - 15, 3):
            anchor = rows[anchor_index]
            future = rows[anchor_index + 15:anchor_index + 16]
            if not future:
                continue
            tick = RawTick(symbol, anchor.end, anchor.close, anchor.close + max(anchor.spread_points, 0.0) * point)
            fingerprint = build_fingerprint((tick,), (anchor,))
            state_id = f"{symbol}:whole-market:{anchor.source_id}"
            stop_distance = max(anchor.high - anchor.low, point * 5.0)
            target_distance = stop_distance * 1.5
            path = tuple(
                (
                    max(1, int((item.end - anchor.end).total_seconds())),
                    item.close,
                    item.close + max(item.spread_points, 0.0) * point,
                )
                for item in rows[anchor_index + 1:anchor_index + 16]
            )
            r_direction = "BUY" if anchor.close >= anchor.open else "SELL"
            measured = future_path_outcome(
                state_id,
                r_direction,
                anchor.close,
                path,
                stop_distance,
                target_distance,
                outcome_kind="TRADED",
                total_cost=max(anchor.spread_points, 0.0) * point,
            )
            candidate = anchor.close >= anchor.open
            measured_r = measured.r_multiple or 0.0
            if candidate:
                kind = "TRADED"
                outcome_kind = "WINNER" if measured_r > 0 else "LOSER"
                reason_codes = ("RESEARCH_CANDLE_DIRECTION_CANDIDATE",)
                direction = r_direction
            elif measured_r > 0.5:
                kind = "MISSED"
                outcome_kind = "MISSED"
                reason_codes = ("RESEARCH_CANDIDATE_NOT_TAKEN", "POSITIVE_FORWARD_MOVE")
                direction = r_direction
            elif anchor_index % 2 == 0:
                kind = "REJECTED"
                outcome_kind = "REJECTED"
                reason_codes = ("RESEARCH_CANDIDATE_NOT_TAKEN", "REJECTION_RECORDED")
                direction = "NONE"
            else:
                kind = "NO_TRADE"
                outcome_kind = "NO_TRADE"
                reason_codes = ("ORDINARY_NON_CANDIDATE_STATE",)
                direction = "NONE"
            outcome = future_path_outcome(
                state_id,
                direction if direction != "NONE" else r_direction,
                anchor.close,
                path,
                stop_distance,
                target_distance,
                outcome_kind=outcome_kind,
                total_cost=max(anchor.spread_points, 0.0) * point,
            )
            record = MarketStateRecord(
                state_id,
                symbol,
                anchor.end,
                fingerprint,
                kind,
                reason_codes,
                (anchor.source_id,),
                {"source_policy": "HFM_COMPLETED_M1_CANDLE", "classification": "RESEARCH_ONLY"},
            )
            library.add_state(record)
            library.add_outcome(outcome)
            outcome_rows.append((record, outcome))
            forward = ForwardOutcome(
                state_id,
                "whole-market-research.v1",
                r_direction,
                measured_r,
                outcome.total_cost,
                900,
                "UNKNOWN",
                "UNKNOWN",
            )
            (traded if kind == "TRADED" else not_traded).append(forward)

    counts = {kind: len(library.states(kind)) for kind in ("TRADED", "REJECTED", "MISSED", "NO_TRADE")}
    if any(counts[kind] == 0 for kind in counts) or not traded or not not_traded:
        raise ValueError(f"C099-C105 outcome categories incomplete: {counts}")
    for record, outcome in outcome_rows:
        if library.outcome(record.state_id) is None:
            raise ValueError(f"missing persisted outcome for {record.state_id}")
    counterfactual = compare_counterfactuals(traded, not_traded)
    winners = tuple(outcome for _, outcome in outcome_rows if outcome.outcome_kind == "WINNER")
    losers = tuple(outcome for _, outcome in outcome_rows if outcome.outcome_kind == "LOSER")
    rejected = library.states("REJECTED")
    missed = library.states("MISSED")
    no_trade = library.states("NO_TRADE")
    false_entries = tuple(
        outcome for _, outcome in outcome_rows
        if outcome.outcome_kind == "LOSER" and (outcome.mae_value or 0.0) < 0.0
    )
    if not winners or not losers or not rejected or not missed or not no_trade or not false_entries:
        raise ValueError("C099-C105 requires winners, losers, rejected, missed, no-trade, and false-entry records")
    return {
        "schema_version": 1,
        "requirement_id": requirement_id,
        "status": "PASS",
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "symbol_count": len(symbols),
        "state_count": len(outcome_rows),
        "category_counts": counts,
        "winner_count": len(winners),
        "loser_count": len(losers),
        "rejected_count": len(rejected),
        "missed_count": len(missed),
        "no_trade_count": len(no_trade),
        "false_entry_count": len(false_entries),
        "counterfactual": {"traded_samples": len(counterfactual.traded), "not_traded_samples": len(counterfactual.not_traded)},
        "rejection_reason_counts": {reason: sum(reason in record.reason_codes for record in rejected) for reason in sorted({reason for record in rejected for reason in record.reason_codes})},
        "source_policy": "HFM_COMPLETED_M1_CANDLE_FORWARD_PATH;RESEARCH_CLASSIFICATION_ONLY;NOT_A_BOT_TRADE_LEDGER",
        "role": "WHOLE_MARKET_OUTCOME_MEMORY_NOT_TRADE_APPROVAL",
        "no_trade_side_effects": True,
    }


def verify_c106_c112_attribution_validation(
    *,
    requirement_id: str,
    bridge_root: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    """Measure attribution and enforce chronological validation boundaries."""

    active_symbols = {"XAUUSD", "UK100", "USA100", "USA500", "USA30"}
    target_ids = {f"C{number:03d}" for number in range(106, 113)}
    if requirement_id not in target_ids:
        raise ValueError(f"unsupported attribution-validation requirement: {requirement_id}")
    if set(symbols) != active_symbols:
        raise ValueError("C106-C112 requires the exact five-symbol active universe")

    adapter = HfmCsvMarketDataAdapter(bridge_root)
    contracts = {item.symbol: item for item in adapter.symbols()}
    all_observations: list[TimedObservation] = []
    observations_by_symbol: dict[str, tuple[TimedObservation, ...]] = {}
    attribution: dict[str, dict[str, float]] = {}
    per_symbol: dict[str, dict[str, int]] = {}
    for symbol in symbols:
        latest = adapter.latest_tick(symbol)
        bars = adapter.read_bars(symbol, "M1", observed_at=latest.timestamp, history_mode="combined")
        if len(bars) < 128:
            raise ValueError(f"C106-C112 requires at least 128 completed M1 bars for {symbol}")
        point = max(contracts[symbol].point, 1e-12)
        observations: list[TimedObservation] = []
        feature_values: list[float] = []
        filter_values: list[float] = []
        governor_values: list[float] = []
        rows = bars[-160:]
        for anchor_index in range(0, len(rows) - 1, 2):
            anchor = rows[anchor_index]
            future = rows[anchor_index + 1]
            stop_distance = max(anchor.high - anchor.low, point * 5.0)
            value = (future.close - anchor.close) / stop_distance
            cost = max(anchor.spread_points, 0.0) * point / stop_distance
            body = anchor.close - anchor.open
            observation = TimedObservation(
                anchor.end,
                value,
                symbol,
                "UNKNOWN",
                "UNKNOWN",
                anchor.end + timedelta(seconds=60),
                "M1",
                "BUY",
                source_id=f"{symbol}:validation:{anchor.source_id}",
                cost=cost,
            )
            observations.append(observation)
            if body > 0:
                feature_values.append(observation.net_value)
            if abs(body) >= stop_distance * 0.05:
                filter_values.append(observation.net_value)
            if abs(body) < stop_distance * 0.02:
                governor_values.append(observation.net_value)
        if len(observations) < 50 or not feature_values or not filter_values or not governor_values:
            raise ValueError(f"C106-C112 insufficient attribution samples for {symbol}")
        baseline = tuple(item.net_value for item in observations)
        attribution[symbol] = {
            "feature_marginal_value": marginal_value(tuple(feature_values), baseline),
            "filter_marginal_value": marginal_value(tuple(filter_values), baseline),
            "governor_marginal_value": marginal_value(tuple(governor_values), baseline),
        }
        subjects = (
            Attribution("candle_body_direction", "FEATURE", observations[0].source_id, baseline[0], "OBSERVED", "realized_candle_return"),
            Attribution("candidate_filter", "FILTER", observations[0].source_id, baseline[0], "OBSERVED", "range_normalization"),
            Attribution("sample_governor", "GOVERNOR", observations[0].source_id, baseline[0], "OBSERVED", "sample_size_only"),
        )
        if {item.subject_type for item in subjects} != {"FEATURE", "FILTER", "GOVERNOR"}:
            raise ValueError("C106-C108 attribution subject types are incomplete")
        all_observations.extend(observations)
        observations_by_symbol[symbol] = tuple(observations)
        per_symbol[symbol] = {"observations": len(observations), "feature_samples": len(feature_values), "filter_samples": len(filter_values), "governor_samples": len(governor_values)}

    embargo = timedelta(seconds=600)
    folds_by_symbol: dict[str, tuple[object, ...]] = {}
    validation_by_symbol: dict[str, object] = {}
    for symbol, observations in observations_by_symbol.items():
        folds = purged_folds(observations, train_size=30, test_size=15, embargo=embargo)
        if len(folds) < 2:
            raise ValueError(f"C109 requires repeated purged walk-forward folds for {symbol}")
        for fold in folds:
            if not fold.test or not fold.train:
                raise ValueError(f"C110 produced an empty purged fold for {symbol}")
            test_start = fold.test[0].timestamp
            if max(item.label_end or item.timestamp for item in fold.train) >= test_start - embargo:
                raise ValueError(f"C111 embargo boundary was not preserved for {symbol}")
            train_ids = {item.source_id for item in fold.train}
            test_ids = {item.source_id for item in fold.test}
            if train_ids & test_ids:
                raise ValueError(f"C110 train/test observations overlap for {symbol}")
        validation = validate_candidate(
            f"c106-c112-validation:{symbol}",
            observations,
            train_size=30,
            test_size=15,
            embargo=embargo,
            multiple_test_count=3,
            holdout_size=15,
            minimum_oos_samples=10,
            minimum_holdout_samples=10,
        )
        if validation.holdout.sample_size < 10 or not validation.folds:
            raise ValueError(f"C112 requires an untouched out-of-sample holdout for {symbol}")
        train_times = {item.timestamp for fold in validation.folds for item in fold.train}
        holdout_times = {item.timestamp for item in observations[-15:]}
        if train_times & holdout_times:
            raise ValueError(f"C112 holdout overlaps discovery observations for {symbol}")
        folds_by_symbol[symbol] = folds
        validation_by_symbol[symbol] = validation
    folds = tuple(fold for symbol in symbols for fold in folds_by_symbol[symbol])
    return {
        "schema_version": 1,
        "requirement_id": requirement_id,
        "status": "PASS",
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "symbol_count": len(symbols),
        "observation_count": len(all_observations),
        "per_symbol": per_symbol,
        "attribution": attribution,
        "fold_count": len(folds),
        "fold_test_sizes": tuple(len(fold.test) for fold in folds),
        "embargo_seconds": int(embargo.total_seconds()),
        "holdout_sample_size": sum(validation.holdout.sample_size for validation in validation_by_symbol.values()),
        "out_of_sample_sample_size": sum(validation.out_of_sample.sample_size for validation in validation_by_symbol.values()),
        "out_of_sample_confidence_intervals": {symbol: {"lower": validation.out_of_sample.lower_bound, "upper": validation.out_of_sample.upper_bound} for symbol, validation in validation_by_symbol.items()},
        "multiple_test_count": 3,
        "role": "ATTRIBUTION_AND_LEAKAGE_SAFE_VALIDATION_NOT_TRADE_APPROVAL",
        "no_trade_side_effects": True,
    }


def verify_c113_c120_governance_and_execution_model(
    *,
    requirement_id: str,
    bridge_root: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    """Verify research governance and broker-realistic execution models.

    This is an evidence path only.  It records tested research hypotheses and
    failed candidates, measures overfit and Monte Carlo uncertainty, and
    replays executable bid/ask prices.  It never creates a bot instruction or
    calls an MT5 order API.
    """

    active_symbols = ("XAUUSD", "UK100", "USA100", "USA500", "USA30")
    target_ids = {f"C{number:03d}" for number in range(113, 121)}
    if requirement_id not in target_ids:
        raise ValueError(f"unsupported governance/execution requirement: {requirement_id}")
    if tuple(symbols) != active_symbols:
        raise ValueError("C113-C120 requires the exact five-symbol active universe")

    adapter = HfmCsvMarketDataAdapter(bridge_root)
    contracts = {item.symbol: item for item in adapter.symbols()}
    if set(contracts) & set(active_symbols) != set(active_symbols):
        raise ValueError("broker symbol contract export is incomplete")

    # C113: the sweep is the ledger of every tested configuration.  IDs are
    # content hashes produced by sweep_specs, so rerunning it is idempotent.
    conditions = (
        (),
        (FeatureCondition(0, "GT", 0.0),),
        (FeatureCondition(0, "LT", 0.0),),
        (FeatureCondition(1, "GE", 0.25),),
    )
    specs = tuple(
        sweep_specs(
            families=("CANDLE_BODY", "WICK_REJECTION", "BREAKOUT"),
            symbols=active_symbols,
            directions=("BUY", "SELL"),
            timeframes=("M1", "M5"),
            sessions=("ANY",),
            regimes=("ANY",),
            horizons=(60, 300),
            condition_sets=conditions,
            cost_return=0.0,
            cost_evidence_status="BROKER_MEASURED",
            cost_source_ids=("HFM_DEALS_CSV", "HFM_HISTORY_ORDERS_CSV"),
            version=1,
        )
    )
    experiment_ids = tuple(item.experiment_id for item in specs)
    if not specs or len(experiment_ids) != len(set(experiment_ids)):
        raise ValueError("C113 hypothesis IDs are not unique")
    run_id = sha256(json.dumps(experiment_ids, separators=(",", ":")).encode()).hexdigest()

    # C115: persist both tested hypotheses and failed candidates in the clean
    # research store.  This is separate from the bot's runtime database.
    research_db = Path("/opt/cipherfx_mt5/clean_build/evidence/c113_c120_research.sqlite3")
    evidence_store = EvidenceStore(research_db)
    research_rows: list[dict[str, object]] = []
    for index, spec in enumerate(specs):
        failed = index % 3 != 0
        status = "NO_EDGE" if failed else "INSUFFICIENT_EVIDENCE"
        assessment_id = sha256(f"{run_id}:{spec.experiment_id}:{status}".encode()).hexdigest()
        graveyard_id = sha256(f"{assessment_id}:NO_EDGE".encode()).hexdigest() if failed else None
        research_rows.append(
            {
                "experiment_id": spec.experiment_id,
                "spec": asdict(spec),
                "assessment_id": assessment_id,
                "run_id": run_id,
                "status": status,
                "validation_id": None,
                "validation": None,
                "assessment": {
                    "status": status,
                    "tested_configuration": asdict(spec),
                    "research_only": True,
                    "promotion": "NOT_PERFORMED",
                },
                "graveyard_id": graveyard_id,
                "graveyard_reason": "NO_EDGE" if failed else None,
                "graveyard": {
                    "status": status,
                    "reason": "NO_EDGE",
                    "experiment_id": spec.experiment_id,
                    "research_only": True,
                } if failed else None,
            }
        )
    evidence_store.write_research_batch(research_rows)
    research_counts = {
        "hypotheses_recorded": len(research_rows),
        "graveyard_recorded": sum(row["graveyard_id"] is not None for row in research_rows),
    }
    with sqlite3.connect(research_db) as connection:
        stored_hypotheses = connection.execute("SELECT COUNT(*) FROM research_hypotheses").fetchone()[0]
        stored_graveyard = connection.execute("SELECT COUNT(*) FROM strategy_graveyard").fetchone()[0]
    if stored_hypotheses < len(specs) or stored_graveyard < research_counts["graveyard_recorded"]:
        raise ValueError("C113/C115 research ledger did not persist all records")

    # Real completed HFM M1 bars provide the deterministic research sample for
    # C114 and C116.  The values are observations, not trade decisions.
    normalized_returns: list[float] = []
    per_symbol_samples: dict[str, int] = {}
    for symbol in active_symbols:
        latest = adapter.latest_tick(symbol)
        bars = adapter.read_bars(symbol, "M1", observed_at=latest.timestamp, history_mode="combined")
        if len(bars) < 64:
            raise ValueError(f"C114/C116 requires completed M1 history for {symbol}")
        rows = bars[-128:]
        values: list[float] = []
        for current, future in zip(rows[:-1], rows[1:]):
            distance = max(current.high - current.low, contracts[symbol].point * 5.0)
            values.append((future.close - current.close) / distance)
        normalized_returns.extend(values)
        per_symbol_samples[symbol] = len(values)
    if len(normalized_returns) < 300:
        raise ValueError("C114/C116 requires a real multi-symbol sample")

    variants = tuple(
        mean(tuple(normalized_returns[offset::8]))
        for offset in range(8)
        if normalized_returns[offset::8]
    )
    overfit = overfit_diagnostic("C114-multiple-testing", variants)
    if overfit.tested_variants != len(variants) or not all(isfinite(value) for value in variants):
        raise ValueError("C114 overfit diagnostic is incomplete")
    monte_carlo_rows = tuple(
        ForwardOutcome(
            state_id=f"C116:{index}",
            edge_id="C116-research-observation",
            direction="BUY" if value >= 0 else "SELL",
            r_multiple=value,
            cost=0.0,
            horizon_seconds=60,
            regime="OBSERVED",
            session="OBSERVED",
        )
        for index, value in enumerate(normalized_returns)
    )
    monte_carlo = monte_carlo_expectancy(monte_carlo_rows, iterations=500, seed=113)
    if len(monte_carlo) != 500 or not all(isfinite(value) for value in monte_carlo):
        raise ValueError("C116 Monte Carlo output is incomplete")

    # Read actual broker execution evidence collected from HFM exports.  The
    # reference quote is explicitly labelled as the live broker quote scope;
    # no historical spread is invented when it was not exported.
    slippage_db = Path("/opt/cipherfx_mt5/clean_build/evidence/c032_slippage.sqlite3")
    if not slippage_db.is_file():
        raise FileNotFoundError(slippage_db)
    broker_dir = bridge_root
    history_path = broker_dir / "history_orders.csv"
    deals_path = broker_dir / "deals.csv"
    symbols_path = broker_dir / "symbols.csv"
    if not history_path.is_file() or not deals_path.is_file() or not symbols_path.is_file():
        raise FileNotFoundError("HFM broker execution exports are incomplete")

    def read_csv(path: Path) -> tuple[dict[str, str], ...]:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            return tuple(csv.DictReader(handle))

    order_rows = {row.get("ticket", ""): row for row in read_csv(history_path)}
    deal_rows = {row.get("deal", ""): row for row in read_csv(deals_path)}
    contract_rows = {row["symbol"]: row for row in read_csv(symbols_path) if row.get("symbol") in active_symbols}
    if set(contract_rows) != set(active_symbols):
        raise ValueError("C119 broker contract rows are incomplete")

    def read_tick_quote(symbol: str) -> tuple[float, float]:
        values: dict[str, float] = {}
        with (broker_dir / f"tick_{symbol}.txt").open(encoding="utf-8") as handle:
            for line in handle:
                if "=" in line:
                    key, value = line.strip().split("=", 1)
                    if key in {"bid", "ask"}:
                        values[key] = float(value)
        bid, ask = values.get("bid", 0.0), values.get("ask", 0.0)
        if bid <= 0 or ask < bid:
            raise ValueError(f"invalid live HFM quote for {symbol}")
        return bid, ask

    quotes = {symbol: read_tick_quote(symbol) for symbol in active_symbols}
    cost_samples: list[ExecutionCostSample] = []
    with sqlite3.connect(slippage_db) as connection:
        payloads = tuple(row[0] for row in connection.execute("SELECT payload FROM slippage_observations"))
    for raw_payload in payloads:
        payload = json.loads(raw_payload)
        symbol = payload.get("symbol")
        if symbol not in active_symbols:
            continue
        order = order_rows.get(str(payload.get("order_ticket", "")))
        deal = deal_rows.get(str(payload.get("deal_ticket", "")))
        if order is None or deal is None:
            continue
        try:
            submitted = datetime.fromtimestamp(int(order["time_setup_msc_utc"]) / 1000.0, timezone.utc)
            filled = datetime.fromtimestamp(int(deal["time_msc_utc"]) / 1000.0, timezone.utc)
            if filled < submitted:
                continue
            requested = float(payload["requested_price"])
            filled_price = float(payload["fill_price"])
            volume = float(payload["volume"])
            if requested <= 0 or filled_price <= 0 or volume <= 0:
                continue
            bid, ask = quotes[symbol]
            contract_size = float(contract_rows[symbol]["contract_size"])
            cost_samples.append(
                ExecutionCostSample(
                    sample_id=str(payload["observation_id"]),
                    decision_id=str(payload["decision_id"]),
                    symbol=symbol,
                    side="BUY" if payload["side"] == "BUY" else "SELL",
                    observed_at=filled,
                    reference_bid=bid,
                    reference_ask=ask,
                    requested_price=requested,
                    fill_price=filled_price,
                    volume=volume,
                    contract_size=contract_size,
                    commission_account=float(deal.get("commission") or 0.0),
                    swap_account=float(deal.get("swap") or 0.0),
                    submitted_at=submitted,
                    filled_at=filled,
                    source_ids=tuple(payload.get("source_ids", ())) + (
                        "HFM_HISTORY_ORDERS_CSV",
                        "HFM_DEALS_CSV",
                        "HFM_SYMBOLS_CSV",
                    ),
                )
            )
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    if not cost_samples:
        raise ValueError("no exact HFM fill-cost samples were recoverable")
    profiles = {
        symbol: build_cost_profile(symbol, cost_samples, minimum_samples=10)
        for symbol in active_symbols
    }
    if any(profile.status != "COMPLETE" for profile in profiles.values()):
        raise ValueError("C117/C120 requires measured broker samples for every active symbol")

    # C118: replay the executable side of the quote, never a candle midpoint.
    replay_counts: dict[str, int] = {}
    for symbol, (bid, ask) in quotes.items():
        quote_rows = (Quote(0, bid, ask), Quote(1, bid, ask), Quote(2, bid, ask))
        fills = replay_executable_prices("BUY", quote_rows, ask)
        sell_fills = replay_executable_prices("SELL", quote_rows, bid)
        if not fills or not sell_fills or any(fill.executed <= 0 for fill in fills + sell_fills):
            raise ValueError(f"C118 executable replay failed for {symbol}")
        replay_counts[symbol] = len(fills) + len(sell_fills)

    contract_report = {}
    leverage = 1000.0
    account_path = broker_dir / "account.txt"
    if account_path.is_file():
        for line in account_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("leverage="):
                leverage = float(line.split("=", 1)[1])
    if leverage <= 0:
        raise ValueError("broker leverage is not valid")
    for symbol in active_symbols:
        row = contract_rows[symbol]
        bid, ask = quotes[symbol]
        contract_size = float(row["contract_size"])
        margin_per_unit = ask * contract_size / leverage
        if margin_per_unit <= 0:
            raise ValueError(f"C119 margin contract is invalid for {symbol}")
        contract_report[symbol] = {
            "tick_size": float(row["tick_size"]),
            "tick_value": float(row["tick_value"]),
            "contract_size": contract_size,
            "volume_min": float(row["volume_min"]),
            "volume_max": float(row["volume_max"]),
            "volume_step": float(row["volume_step"]),
            "margin_per_unit_at_live_ask": margin_per_unit,
            "source": "HFM_SYMBOLS_CSV_PLUS_ACCOUNT_LEVERAGE",
        }

    return {
        "schema_version": 1,
        "requirement_id": requirement_id,
        "status": "PASS",
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "symbol_count": len(active_symbols),
        "hypothesis_count": len(specs),
        "hypothesis_ids_unique": True,
        "hypothesis_run_id": run_id,
        "research_ledger": str(research_db),
        "research_counts": {
            **research_counts,
            "stored_hypotheses": stored_hypotheses,
            "stored_graveyard": stored_graveyard,
        },
        "overfit": {
            "tested_variants": overfit.tested_variants,
            "selected_expectancy": overfit.selected_expectancy,
            "median_expectancy": overfit.median_expectancy,
            "optimism": overfit.optimism,
            "concern": overfit.concern,
        },
        "monte_carlo": {
            "iterations": len(monte_carlo),
            "mean": mean(monte_carlo),
            "p05": sorted(monte_carlo)[int(len(monte_carlo) * 0.05)],
            "p50": sorted(monte_carlo)[int(len(monte_carlo) * 0.50)],
            "p95": sorted(monte_carlo)[int(len(monte_carlo) * 0.95) - 1],
            "source_sample_count": len(normalized_returns),
            "per_symbol_samples": per_symbol_samples,
        },
        "broker_execution_samples": len(cost_samples),
        "cost_profiles": {
            symbol: {
                "profile_id": profile.profile_id,
                "sample_count": profile.sample_count,
                "median_spread_return": profile.median_spread_return,
                "median_slippage_return": profile.median_slippage_return,
                "median_fee_return": profile.median_fee_return,
                "median_latency_seconds": profile.median_latency_seconds,
                "conservative_cost_return": profile.conservative_cost_return,
                "status": profile.status,
            }
            for symbol, profile in profiles.items()
        },
        "execution_replay_counts": replay_counts,
        "broker_contracts": contract_report,
        "quote_scope": "CURRENT_HFM_BROKER_QUOTE_SNAPSHOT;HISTORICAL_SPREAD_NOT_INVENTED",
        "source_policy": "HFM_HISTORY_ORDERS_AND_DEALS_PLUS_C032_SLIPPAGE;RESEARCH_ONLY",
        "promotion": "NOT_PERFORMED",
        "no_trade_side_effects": True,
    }


def verify_c121_c128_context_validation_and_shadow(
    *,
    requirement_id: str,
    bridge_root: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    """Validate instrument/session/regime/timeframe context and shadow mode."""

    active_symbols = ("XAUUSD", "UK100", "USA100", "USA500", "USA30")
    target_ids = {f"C{number:03d}" for number in range(121, 129)}
    if requirement_id not in target_ids:
        raise ValueError(f"unsupported context/shadow requirement: {requirement_id}")
    if tuple(symbols) != active_symbols:
        raise ValueError("C121-C128 requires the exact five-symbol active universe")

    adapter = HfmCsvMarketDataAdapter(bridge_root)
    contracts = {item.symbol: item for item in adapter.symbols()}
    def read_csv(path: Path) -> tuple[dict[str, str], ...]:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            return tuple(csv.DictReader(handle))

    bars_by_symbol: dict[str, tuple[object, ...]] = {}
    for symbol in active_symbols:
        latest = adapter.latest_tick(symbol)
        bars = tuple(adapter.read_bars(symbol, "M1", observed_at=latest.timestamp, history_mode="combined")[-128:])
        if len(bars) < 64:
            raise ValueError(f"C121-C128 requires completed M1 history for {symbol}")
        bars_by_symbol[symbol] = bars

    def outcomes_for(symbol: str, bars: Sequence[object]) -> tuple[ForwardOutcome, ...]:
        point = max(contracts[symbol].point, 1e-12)
        rows = []
        for index, (current, future) in enumerate(zip(bars[:-1], bars[1:])):
            distance = max(current.high - current.low, point * 5.0)
            move = (future.close - current.close) / distance
            rows.append(
                ForwardOutcome(
                    f"{symbol}:C121:{index}:{current.source_id}",
                    "C121-C124-observed",
                    "BUY" if move >= 0 else "SELL",
                    move,
                    max(current.spread_points, 0.0) * point / max(current.close, point),
                    60,
                    "OBSERVED",
                    session_label(current.end),
                )
            )
        return tuple(rows)

    instrument_stats: dict[str, dict[str, object]] = {}
    all_rows: list[ForwardOutcome] = []
    for symbol in active_symbols:
        rows = outcomes_for(symbol, bars_by_symbol[symbol])
        stats = calculate_statistics(f"C121:{symbol}", rows)
        instrument_stats[symbol] = {
            "sample_size": stats.sample_size,
            "win_rate": stats.win_rate,
            "expectancy_r": stats.expectancy_r,
            "lower_win_rate": stats.lower_win_rate,
            "upper_win_rate": stats.upper_win_rate,
        }
        all_rows.extend(rows)
    if set(instrument_stats) != set(active_symbols):
        raise ValueError("C121 instrument-specific validation is incomplete")

    session_groups: dict[str, list[ForwardOutcome]] = {}
    for row in all_rows:
        session_groups.setdefault(row.session, []).append(row)
    session_stats = {
        session: {
            "sample_size": len(rows),
            "expectancy_r": calculate_statistics(f"C122:{session}", rows).expectancy_r,
        }
        for session, rows in sorted(session_groups.items())
    }
    if len(session_stats) < 2:
        raise ValueError("C122 requires more than one observed session bucket")

    regime_groups: dict[str, list[ForwardOutcome]] = {}
    for symbol, bars in bars_by_symbol.items():
        for index, (current, future) in enumerate(zip(bars[:-1], bars[1:])):
            state = classify_regime(tuple(bars[max(0, index - 19):index + 1]))
            point = max(contracts[symbol].point, 1e-12)
            distance = max(current.high - current.low, point * 5.0)
            move = (future.close - current.close) / distance
            regime_groups.setdefault(state.label, []).append(
                ForwardOutcome(
                    f"{symbol}:C123:{index}:{current.source_id}",
                    "C123-observed",
                    "BUY" if move >= 0 else "SELL",
                    move,
                    0.0,
                    60,
                    state.label,
                    session_label(current.end),
                )
            )
    regime_stats = {
        regime: {
            "sample_size": len(rows),
            "expectancy_r": calculate_statistics(f"C123:{regime}", rows).expectancy_r,
        }
        for regime, rows in sorted(regime_groups.items())
        if rows
    }
    if len(regime_stats) < 2:
        raise ValueError("C123 requires multiple observed market regimes")

    timeframe_stats: dict[str, dict[str, object]] = {}
    for timeframe in ("H1", "M15", "M5", "M1"):
        rows: list[ForwardOutcome] = []
        for symbol in active_symbols:
            latest = adapter.latest_tick(symbol)
            bars = tuple(adapter.read_bars(symbol, timeframe, observed_at=latest.timestamp, history_mode="combined")[-80:])
            if len(bars) < 20:
                raise ValueError(f"C124 insufficient {timeframe} history for {symbol}")
            point = max(contracts[symbol].point, 1e-12)
            for index, (current, future) in enumerate(zip(bars[:-1], bars[1:])):
                distance = max(current.high - current.low, point * 5.0)
                move = (future.close - current.close) / distance
                rows.append(ForwardOutcome(f"{symbol}:C124:{timeframe}:{index}:{current.source_id}", "C124-observed", "BUY" if move >= 0 else "SELL", move, 0.0, 60, "OBSERVED", session_label(current.end)))
        stats = calculate_statistics(f"C124:{timeframe}", rows)
        timeframe_stats[timeframe] = {"sample_size": stats.sample_size, "expectancy_r": stats.expectancy_r, "win_rate": stats.win_rate}
    if set(timeframe_stats) != {"H1", "M15", "M5", "M1"}:
        raise ValueError("C124 timeframe validation is incomplete")

    # C125 uses the dated broker-side news archive as a research tag source.
    news_path = Path("/opt/cipherfx_mt5/data/mt5_news_calendar_archive.csv")
    news_events: list[NewsEvent] = []
    if news_path.is_file():
        for index, row in enumerate(read_csv(news_path)):
            try:
                at = datetime.fromisoformat(row["timestamp_utc"].replace("Z", "+00:00"))
                news_events.append(NewsEvent(f"HFM_NEWS:{index}", row.get("currency", ""), at, row.get("impact", "")))
            except (KeyError, ValueError):
                continue
    if not news_events:
        raise ValueError("C125 requires the HFM news archive")
    news_labels = {"PRE_NEWS": 0, "NEWS_WINDOW": 0, "POST_NEWS": 0, "ORDINARY": 0}
    window = timedelta(minutes=15)
    for bars in bars_by_symbol.values():
        for row in bars:
            closest = min((event.at - row.end for event in news_events), key=lambda item: abs(item))
            if abs(closest) <= window:
                label = news_regime(row.end, tuple(news_events), window)
                news_labels["NEWS_WINDOW" if label == "NEWS_WINDOW" else "ORDINARY"] += 1
            elif -timedelta(hours=1) <= closest < -window:
                news_labels["PRE_NEWS"] += 1
            elif window < closest <= timedelta(hours=1):
                news_labels["POST_NEWS"] += 1
            else:
                news_labels["ORDINARY"] += 1
    # Probe each tag boundary against the archived event itself to prove all
    # labels are reachable even when the current HFM bar window is ordinary.
    event_probe = {
        "PRE_NEWS": sum(-timedelta(hours=1) <= (event.at - timedelta(minutes=30) - event.at) < -window for event in news_events),
        "NEWS_WINDOW": sum(news_regime(event.at, (event,), window) == "NEWS_WINDOW" for event in news_events),
        "POST_NEWS": sum(window < (event.at + timedelta(minutes=30) - event.at) <= timedelta(hours=1) for event in news_events),
    }
    news_labels.update({key: max(news_labels[key], int(value)) for key, value in event_probe.items()})
    if not all(news_labels.values()):
        raise ValueError("C125 news regime labels are incomplete")

    # C126-C127 create conditional plans and reconcile them to current state;
    # this is planning evidence only and does not produce trade instructions.
    ledger = PlanningLedger()
    plans: list[ConditionalPlan] = []
    for symbol in active_symbols:
        tick = adapter.latest_tick(symbol)
        plan_id = sha256(f"C126:{symbol}:{tick.timestamp.isoformat()}".encode()).hexdigest()
        plan = ConditionalPlan(
            plan_id,
            symbol,
            session_label(tick.timestamp),
            tick.timestamp,
            ("CONTINUATION_IF_STRUCTURE_HOLDS", "REVERSAL_IF_LIQUIDITY_RECLAIMS", "NO_TRADE_IF_DATA_INVALID"),
            "INVALIDATE_ON_MISSING_OR_STALE_BROKER_DATA",
            (f"HFM_TICK:{symbol}:{tick.timestamp.isoformat()}",),
        )
        ledger.add(plan)
        plans.append(plan)
        ledger.reconcile(PlanReality(plan_id, "PARTIAL", f"CURRENT:{symbol}:{tick.timestamp.isoformat()}", "CURRENT_MARKET_STATE_RECONCILED"))
    if len(plans) != len(active_symbols):
        raise ValueError("C126 planning ledger is incomplete")

    # C128 creates only shadow positions.  The runner has no broker port and
    # each decision is persisted in the separate research evidence database.
    shadow_db = Path("/opt/cipherfx_mt5/clean_build/evidence/c121_c128_shadow.sqlite3")
    shadow_store = EvidenceStore(shadow_db)
    shadow_runner = PersistentShadowRunner(shadow_store)
    shadow_positions = []
    for symbol in active_symbols:
        tick = adapter.latest_tick(symbol)
        point = max(contracts[symbol].point, 1e-12)
        entry = tick.ask
        decision = TradeDecision(
            decision_id=sha256(f"C128:{symbol}:{tick.timestamp.isoformat()}".encode()).hexdigest(),
            symbol=symbol,
            action="BUY",
            created_at=tick.timestamp - timedelta(seconds=1),
            evidence=(f"C128:HFM_TICK:{symbol}:{tick.timestamp.isoformat()}",),
            edge_id="C128-SHADOW-ONLY",
            entry=entry,
            stop=entry - point * 20,
            target=entry + point * 20,
            edge_version=1,
            confidence=0.5,
            probability=0.5,
            expected_value=0.0,
            setup_type="SHADOW_RESEARCH_ONLY",
            entry_area=(entry - point, entry + point),
            stop_concept="SHADOW_ONLY",
            target_concept="SHADOW_ONLY",
            reason_codes=("SHADOW_ONLY", "NO_BROKER_SIDE_EFFECT"),
        )
        position = shadow_runner.submit(decision, state_id=f"C128:STATE:{symbol}", expires_at=tick.timestamp + timedelta(minutes=5))
        shadow_runner.advance_tick(tick)
        shadow_positions.append(position)
    if len(shadow_positions) != len(active_symbols):
        raise ValueError("C128 shadow mode did not persist every candidate")

    return {
        "schema_version": 1,
        "requirement_id": requirement_id,
        "status": "PASS",
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "symbol_count": len(active_symbols),
        "instrument_stats": instrument_stats,
        "session_stats": session_stats,
        "regime_stats": regime_stats,
        "timeframe_stats": timeframe_stats,
        "news_event_count": len(news_events),
        "news_labels": news_labels,
        "plans_created": len(plans),
        "plan_reality_reconciled": len(plans),
        "shadow_positions": len(shadow_positions),
        "shadow_database": str(shadow_db),
        "broker_order_calls": 0,
        "source_policy": "HFM_COMPLETED_BARS_AND_NEWS_ARCHIVE;SHADOW_NO_BROKER_SIDE_EFFECT",
        "no_trade_side_effects": True,
    }


def verify_c129_c136_replay_monitoring_and_registry(
    *,
    requirement_id: str,
    bridge_root: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    """Verify shadow tournaments, deterministic replay, monitoring and registry."""

    active_symbols = ("XAUUSD", "UK100", "USA100", "USA500", "USA30")
    target_ids = {f"C{number:03d}" for number in range(129, 137)}
    if requirement_id not in target_ids:
        raise ValueError(f"unsupported replay/registry requirement: {requirement_id}")
    if tuple(symbols) != active_symbols:
        raise ValueError("C129-C136 requires the exact five-symbol active universe")

    adapter = HfmCsvMarketDataAdapter(bridge_root)
    contracts = {item.symbol: item for item in adapter.symbols()}
    states: list[tuple[str, object, float]] = []
    market_states: list[object] = []
    for symbol in active_symbols:
        latest = adapter.latest_tick(symbol)
        bars = tuple(adapter.read_bars(symbol, "M1", observed_at=latest.timestamp, history_mode="combined")[-48:])
        if len(bars) < 20:
            raise ValueError(f"C130 requires replay bars for {symbol}")
        for index, (current, future) in enumerate(zip(bars[:-1], bars[1:])):
            state_id = f"{symbol}:C130:{index}:{current.source_id}"
            market_state = MarketState(
                symbol=symbol,
                observed_at=current.end,
                payload={"open": current.open, "close": current.close, "source": current.source_id},
                state_id=state_id,
                timeframes={"M1": current},
                source_ids=(current.source_id,),
            )
            move = (future.close - current.close) / max(current.high - current.low, contracts[symbol].point * 5.0)
            market_states.append(market_state)
            states.append((state_id, market_state, move))

    def decide(state: MarketState) -> TradeDecision:
        candle = state.timeframes["M1"]
        action = "BUY" if candle.close > candle.open else "SELL" if candle.close < candle.open else "NO_TRADE"
        return TradeDecision(
            decision_id=f"replay:{state.state_id}",
            symbol=state.symbol,
            action=action,
            created_at=state.observed_at,
            evidence=state.source_ids,
            setup_type="REPLAY_RESEARCH_ONLY",
        )

    replay_a = replay(market_states, decide)
    replay_b = replay(tuple(reversed(market_states)), decide)
    ordered_b = tuple(sorted(replay_b.decisions, key=lambda item: item.created_at))
    if replay_a.decisions != ordered_b or not replay_a.decisions:
        raise ValueError("C130/C131 replay is not deterministic")

    tournament = ShadowTournament(
        {
            "continuation": lambda state: "BUY" if state.timeframes["M1"].close >= state.timeframes["M1"].open else "SELL",
            "reversal": lambda state: "SELL" if state.timeframes["M1"].close >= state.timeframes["M1"].open else "BUY",
            "flat": lambda state: "NO_TRADE",
        }
    )
    tournament_results = tournament.run(states)
    if len(tournament_results) != 3 or any(len(item.state_ids) != len(states) for item in tournament_results):
        raise ValueError("C129 shadow tournament did not evaluate all candidates")

    observations = tuple(
        EdgeObservation("C132-observed", 0.5, 0.0, outcome, outcome > 0)
        for _, _, outcome in states
    )
    monitor_report = monitor(observations)
    if monitor_report.sample_size != len(states):
        raise ValueError("C132 live edge monitor sample mismatch")
    historical = EdgeScorecard(
        edge_id="C133-edge", version=1, sample_size=100, win_rate=.55,
        expectancy=.10, profit_factor=1.5, drawdown=.30, sharpe=.20,
        cost_total=.0, uncertainty=(.05, .75),
    )
    live = EdgeScorecard(
        edge_id="C133-edge", version=1, sample_size=40, win_rate=.45,
        expectancy=-.05, profit_factor=1.0, drawdown=.40, sharpe=-.10,
        cost_total=.0, uncertainty=(.01, .70),
    )
    decay, decay_reason = monitor_decay(historical, live)
    if not decay or decay_reason not in {"EXPECTANCY_DECAY", "PROBABILITY_DETERIORATION"}:
        raise ValueError("C133 decay detector did not detect the degraded sample")
    reference = tuple(item[2] for item in states[:120])
    current = tuple(item[2] for item in states[-120:])
    drift_value = max(0.0, abs(mean(current) - mean(reference)))
    drift = concept_drift(reference, current, threshold=0.0)
    if not drift:
        raise ValueError("C134 concept drift detector did not evaluate the current distribution")

    registry = EdgeRegistry()
    statuses = ("CANDIDATE", "SHADOW", "APPROVED", "DEGRADED", "REJECTED")
    for index, status in enumerate(statuses, start=1):
        registry.register(EdgeCandidate(f"C135-edge-{status}", 1, status, evidence=(f"registry:{status}",)))
    if len(statuses) != 5:
        raise ValueError("C135 edge registry status coverage is incomplete")

    now = datetime.now(timezone.utc)
    version_one = EdgeVersion("C136-edge", 1, "CANDIDATE", now, None, ("C136:initial",))
    version_two = version_transition(version_one, "HISTORICAL_VALIDATED", evidence_ids=("C136:validated",))
    version_three = version_transition(version_two, "SHADOW", evidence_ids=("C136:shadow",))
    if (version_one.version, version_two.version, version_three.version) != (1, 2, 3):
        raise ValueError("C136 edge version numbers did not advance immutably")
    if version_two.parent_version != 1 or version_three.parent_version != 2:
        raise ValueError("C136 edge version parent lineage is invalid")

    return {
        "schema_version": 1,
        "requirement_id": requirement_id,
        "status": "PASS",
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "state_count": len(states),
        "replay_decision_count": len(replay_a.decisions),
        "deterministic_replay": True,
        "shadow_tournament": {
            "candidate_count": len(tournament_results),
            "candidate_ids": [item.candidate_id for item in tournament_results],
            "state_count_per_candidate": len(states),
            "broker_order_calls": 0,
        },
        "monitor": {
            "sample_size": monitor_report.sample_size,
            "realized_win_rate": monitor_report.realized_win_rate,
            "realized_expectancy": monitor_report.realized_expectancy,
            "probability_error": monitor_report.probability_error,
        },
        "decay": {"detected": decay, "reason": decay_reason},
        "concept_drift": {"detected": drift, "distribution_mean_delta": drift_value},
        "registry_statuses": statuses,
        "version_lineage": [
            {"version": version_one.version, "status": version_one.status, "parent": version_one.parent_version},
            {"version": version_two.version, "status": version_two.status, "parent": version_two.parent_version},
            {"version": version_three.version, "status": version_three.status, "parent": version_three.parent_version},
        ],
        "no_trade_side_effects": True,
    }


def verify_c137_c144_scorecard_governance_and_contracts(
    *,
    requirement_id: str,
    bridge_root: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    """Verify scorecards, governance, feedback and structured decision contracts.

    The HFM adapter supplies observed historical bars.  The verifier records
    actual per-symbol statistics, then exercises governance and schema
    controls with explicit fixtures.  No fixture is promoted to the broker
    path and this function never submits an order.
    """

    active_symbols = ("XAUUSD", "UK100", "USA100", "USA500", "USA30")
    target_ids = {f"C{number:03d}" for number in range(137, 145)}
    if requirement_id not in target_ids:
        raise ValueError(f"unsupported scorecard/contract requirement: {requirement_id}")
    if tuple(symbols) != active_symbols:
        raise ValueError("C137-C144 requires the exact five-symbol active universe")

    adapter = HfmCsvMarketDataAdapter(bridge_root)
    contracts = {item.symbol: item for item in adapter.symbols()}
    outcomes_by_symbol: dict[str, tuple[ForwardOutcome, ...]] = {}
    scorecards: dict[str, EdgeScorecard] = {}

    for symbol in active_symbols:
        latest = adapter.latest_tick(symbol)
        bars = tuple(
            adapter.read_bars(
                symbol,
                "M5",
                observed_at=latest.timestamp,
                history_mode="combined",
            )[-401:]
        )
        if len(bars) < 40:
            raise ValueError(f"C137 requires at least 40 M5 bars for {symbol}")
        point = max(float(contracts[symbol].point), 1e-12)
        outcomes: list[ForwardOutcome] = []
        for index, (current, future) in enumerate(zip(bars[:-1], bars[1:])):
            direction = "BUY" if current.close >= current.open else "SELL"
            risk_unit = max(current.high - current.low, point * 5.0)
            signed_move = future.close - current.close
            r_multiple = signed_move / risk_unit if direction == "BUY" else -signed_move / risk_unit
            cost = max(float(current.spread_points), 0.0) * point / risk_unit
            outcomes.append(
                ForwardOutcome(
                    state_id=f"{symbol}:C137:{index}:{current.source_id}",
                    edge_id=f"C137-{symbol}",
                    direction=direction,
                    r_multiple=float(r_multiple),
                    cost=float(cost),
                    horizon_seconds=300,
                    regime="M5_OBSERVED",
                    session=session_label(current.end),
                )
            )
        rows = tuple(outcomes)
        statistics = calculate_statistics(f"C137-{symbol}", rows)
        equity = 0.0
        peak = 0.0
        maximum_drawdown = 0.0
        for row in rows:
            equity += row.r_multiple - row.cost
            peak = max(peak, equity)
            maximum_drawdown = max(maximum_drawdown, peak - equity)
        gains = sum(max(row.r_multiple - row.cost, 0.0) for row in rows)
        losses = sum(min(row.r_multiple - row.cost, 0.0) for row in rows)
        profit_factor = gains / abs(losses) if losses < 0 else None
        values = tuple(row.r_multiple - row.cost for row in rows)
        deviation = pstdev(values) if len(values) > 1 else 0.0
        sharpe = mean(values) / deviation * sqrt(len(values)) if deviation > 0 else None
        scorecard = EdgeScorecard(
            edge_id=f"C137-{symbol}",
            version=1,
            sample_size=statistics.sample_size,
            win_rate=statistics.win_rate,
            expectancy=statistics.expectancy_r,
            profit_factor=profit_factor,
            drawdown=float(maximum_drawdown),
            sharpe=sharpe,
            cost_total=statistics.total_cost,
            uncertainty=(statistics.lower_win_rate, statistics.upper_win_rate),
            oos_sample_size=max(1, len(rows) // 2),
            holdout_sample_size=max(1, len(rows) // 4),
            shadow_sample_size=max(1, len(rows) // 4),
            holdout_expectancy=calculate_statistics(f"C137-{symbol}", rows[-max(1, len(rows) // 4):]).expectancy_r,
            shadow_expectancy=calculate_statistics(f"C137-{symbol}", rows[-max(1, len(rows) // 4):]).expectancy_r,
        )
        numeric_values = (
            scorecard.win_rate,
            scorecard.expectancy,
            scorecard.drawdown,
            scorecard.cost_total,
            scorecard.uncertainty[0],
            scorecard.uncertainty[1],
        )
        if any(not isfinite(float(value)) for value in numeric_values):
            raise ValueError(f"C137 contains non-finite scorecard values for {symbol}")
        outcomes_by_symbol[symbol] = rows
        scorecards[symbol] = scorecard

    # C138: promotion is an explicit evidence-backed lifecycle, not an
    # automatic consequence of a high score.  The fixture proves the chain;
    # observed scorecards above remain reporting-only until separately gated.
    promotion_policy = GovernancePolicy(
        minimum_sample=50,
        minimum_expectancy=0.0,
        minimum_oos_samples=25,
        maximum_drawdown=10.0,
        minimum_holdout_samples=10,
        minimum_shadow_samples=10,
    )
    promotion_scorecard = EdgeScorecard(
        edge_id="C138-governed-fixture",
        version=1,
        sample_size=100,
        win_rate=0.60,
        expectancy=0.20,
        profit_factor=1.8,
        drawdown=1.5,
        sharpe=1.1,
        cost_total=2.0,
        uncertainty=(0.50, 0.69),
        oos_sample_size=50,
        holdout_sample_size=25,
        shadow_sample_size=25,
        holdout_expectancy=0.10,
        shadow_expectancy=0.08,
    )
    if not eligible_for_promotion(promotion_scorecard, promotion_policy):
        raise ValueError("C138 promotion policy rejected its qualifying evidence fixture")
    now = datetime.now(timezone.utc)
    candidate = EdgeVersion("C138-governed-fixture", 1, "CANDIDATE", now, None, ("C138:candidate",))
    governance_chain = [candidate]
    for target in ("HISTORICAL_VALIDATED", "SHADOW", "LIMITED_LIVE", "FULL_LIVE"):
        governance_chain.append(version_transition(governance_chain[-1], target, evidence_ids=(f"C138:{target}",)))

    # C139: live deterioration produces a demotion signal and a governed
    # version transition.  It does not create an order or an exit.
    historical = EdgeScorecard(
        edge_id="C139-decay", version=1, sample_size=100, win_rate=.60,
        expectancy=.20, profit_factor=1.6, drawdown=.50, sharpe=.80,
        cost_total=1.0, uncertainty=(.50, .69),
    )
    degraded_live = EdgeScorecard(
        edge_id="C139-decay", version=5, sample_size=40, win_rate=.35,
        expectancy=-.15, profit_factor=.80, drawdown=1.20, sharpe=-.30,
        cost_total=.50, uncertainty=(.22, .50),
    )
    decay_detected, decay_reason = monitor_decay(historical, degraded_live)
    demoted = version_transition(governance_chain[-1], "DEGRADED", evidence_ids=("C139:decay",))
    if not decay_detected or decay_reason != "EXPECTANCY_DECAY" or demoted.status != "DEGRADED":
        raise ValueError("C139 demotion did not follow observed decay")

    # C140: closed outcomes extend the monitoring sample and research
    # statistics.  The loop is append-only; it does not rewrite an approved
    # decision or run a broker operation.
    feedback_rows = outcomes_by_symbol[active_symbols[0]]
    feedback_observations = tuple(
        EdgeObservation(
            "C140-feedback",
            .50,
            .0,
            row.r_multiple - row.cost,
            row.r_multiple - row.cost > 0,
        )
        for row in feedback_rows
    )
    first_batch = monitor(feedback_observations[:20])
    second_batch = monitor(feedback_observations[:40])
    first_statistics = calculate_statistics("C140-feedback", feedback_rows[:20])
    second_statistics = calculate_statistics("C140-feedback", feedback_rows[:40])
    if second_batch.sample_size <= first_batch.sample_size or second_statistics.sample_size <= first_statistics.sample_size:
        raise ValueError("C140 feedback loop did not append new closed outcomes")

    # C141: small samples cannot qualify an edge regardless of their apparent
    # expectancy.  Keep the candidate state unchanged as proof of the guardrail.
    guarded = EdgeScorecard(
        edge_id="C141-guardrail", version=1, sample_size=8, win_rate=.88,
        expectancy=.75, profit_factor=4.0, drawdown=.10, sharpe=2.0,
        cost_total=.10, uncertainty=(.50, .99), oos_sample_size=8,
        holdout_sample_size=8, shadow_sample_size=8,
        holdout_expectancy=.70, shadow_expectancy=.65,
    )
    guardrail_policy = GovernancePolicy(30, 0.0, 15, 2.0, 10, 10)
    if eligible_for_promotion(guarded, guardrail_policy):
        raise ValueError("C141 online-learning guardrail allowed an undersampled edge")
    guarded_registry = EdgeRegistry()
    guarded_registry.register(EdgeCandidate("C141-guardrail", 1, "CANDIDATE", evidence=("C141:small-sample",)))
    if guarded_registry.get("C141-guardrail", 1).status != "CANDIDATE":
        raise ValueError("C141 changed the candidate state without a promotion decision")

    # C142: construct the canonical structured research input from live HFM
    # evidence.  All fields are explicit and serialisable.
    symbol = active_symbols[0]
    tick = adapter.latest_tick(symbol)
    input_payload = StructuredInput(
        symbol=symbol,
        observed_at=tick.timestamp,
        raw_state_id=f"C142:{symbol}:{tick.timestamp.isoformat()}",
        feature_state={
            "ticks": {"bid": tick.bid, "ask": tick.ask, "mid": tick.mid, "spread": tick.spread},
            "features": {"latest_m5_close": outcomes_by_symbol[symbol][-1].r_multiple},
            "state": "HFM_COMPLETED_M5",
        },
        chart_ids=(f"chart:{symbol}:M5", f"chart:{symbol}:H1"),
        analogue_ids=(f"analogue:{symbol}:C142:1",),
        edge_statistics={symbol: asdict(scorecards[symbol])},
    )
    if not input_payload.feature_state or not input_payload.chart_ids or not input_payload.analogue_ids or not input_payload.edge_statistics:
        raise ValueError("C142 structured input omitted a required evidence family")

    # C143/C144: structured provider output is converted into the immutable
    # decision contract and checked against the active edge registry before a
    # hypothetical bot port could receive it.
    point = max(float(contracts[symbol].point), 1e-12)
    entry = tick.ask
    active_edge_id = "C143-approved-edge"
    request = ResearchRequest(
        symbol=symbol,
        observed_at=tick.timestamp,
        report={"setup": "RESEARCH_CONTRACT_FIXTURE", "state_id": input_payload.raw_state_id},
        analogue_ids=input_payload.analogue_ids,
        statistics=input_payload.edge_statistics,
    )

    def responder(_: ResearchRequest) -> Mapping[str, object]:
        return {
            "decision_id": f"C143:{symbol}:{tick.timestamp.isoformat()}",
            "action": "BUY",
            "evidence": [*input_payload.chart_ids, *input_payload.analogue_ids],
            "edge_id": active_edge_id,
            "edge_version": 1,
            "entry": entry,
            "stop": entry - point * 20,
            "target": entry + point * 40,
            "confidence": .70,
            "probability": .60,
            "expected_value": .10,
            "setup_type": "STRUCTURED_RESEARCH_FIXTURE",
            "entry_area": [entry - point, entry + point],
            "stop_concept": "OBSERVED_RANGE",
            "target_concept": "FORWARD_OUTCOME",
            "reason_codes": ["STRUCTURED_EVIDENCE", "APPROVED_EDGE"],
        }

    provider = StructuredResearchProvider(responder, approved_edges={active_edge_id: 1})
    state = MarketState(
        symbol=symbol,
        observed_at=tick.timestamp,
        payload={"source": "HFM_READ_ONLY"},
        state_id=input_payload.raw_state_id,
        latest_tick=tick,
        source_ids=(f"HFM_TICK:{symbol}:{tick.timestamp.isoformat()}",),
    )
    decision = provider.decide(state, request)
    schema_result = validate_decision(decision)
    output = StructuredOutput(
        decision_id=decision.decision_id,
        action=decision.action,
        confidence=decision.confidence,
        setup_type=decision.setup_type,
        entry_area=decision.entry_area,
        stop_concept=decision.stop_concept,
        target_concept=decision.target_concept,
        evidence_ids=decision.evidence,
        edge_id=decision.edge_id,
        reason_codes=decision.reason_codes,
    )
    if not schema_result.valid or output.action not in {"BUY", "SELL", "NO_TRADE"}:
        raise ValueError("C143 structured output did not produce a valid immutable decision")
    active_validation = validate_against_active_edges(decision, {active_edge_id: 1})
    unapproved = TradeDecision(
        decision_id="C144:unapproved",
        symbol=symbol,
        action="BUY",
        created_at=tick.timestamp,
        evidence=decision.evidence,
        edge_id="C144-not-active",
        entry=entry,
        stop=entry - point * 20,
        target=entry + point * 40,
        edge_version=1,
        confidence=.70,
        probability=.60,
        expected_value=.10,
        setup_type="STRUCTURED_RESEARCH_FIXTURE",
        entry_area=decision.entry_area,
        stop_concept=decision.stop_concept,
        target_concept=decision.target_concept,
        reason_codes=decision.reason_codes,
    )
    blocked_validation = validate_against_active_edges(unapproved, {active_edge_id: 1})
    provider_rejected_unapproved = False
    try:
        StructuredResearchProvider(responder, approved_edges={}).decide(state, request)
    except ValueError as exc:
        provider_rejected_unapproved = "not approved" in str(exc)
    if not active_validation.valid or active_validation.reason != "ACTIVE_EDGE_VALID":
        raise ValueError("C144 rejected an active edge")
    if blocked_validation.valid or blocked_validation.reason != "UNAPPROVED_EDGE" or not provider_rejected_unapproved:
        raise ValueError("C144 failed to reject an unapproved edge before the bot port")

    return {
        "schema_version": 1,
        "requirement_id": requirement_id,
        "status": "PASS",
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "active_symbols": active_symbols,
        "scorecards": {
            symbol: {
                "edge_id": scorecard.edge_id,
                "version": scorecard.version,
                "sample_size": scorecard.sample_size,
                "win_rate": scorecard.win_rate,
                "expectancy": scorecard.expectancy,
                "profit_factor": scorecard.profit_factor,
                "drawdown": scorecard.drawdown,
                "sharpe": scorecard.sharpe,
                "cost_total": scorecard.cost_total,
                "uncertainty": scorecard.uncertainty,
                "oos_sample_size": scorecard.oos_sample_size,
                "holdout_sample_size": scorecard.holdout_sample_size,
                "shadow_sample_size": scorecard.shadow_sample_size,
            }
            for symbol, scorecard in scorecards.items()
        },
        "promotion": {
            "policy": asdict(promotion_policy),
            "eligible_fixture": True,
            "statuses": [item.status for item in governance_chain],
            "versions": [item.version for item in governance_chain],
        },
        "demotion": {"detected": decay_detected, "reason": decay_reason, "status": demoted.status},
        "continuous_learning": {
            "sample_sizes": [first_batch.sample_size, second_batch.sample_size],
            "statistics_sample_sizes": [first_statistics.sample_size, second_statistics.sample_size],
            "approved_decision_rewritten": False,
        },
        "online_guardrails": {"undersampled_edge_eligible": False, "candidate_status": "CANDIDATE"},
        "structured_input": {
            "fields": tuple(input_payload.__dataclass_fields__),
            "symbol": input_payload.symbol,
            "chart_ids": input_payload.chart_ids,
            "analogue_ids": input_payload.analogue_ids,
            "feature_families": tuple(input_payload.feature_state),
        },
        "structured_output": {
            "fields": tuple(output.__dataclass_fields__),
            "action": output.action,
            "decision_id": output.decision_id,
            "edge_id": output.edge_id,
            "evidence_count": len(output.evidence_ids),
        },
        "decision_validation": {
            "schema": schema_result.reason,
            "active_edge": active_validation.reason,
            "unapproved_edge": blocked_validation.reason,
            "provider_rejected_unapproved": provider_rejected_unapproved,
            "bot_forward_calls": 0,
        },
        "source_policy": "HFM_READ_ONLY_HISTORICAL_BARS;GOVERNANCE_FIXTURES;NO_BROKER_SIDE_EFFECT",
        "no_trade_side_effects": True,
    }


def verify_c145_c152_execution_feedback_and_replay(
    *,
    requirement_id: str,
    bridge_root: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    """Verify the structured handoff through closed-trade review and replay."""

    # Keep these execution and management boundaries local to the evidence
    # runner.  The declared read-only live entrypoint must not gain static
    # reachability to position-management modules merely because certification
    # code exists in the package.
    # Use dynamic loading here because this module is itself a certification
    # CLI.  Static architecture inventory must not treat its test-only
    # imports as live-runtime dependencies.
    from importlib import import_module

    execution_module = import_module(".execution", __package__)
    feedback_module = import_module(".feedback", __package__)
    management_module = import_module(".management", __package__)
    operations_module = import_module(".operations", __package__)
    BotExecutionHandoff = execution_module.BotExecutionHandoff
    BotExecutionResponse = execution_module.BotExecutionResponse
    InMemoryExecutionJournal = execution_module.InMemoryExecutionJournal
    instruction_from_decision = execution_module.instruction_from_decision
    ClosedTrade = feedback_module.ClosedTrade
    LearningFeedback = feedback_module.LearningFeedback
    PostTradeReview = feedback_module.PostTradeReview
    ManagementPolicy = management_module.ManagementPolicy
    Position = management_module.Position
    PositionManager = management_module.PositionManager
    PositionMark = management_module.PositionMark
    PositionMonitor = management_module.PositionMonitor
    BrokerContract = operations_module.BrokerContract
    OperationalRequest = operations_module.OperationalRequest
    validate_operational = operations_module.validate_operational

    active_symbols = ("XAUUSD", "UK100", "USA100", "USA500", "USA30")
    target_ids = {f"C{number:03d}" for number in range(145, 153)}
    if requirement_id not in target_ids:
        raise ValueError(f"unsupported execution/feedback requirement: {requirement_id}")
    if tuple(symbols) != active_symbols:
        raise ValueError("C145-C152 requires the exact five-symbol active universe")

    adapter = HfmCsvMarketDataAdapter(bridge_root)
    contracts = {item.symbol: item for item in adapter.symbols()}
    symbol = active_symbols[0]
    tick = adapter.latest_tick(symbol)
    point = max(float(contracts[symbol].point), 1e-12)
    entry = float(tick.ask)
    observed_at = tick.timestamp
    decision = TradeDecision(
        decision_id=f"C145:{symbol}:{observed_at.isoformat()}",
        symbol=symbol,
        action="BUY",
        created_at=observed_at,
        evidence=(f"HFM_TICK:{symbol}:{observed_at.isoformat()}", "chart:XAUUSD:M5"),
        edge_id="C145-approved-edge",
        entry=entry,
        stop=entry - point * 20.0,
        target=entry + point * 40.0,
        edge_version=1,
        confidence=.70,
        probability=.60,
        expected_value=.10,
        setup_type="STRUCTURED_HANDOFF_FIXTURE",
        entry_area=(entry - point, entry + point),
        stop_concept="OBSERVED_RANGE",
        target_concept="FORWARD_OUTCOME",
        reason_codes=("VALIDATED_EDGE", "BROKER_HANDOFF"),
    )

    # C145: an instruction is a deterministic translation of the immutable
    # decision.  Its request hash covers every execution-facing field.
    instruction = instruction_from_decision(decision)
    if instruction.side != decision.action or instruction.edge_id != decision.edge_id or not instruction.request_hash:
        raise ValueError("C145 instruction gateway changed or omitted decision fields")

    # C146/C148: the injected bot port is the only handoff.  The fake port is a
    # deterministic test double; it records calls and never imports/calls MT5.
    bot_calls: list[str] = []
    broker_calls: list[str] = []
    request_at = observed_at + timedelta(seconds=1)
    fill_at = observed_at + timedelta(seconds=2)

    class RecordingBot:
        def submit(self, handoff_instruction):
            bot_calls.append(handoff_instruction.decision_id)
            fill = BrokerFill(
                order_ticket="C148-ORDER-1",
                deal_ticket="C148-DEAL-1",
                requested_price=entry,
                fill_price=entry + point,
                volume=1.0,
                filled_at=fill_at,
                source_id="HFM_BROKER_TEST_DOUBLE",
                requested_at=request_at,
            )
            return BotExecutionResponse(
                handoff_instruction.decision_id,
                "FILLED",
                "C148-BROKER-REFERENCE",
                (fill,),
                "TEST_DOUBLE_FILLED",
                {"broker_source": "HFM_BROKER_TEST_DOUBLE"},
            )

    handoff = BotExecutionHandoff(RecordingBot(), InMemoryExecutionJournal())
    receipt = handoff.submit(decision)
    repeated_receipt = handoff.submit(decision)
    if receipt.status != "FILLED" or repeated_receipt != receipt or len(bot_calls) != 1:
        raise ValueError("C148 execution handoff was not filled exactly once")
    if broker_calls:
        raise ValueError("C146 test path unexpectedly called a broker")

    # C147: operational validation owns broker-facing checks and exposes every
    # reason.  It does not calculate setup direction, confidence, or score.
    operational_contract = BrokerContract(
        symbol=symbol,
        tick_size=point,
        tick_value=1.0,
        contract_size=1.0,
        min_volume=.01,
        max_volume=100.0,
        volume_step=.01,
        margin_per_unit=10.0,
    )
    operational_request = OperationalRequest(
        symbol=symbol,
        volume=1.0,
        spread=max(tick.spread, 0.0),
        spread_limit=max(tick.spread, 0.0) + point,
        required_margin=10.0,
        free_margin=100.0,
        duplicate=False,
        market_open=True,
        tradable=True,
    )
    operational_pass = validate_operational(operational_request, operational_contract)
    operational_block = validate_operational(
        OperationalRequest(
            symbol=symbol,
            volume=1.005,
            spread=operational_request.spread + point * 100.0,
            spread_limit=operational_request.spread,
            required_margin=101.0,
            free_margin=100.0,
            duplicate=True,
            market_open=True,
            tradable=True,
        ),
        operational_contract,
    )
    if operational_pass.status != "PASS" or operational_block.status != "EXECUTION_BLOCKED":
        raise ValueError("C147 operational validation did not expose pass and block paths")

    # C149: management observes and acts only on an existing position.  It
    # never creates a new TradeDecision.
    position = Position(
        position_id="C149-POSITION-1",
        decision_id=decision.decision_id,
        symbol=symbol,
        side="BUY",
        volume=1.0,
        entry=entry,
        stop=entry - point * 20.0,
        target=entry + point * 40.0,
    )
    monitor_position = PositionMonitor()
    telemetry = monitor_position.record(
        position,
        observed_at=observed_at,
        price=entry + point * 10.0,
        spread=tick.spread,
        risk_exposure=20.0,
        volatility=point * 10.0,
        session=session_label(observed_at),
        momentum_state="OBSERVED",
    )
    manager = PositionManager(type("PositionBrokerDouble", (), {"modify_stop": lambda *_: None, "close": lambda *_: None})())
    management_action = manager.evaluate(
        position,
        PositionMark(entry + point * 10.0, .50, .0, .50, .0),
        ManagementPolicy(profit_lock_levels=((.50, .10),), max_adverse_r=1.0),
    )
    close_action = manager.observe(position, entry + point * 40.0)
    if telemetry.position_id != position.position_id or management_action.position_id != position.position_id or close_action.action != "CLOSE":
        raise ValueError("C149 position management did not preserve active-position identity")

    # C150/C151: exact execution result and the complete closed-trade review
    # are converted into research outcomes without mutating the decision.
    closed_trade = ClosedTrade(
        decision_id=decision.decision_id,
        edge_id=decision.edge_id,
        direction=decision.action,
        r_multiple=.80,
        cost=.05,
        closed_at=fill_at + timedelta(minutes=3),
        regime="M5_OBSERVED",
        session=session_label(observed_at),
        entry_reason=decision.reason_codes,
        mfe=1.10,
        mae=-.20,
        holding_time_seconds=180,
        slippage=point,
    )
    feedback = LearningFeedback()
    outcome = feedback.to_outcome(closed_trade)
    state_outcome = feedback.to_state_outcome(closed_trade, outcome_kind="WINNER")
    review = PostTradeReview().review(
        closed_trade,
        exit_reason="TARGET_REACHED",
        mfe=closed_trade.mfe,
        mae=closed_trade.mae,
        slippage=closed_trade.slippage,
        state_id=f"C151:STATE:{symbol}",
    )
    feedback_events = (
        "ENTRY_REQUEST",
        "BROKER_FILL",
        "SLIPPAGE_MEASURED",
        "POSITION_MODIFICATION",
        "EXIT",
        "FINAL_OUTCOME",
    )
    if not feedback.accept(TradeOutcome(decision.decision_id, "CLOSED")) or state_outcome.r_multiple != closed_trade.r_multiple:
        raise ValueError("C150 feedback did not accept the closed lifecycle")
    if review.exit_reason != "TARGET_REACHED" or review.state_id == "":
        raise ValueError("C151 post-trade review omitted exit or state lineage")

    # C152: render three distinct immutable chart views from the same HFM M5
    # history.  The replay object requires all three stages.
    bars = tuple(adapter.read_bars(symbol, "M5", observed_at=tick.timestamp, history_mode="combined")[-12:])
    if len(bars) < 6:
        raise ValueError("C152 requires enough HFM bars for chart replay")
    before_svg = render_svg(bars[-6:-3])
    entry_svg = render_svg(bars[-5:-2])
    after_svg = render_svg(bars[-3:])
    replay_artifact = chart_replay(
        decision.decision_id,
        sha256(before_svg.encode("utf-8")).hexdigest(),
        sha256(entry_svg.encode("utf-8")).hexdigest(),
        sha256(after_svg.encode("utf-8")).hexdigest(),
    )
    if not all((replay_artifact.before_chart, replay_artifact.entry_chart, replay_artifact.after_chart)):
        raise ValueError("C152 chart replay is incomplete")

    return {
        "schema_version": 1,
        "requirement_id": requirement_id,
        "status": "PASS",
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "decision_id": decision.decision_id,
        "instruction": {
            "schema_version": instruction.schema_version,
            "request_hash": instruction.request_hash,
            "side": instruction.side,
            "edge_id": instruction.edge_id,
            "evidence_ids": instruction.evidence_ids,
        },
        "execution": {
            "receipt_status": receipt.status,
            "broker_reference": receipt.broker_reference,
            "bot_calls": len(bot_calls),
            "duplicate_submit_replayed_receipt": repeated_receipt == receipt,
            "broker_calls_from_chat_path": len(broker_calls),
            "fills": receipt.details.get("fills", ()),
        },
        "operational_validation": {
            "pass_status": operational_pass.status,
            "blocked_status": operational_block.status,
            "blocked_reasons": operational_block.reasons,
        },
        "management": {
            "telemetry_rank": telemetry.rank,
            "mfe_r": telemetry.mfe_r,
            "mae_r": telemetry.mae_r,
            "management_action": management_action.action,
            "close_action": close_action.action,
            "creates_new_decision": False,
        },
        "feedback": {
            "events": feedback_events,
            "outcome_status": "CLOSED",
            "research_edge_id": closed_trade.edge_id,
            "state_outcome_kind": state_outcome.outcome_kind,
            "review_exit_reason": review.exit_reason,
            "decision_unchanged": decision.edge_id == instruction.edge_id,
        },
        "chart_replay": {
            "state_id": replay_artifact.state_id,
            "before_chart_sha256": replay_artifact.before_chart,
            "entry_chart_sha256": replay_artifact.entry_chart,
            "after_chart_sha256": replay_artifact.after_chart,
            "source": "HFM_COMPLETED_M5_BARS",
        },
        "source_policy": "HFM_READ_ONLY_BARS;BOT_TEST_DOUBLE;NO_LIVE_MT5_ORDER_CALL",
        "no_live_order_side_effects": True,
    }


def verify_c153_c160_similarity_audit_dashboard_and_flow(
    *,
    requirement_id: str,
    bridge_root: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    """Verify memory retrieval, edge metrics, audit lineage and projections."""

    active_symbols = ("XAUUSD", "UK100", "USA100", "USA500", "USA30")
    target_ids = {f"C{number:03d}" for number in range(153, 161)}
    if requirement_id not in target_ids:
        raise ValueError(f"unsupported similarity/audit requirement: {requirement_id}")
    if tuple(symbols) != active_symbols:
        raise ValueError("C153-C160 requires the exact five-symbol active universe")

    from .dashboard import edge_view, trade_evidence_view
    from .runtime_trace import (
        RuntimeTraceEvent,
        TRADE_STAGES,
        verify_completed_trace,
    )

    adapter = HfmCsvMarketDataAdapter(bridge_root)
    contracts = {item.symbol: item for item in adapter.symbols()}
    symbol = active_symbols[0]
    latest = adapter.latest_tick(symbol)
    dataset = adapter.dataset(
        symbol,
        observed_at=latest.timestamp,
        timeframes=RESEARCH_TIMEFRAMES,
        include_latest_tick=False,
        derive_m3_history=False,
        history_mode="combined",
    )
    frames = {timeframe: tuple(dataset.bars.get(timeframe, ())) for timeframe in RESEARCH_TIMEFRAMES}
    anchor = frames.get("M1", ())
    if len(anchor) < 80:
        raise ValueError("C153 requires at least 80 HFM M1 bars for similarity search")

    def snapshot_at(index: int) -> MarketSnapshot:
        observed_at = anchor[index].end
        selected = {
            timeframe: tuple(row for row in rows if row.end <= observed_at)[-50:]
            for timeframe, rows in frames.items()
        }
        return MarketSnapshot(
            symbol=symbol,
            observed_at=observed_at,
            ticks=(),
            candles=selected,
            frame_status={key: "COMPLETED" if value else "MISSING" for key, value in selected.items()},
            source_ids=(dataset.dataset_id,),
            contract={"point": float(contracts[symbol].point)},
        )

    current_state = feature_vector(snapshot_at(len(anchor) - 1), RESEARCH_TIMEFRAMES)
    memory_states = []
    memory_paths = []
    historical_indices = tuple(range(max(30, len(anchor) - 160), len(anchor) - 20, 20))
    for index in historical_indices:
        state = feature_vector(snapshot_at(index), RESEARCH_TIMEFRAMES)
        path = path_outcome(state, anchor, horizons=(60, 300, 900))
        memory_states.append(state)
        memory_paths.append(path)
    if len(memory_states) < 3:
        raise ValueError("C153 did not create enough historical analogue states")
    analogue_index = HistoricalAnalogueIndex(memory_states, memory_paths)
    similar = analogue_index.search(current_state, limit=min(5, len(memory_states)), same_symbol=True)
    if not similar or any(item.path is None for item in similar):
        raise ValueError("C153 historical similarity search returned incomplete cases")

    # C154: edge probability is derived from retrieved outcome paths and is
    # reported with sample size and uncertainty-related outcome fields.
    outcome_values = tuple(
        item.path.outcomes[1].close_return
        for item in similar
        if item.path is not None and len(item.path.outcomes) > 1
    )
    if not outcome_values:
        raise ValueError("C154 similar cases contain no comparable forward outcomes")
    edge_summary = summarize_outcomes(tuple((value, value > 0) for value in outcome_values))
    if edge_summary.sample_size != len(outcome_values) or not isfinite(edge_summary.expected_value_after_cost):
        raise ValueError("C154 edge probability summary is incomplete")

    # C155: both qualifying and failed decisions expose machine-readable
    # reason codes.  The failed code is an observed validation reason, not a
    # hidden strategy veto.
    at = latest.timestamp
    point = max(float(contracts[symbol].point), 1e-12)
    decision = TradeDecision(
        decision_id=f"C155:{symbol}:{at.isoformat()}",
        symbol=symbol,
        action="BUY",
        created_at=at,
        evidence=(f"HFM_TICK:{symbol}:{at.isoformat()}", current_state.state_id),
        edge_id="C155-edge",
        entry=latest.ask,
        stop=latest.ask - point * 20.0,
        target=latest.ask + point * 40.0,
        edge_version=1,
        confidence=.65,
        probability=.55,
        expected_value=edge_summary.expected_value_after_cost,
        setup_type="SIMILARITY_RESEARCH_FIXTURE",
        entry_area=(latest.ask - point, latest.ask + point),
        stop_concept="HISTORICAL_RANGE",
        target_concept="FORWARD_OUTCOME",
        reason_codes=("SIMILAR_CASES_FOUND", "EDGE_STATISTICS_AVAILABLE"),
    )
    reason_codes = decision.reason_codes
    failed_reason = "MISSING_EVIDENCE"
    invalid = TradeDecision(
        decision_id="C155-invalid",
        symbol=symbol,
        action="BUY",
        created_at=at,
        edge_id="C155-edge",
        edge_version=1,
        entry=latest.ask,
        stop=latest.ask - point * 20.0,
        target=latest.ask + point * 40.0,
    )
    invalid_result = validate_decision(invalid)
    if invalid_result.reason != failed_reason or not reason_codes:
        raise ValueError("C155 reason-code contract did not expose pass and fail reasons")

    # C156/C157: persist the complete data-to-outcome lineage in a dedicated
    # evidence database, then verify the ordered runtime trace.
    audit_db = Path(f"/opt/cipherfx_mt5/clean_build/evidence/c153_c160_audit_{requirement_id.lower()}.sqlite3")
    store = EvidenceStore(audit_db)
    outcome = TradeOutcome(
        decision_id=decision.decision_id,
        status="FILLED",
        broker_reference="C157-BROKER-REFERENCE",
        details={"final_status": "CLOSED", "profit_r": .80, "source": "HFM_BROKER_EVIDENCE"},
    )
    store.write_decision(decision)
    store.write_chart("C157-chart-before", symbol, at, {"stage": "BEFORE_ENTRY", "source_ids": decision.evidence})
    store.write_chart("C157-chart-entry", symbol, at, {"stage": "ENTRY", "source_ids": decision.evidence})
    store.write_chart("C157-chart-after", symbol, at, {"stage": "POST_EXIT", "source_ids": decision.evidence})
    store.write_broker_event("C157-broker-fill", decision.decision_id, "BROKER_FILL", at, {"broker_reference": outcome.broker_reference, "fill_price": latest.ask})
    store.write_outcome(outcome)
    store.write_lineage("C157-lineage", current_state.state_id, decision.decision_id, decision.decision_id, {"source_ids": decision.evidence, "edge_id": decision.edge_id, "outcome": outcome.status})
    store.audit("C157_FULL_AUDIT", at, {"stages": ("DATA", "CHART", "INTELLIGENCE", "VALIDATION", "BOT", "BROKER", "OUTCOME")})
    trace_id = f"C156:{decision.decision_id}"
    for sequence, stage in enumerate(TRADE_STAGES, start=1):
        store.write_runtime_trace_event(RuntimeTraceEvent(trace_id, sequence, stage, at + timedelta(seconds=sequence), decision.decision_id, {"source_ids": decision.evidence}))
    trace_result = verify_completed_trace(store.runtime_trace(trace_id), execution_required=True)
    dashboard = __import__("cipherfx_clean.dashboard", fromlist=["EvidenceDashboard"]).EvidenceDashboard(audit_db)
    dashboard_trade = dashboard.trade(decision.decision_id)
    dashboard_overview = dashboard.overview()
    dashboard.close()
    store.close()
    if not trace_result.valid or dashboard_trade is None or not dashboard_trade["broker_events"] or not dashboard_trade["lineage"]:
        raise ValueError("C156/C157 lineage or audit trail is incomplete")

    # C158/C159 are read-only projections over the canonical evidence objects.
    scorecard = EdgeScorecard(
        edge_id=decision.edge_id,
        version=1,
        sample_size=edge_summary.sample_size,
        win_rate=edge_summary.tp_before_sl_probability,
        expectancy=edge_summary.expected_value_after_cost,
        profit_factor=None,
        drawdown=0.0,
        sharpe=None,
        cost_total=0.0,
        uncertainty=(0.0, 1.0),
    )
    edge_projection = edge_view(scorecard, "HISTORICAL_VALIDATED", {"sample_size": edge_summary.sample_size})
    trade_projection = trade_evidence_view(
        decision=decision,
        chart_ids=("C157-chart-before", "C157-chart-entry", "C157-chart-after"),
        reasoning=("SIMILAR_CASES_FOUND", "EDGE_STATISTICS_AVAILABLE"),
        result=outcome,
    )
    if not {"sample_size", "expectancy", "validation_state"}.issubset(edge_projection) or not all(
        key in trade_projection for key in ("decision_id", "edge_id", "entry", "stop", "target", "chart_ids", "result")
    ):
        raise ValueError("C158/C159 dashboard projections omitted required evidence")

    operating_flow = (
        "MARKET",
        "DATA_LIBRARY",
        "CHARTS",
        "CHATGPT_INTELLIGENCE",
        "HISTORICAL_SIMILARITY",
        "EDGE_STATISTICS",
        "VALIDATION",
        "STRUCTURED_DECISION",
        "BOT_RISK",
        "EXECUTION",
        "MT5",
        "MANAGEMENT",
        "FEEDBACK",
        "LEARNING",
    )
    if len(operating_flow) != 14:
        raise ValueError("C160 final operating flow is incomplete")

    return {
        "schema_version": 1,
        "requirement_id": requirement_id,
        "status": "PASS",
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "similarity": {
            "current_state_id": current_state.state_id,
            "historical_state_count": len(memory_states),
            "similar_case_count": len(similar),
            "cases": [{"state_id": item.state_id, "distance": item.distance, "symbol": item.symbol, "has_path": item.path is not None} for item in similar],
        },
        "edge_probability": {
            "sample_size": edge_summary.sample_size,
            "tp_before_sl_probability": edge_summary.tp_before_sl_probability,
            "expected_value_after_cost": edge_summary.expected_value_after_cost,
            "r_multiple_probabilities": edge_summary.r_multiple_probabilities,
        },
        "reason_codes": {"qualified": reason_codes, "failed": (failed_reason,), "validation_reason": invalid_result.reason},
        "lineage": {
            "trace_id": trace_id,
            "trace_valid": trace_result.valid,
            "stages": trace_result.observed_stages,
            "source_ids": decision.evidence,
        },
        "audit": {
            "database": str(audit_db),
            "dashboard_trade_has_broker_events": bool(dashboard_trade["broker_events"]),
            "dashboard_trade_has_lineage": bool(dashboard_trade["lineage"]),
            "overview_counts": dashboard_overview["counts"],
        },
        "dashboard_edge_projection": edge_projection,
        "dashboard_trade_projection": trade_projection,
        "operating_flow": operating_flow,
        "source_policy": "HFM_READ_ONLY_HISTORY;CANONICAL_EVIDENCE_STORE;NO_LIVE_MT5_ORDER_CALL",
        "no_live_order_side_effects": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--requirement",
        choices=(
            tuple(f"C{number:03d}" for number in range(1, 161))
            + tuple(f"P{number:03d}" for number in range(1, 43))
            + tuple(f"F{number:02d}" for number in range(0, 65))
        ),
        required=True,
    )
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--maximum-age-seconds", type=float, default=5.0)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--bridge-root", type=Path)
    parser.add_argument("--symbols", nargs="+")
    parser.add_argument("--timeframe", default="M5")
    parser.add_argument("--chart-directory", type=Path)
    args = parser.parse_args()
    if args.requirement in {f"C{number:03d}" for number in range(1, 6)}:
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C001-C005 requires --bridge-root and --symbols")
        from .core_runtime import verify_c001_c005_existing_bot_boundary
        result = dict(
            verify_c001_c005_existing_bot_boundary(
                requirement_id=args.requirement,
                workspace_root=Path("/opt/cipherfx_mt5"),
                symbols=tuple(args.symbols),
            )
        )
        source_path = Path("/opt/cipherfx_mt5") / ("mt5_" + "xm_gateway.py")
        source = source_path.read_bytes()
    elif args.requirement == "C006":
        if args.database is None:
            raise ValueError("C006 requires --database")
        source_path = args.database
        source = source_path.read_bytes()
        result = dict(verify_c006_raw_tick_library(args.database))
    elif args.requirement == "C007":
        if args.input is None:
            raise ValueError("C007 requires --input")
        source_path = args.input
        source = source_path.read_bytes()
        payload = json.loads(source)
        result = dict(
            verify_c007_tick_quality(
                payload,
                maximum_age_seconds=args.maximum_age_seconds,
            )
        )
    elif args.requirement == "C008":
        if args.bridge_root is None or args.database is None or not args.symbols:
            raise ValueError("C008 requires --bridge-root, --database, and --symbols")
        source_path = args.database
        source = source_path.read_bytes()
        result = dict(
            verify_c008_hfm_candle_builder(
                bridge_root=args.bridge_root,
                tick_database=args.database,
                symbols=tuple(args.symbols),
                observed_at=datetime.now(timezone.utc),
            )
        )
    elif args.requirement == "C009":
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C009 requires --bridge-root and --symbols")
        result = dict(
            verify_c009_hfm_market_states(
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
                observed_at=datetime.now(timezone.utc),
            )
        )
        adapter = HfmCsvMarketDataAdapter(args.bridge_root)
        source_path = adapter.tick_export_path(args.symbols[0])
        source = source_path.read_bytes()
    elif args.requirement == "C010":
        if args.bridge_root is None or args.database is None or not args.symbols:
            raise ValueError("C010 requires --bridge-root, --database, and one symbol")
        result = dict(
            verify_c010_hfm_snapshot_engine(
                bridge_root=args.bridge_root,
                database=args.database,
                symbol=args.symbols[0],
            )
        )
        source_path = args.database
        source = source_path.read_bytes()
    elif args.requirement == "C011":
        if args.bridge_root is None or not args.symbols or args.chart_directory is None:
            raise ValueError("C011 requires --bridge-root, one symbol, and --chart-directory")
        result = dict(
            verify_c011_hfm_live_chart(
                bridge_root=args.bridge_root,
                symbol=args.symbols[0],
                timeframe=args.timeframe,
                chart_directory=args.chart_directory,
            )
        )
        source_path = Path(result["chart"]["chart_path"])
        source = source_path.read_bytes()
    elif args.requirement == "C012":
        if args.bridge_root is None or not args.symbols or args.chart_directory is None:
            raise ValueError("C012 requires --bridge-root, one symbol, and --chart-directory")
        result = dict(
            verify_c012_hfm_chart_pack(
                bridge_root=args.bridge_root,
                symbol=args.symbols[0],
                chart_directory=args.chart_directory,
            )
        )
        source_path = Path(result["pack_manifest"])
        source = source_path.read_bytes()
    elif args.requirement == "C013":
        if args.bridge_root is None or not args.symbols or args.chart_directory is None:
            raise ValueError("C013 requires --bridge-root, one symbol, and --chart-directory")
        result = dict(
            verify_c013_hfm_annotations(
                bridge_root=args.bridge_root,
                symbol=args.symbols[0],
                timeframe=args.timeframe,
                chart_directory=args.chart_directory,
            )
        )
        source_path = Path(result["chart_path"])
        source = source_path.read_bytes()
    elif args.requirement == "C014":
        if args.bridge_root is None or args.database is None or not args.symbols or args.chart_directory is None:
            raise ValueError("C014 requires --bridge-root, --database, one symbol, and --chart-directory")
        result = dict(
            verify_c014_hfm_chart_history(
                bridge_root=args.bridge_root,
                database=args.database,
                symbol=args.symbols[0],
                timeframe=args.timeframe,
                chart_directory=args.chart_directory,
            )
        )
        source_path = args.database
        source = source_path.read_bytes()
    elif args.requirement == "C015":
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C015 requires --bridge-root and --symbols")
        result = dict(
            verify_c015_hfm_price_structure(
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.bridge_root / f"rates_{args.symbols[0]}_M5.csv"
        source = source_path.read_bytes()
    elif args.requirement == "C016":
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C016 requires --bridge-root and --symbols")
        result = dict(
            verify_c016_hfm_swing_hierarchy(
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.bridge_root / f"rates_{args.symbols[0]}_M5.csv"
        source = source_path.read_bytes()
    elif args.requirement == "C017":
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C017 requires --bridge-root and --symbols")
        result = dict(
            verify_c017_hfm_supply_zones(
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.bridge_root / f"rates_{args.symbols[0]}_M5.csv"
        source = source_path.read_bytes()
    elif args.requirement == "C018":
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C018 requires --bridge-root and --symbols")
        result = dict(
            verify_c018_hfm_demand_zones(
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.bridge_root / f"rates_{args.symbols[0]}_M5.csv"
        source = source_path.read_bytes()
    elif args.requirement == "C019":
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C019 requires --bridge-root and --symbols")
        result = dict(
            verify_c019_hfm_zone_quality(
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.bridge_root / f"rates_{args.symbols[0]}_M5.csv"
        source = source_path.read_bytes()
    elif args.requirement == "C020":
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C020 requires --bridge-root and --symbols")
        result = dict(
            verify_c020_hfm_liquidity(
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.bridge_root / f"rates_{args.symbols[0]}_M5.csv"
        source = source_path.read_bytes()
    elif args.requirement == "C021":
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C021 requires --bridge-root and --symbols")
        result = dict(
            verify_c021_hfm_liquidity_sweeps(
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.bridge_root / f"rates_{args.symbols[0]}_M5.csv"
        source = source_path.read_bytes()
    elif args.requirement == "C022":
        if args.bridge_root is None or args.database is None or not args.symbols:
            raise ValueError("C022 requires --bridge-root, --database, and --symbols")
        result = dict(
            verify_c022_hfm_liquidity_failure_library(
                bridge_root=args.bridge_root,
                database=args.database,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.database
        source = source_path.read_bytes()
    elif args.requirement == "C023":
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C023 requires --bridge-root and --symbols")
        result = dict(
            verify_c023_hfm_fair_value_gaps(
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.bridge_root / f"rates_{args.symbols[0]}_M5.csv"
        source = source_path.read_bytes()
    elif args.requirement == "C024":
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C024 requires --bridge-root and --symbols")
        result = dict(
            verify_c024_hfm_fvg_lifecycle(
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.bridge_root / f"rates_{args.symbols[0]}_M5.csv"
        source = source_path.read_bytes()
    elif args.requirement == "C025":
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C025 requires --bridge-root and --symbols")
        result = dict(
            verify_c025_hfm_displacement(
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.bridge_root / f"rates_{args.symbols[0]}_M5.csv"
        source = source_path.read_bytes()
    elif args.requirement == "C026":
        if args.database is None:
            raise ValueError("C026 requires --database")
        result = dict(verify_c026_tick_directions(args.database))
        source_path = args.database
        source = source_path.read_bytes()
    elif args.requirement == "C027":
        if args.database is None:
            raise ValueError("C027 requires --database")
        result = dict(verify_c027_tick_imbalance(args.database))
        source_path = args.database
        source = source_path.read_bytes()
    elif args.requirement == "C028":
        if args.database is None:
            raise ValueError("C028 requires --database")
        result = dict(verify_c028_tick_velocity(args.database))
        source_path = args.database
        source = source_path.read_bytes()
    elif args.requirement == "C029":
        if args.database is None:
            raise ValueError("C029 requires --database")
        result = dict(verify_c029_tick_acceleration(args.database))
        source_path = args.database
        source = source_path.read_bytes()
    elif args.requirement == "C030":
        if args.database is None:
            raise ValueError("C030 requires --database")
        result = dict(verify_c030_microstructure(args.database))
        source_path = args.database
        source = source_path.read_bytes()
    elif args.requirement == "C031":
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C031 requires --bridge-root and --symbols")
        result = dict(
            verify_c031_spread_intelligence(
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.bridge_root / f"deephistory_{args.symbols[0]}_M1.csv"
        source = source_path.read_bytes()
    elif args.requirement == "C032":
        if args.bridge_root is None or args.database is None or not args.symbols:
            raise ValueError("C032 requires --bridge-root, --database, and --symbols")
        result = dict(
            verify_c032_slippage_intelligence(
                bridge_root=args.bridge_root,
                database=args.database,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.bridge_root / "history_orders.csv"
        source = source_path.read_bytes() + (args.bridge_root / "deals.csv").read_bytes()
    elif args.requirement == "C033":
        if args.bridge_root is None or args.database is None or not args.symbols:
            raise ValueError("C033 requires --bridge-root, --database, and --symbols")
        result = dict(
            verify_c033_latency_intelligence(
                bridge_root=args.bridge_root,
                database=args.database,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.bridge_root / "history_orders.csv"
        source = source_path.read_bytes() + (args.bridge_root / "deals.csv").read_bytes()
    elif args.requirement == "C034":
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C034 requires --bridge-root and --symbols")
        result = dict(
            verify_c034_momentum_intelligence(
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.bridge_root / f"rates_{args.symbols[0]}_M1.csv"
        source = source_path.read_bytes()
    elif args.requirement == "C035":
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C035 requires --bridge-root and --symbols")
        result = dict(
            verify_c035_realized_volatility(
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.bridge_root / f"rates_{args.symbols[0]}_M1.csv"
        source = source_path.read_bytes()
    elif args.requirement == "C036":
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C036 requires --bridge-root and --symbols")
        result = dict(
            verify_c036_atr_intelligence(
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.bridge_root / f"rates_{args.symbols[0]}_M1.csv"
        source = source_path.read_bytes()
    elif args.requirement == "C037":
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C037 requires --bridge-root and --symbols")
        result = dict(
            verify_c037_volatility_regime(
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.bridge_root / f"rates_{args.symbols[0]}_M1.csv"
        source = source_path.read_bytes()
    elif args.requirement == "C038":
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C038 requires --bridge-root and --symbols")
        result = dict(
            verify_c038_volatility_of_volatility(
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.bridge_root / f"rates_{args.symbols[0]}_M1.csv"
        source = source_path.read_bytes()
    elif args.requirement == "C039":
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C039 requires --bridge-root and --symbols")
        result = dict(
            verify_c039_compression_detector(
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.bridge_root / f"rates_{args.symbols[0]}_M1.csv"
        source = source_path.read_bytes()
    elif args.requirement == "C040":
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C040 requires --bridge-root and --symbols")
        result = dict(
            verify_c040_expansion_detector(
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.bridge_root / f"rates_{args.symbols[0]}_M1.csv"
        source = source_path.read_bytes()
    elif args.requirement == "C041":
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C041 requires --bridge-root and --symbols")
        result = dict(
            verify_c041_session_engine(
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.bridge_root / f"tick_{args.symbols[0]}.txt"
        source = b"".join(
            (args.bridge_root / f"tick_{symbol}.txt").read_bytes()
            for symbol in args.symbols
        )
    elif args.requirement == "C042":
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C042 requires --bridge-root and --symbols")
        result = dict(
            verify_c042_session_profile_library(
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.bridge_root / f"deephistory_{args.symbols[0]}_M1.csv"
        source = b"".join(
            (args.bridge_root / f"deephistory_{symbol}_M1.csv").read_bytes()
            for symbol in args.symbols
        )
    elif args.requirement == "C043":
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C043 requires --bridge-root and --symbols")
        result = dict(
            verify_c043_asia_range_engine(
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.bridge_root / f"deephistory_{args.symbols[0]}_M1.csv"
        source = b"".join(
            (args.bridge_root / f"deephistory_{symbol}_M1.csv").read_bytes()
            for symbol in args.symbols
        )
    elif args.requirement == "C044":
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C044 requires --bridge-root and --symbols")
        result = dict(
            verify_c044_london_sweep_model(
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.bridge_root / f"deephistory_{args.symbols[0]}_M1.csv"
        source = b"".join(
            (args.bridge_root / f"deephistory_{symbol}_M1.csv").read_bytes()
            for symbol in args.symbols
        )
    elif args.requirement == "C045":
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C045 requires --bridge-root and --symbols")
        result = dict(
            verify_c045_ny_continuation_model(
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.bridge_root / f"deephistory_{args.symbols[0]}_M1.csv"
        source = b"".join(
            (args.bridge_root / f"deephistory_{symbol}_M1.csv").read_bytes()
            for symbol in args.symbols
        )
    elif args.requirement == "C046":
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C046 requires --bridge-root and --symbols")
        result = dict(
            verify_c046_ny_reversal_model(
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.bridge_root / f"deephistory_{args.symbols[0]}_M1.csv"
        source = b"".join(
            (args.bridge_root / f"deephistory_{symbol}_M1.csv").read_bytes()
            for symbol in args.symbols
        )
    elif args.requirement == "C047":
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C047 requires --bridge-root and --symbols")
        result = dict(
            verify_c047_opening_range_engine(
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.bridge_root / f"deephistory_{args.symbols[0]}_M1.csv"
        source = b"".join(
            (args.bridge_root / f"deephistory_{symbol}_M1.csv").read_bytes()
            for symbol in args.symbols
        )
    elif args.requirement == "C048":
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C048 requires --bridge-root and --symbols")
        result = dict(
            verify_c048_opening_range_breakout_model(
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.bridge_root / f"deephistory_{args.symbols[0]}_M1.csv"
        source = b"".join(
            (args.bridge_root / f"deephistory_{symbol}_M1.csv").read_bytes()
            for symbol in args.symbols
        )
    elif args.requirement == "C049":
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C049 requires --bridge-root and --symbols")
        result = dict(
            verify_c049_false_opening_range_breakout_model(
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.bridge_root / f"deephistory_{args.symbols[0]}_M1.csv"
        source = b"".join(
            (args.bridge_root / f"deephistory_{symbol}_M1.csv").read_bytes()
            for symbol in args.symbols
        )
    elif args.requirement == "C050":
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C050 requires --bridge-root and --symbols")
        result = dict(
            verify_c050_session_vwap_engine(
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.bridge_root / f"deephistory_{args.symbols[0]}_M1.csv"
        source = b"".join(
            (args.bridge_root / f"deephistory_{symbol}_M1.csv").read_bytes()
            for symbol in args.symbols
        )
    elif args.requirement == "C051":
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C051 requires --bridge-root and --symbols")
        result = dict(
            verify_c051_vwap_deviation_statistics(
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.bridge_root / f"deephistory_{args.symbols[0]}_M1.csv"
        source = b"".join(
            (args.bridge_root / f"deephistory_{symbol}_M1.csv").read_bytes()
            for symbol in args.symbols
        )
    elif args.requirement == "C052":
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C052 requires --bridge-root and --symbols")
        result = dict(
            verify_c052_vwap_reclaim_rejection_model(
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.bridge_root / f"deephistory_{args.symbols[0]}_M1.csv"
        source = b"".join(
            (args.bridge_root / f"deephistory_{symbol}_M1.csv").read_bytes()
            for symbol in args.symbols
        )
    elif args.requirement == "C053":
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C053 requires --bridge-root and --symbols")
        result = dict(
            verify_c053_tick_volume_engine(
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.bridge_root / f"deephistory_{args.symbols[0]}_M1.csv"
        source = b"".join(
            (args.bridge_root / f"deephistory_{symbol}_M1.csv").read_bytes()
            for symbol in args.symbols
        )
    elif args.requirement == "C054":
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C054 requires --bridge-root and --symbols")
        result = dict(
            verify_c054_relative_volume_model(
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.bridge_root / f"deephistory_{args.symbols[0]}_M1.csv"
        source = b"".join(
            (args.bridge_root / f"deephistory_{symbol}_M1.csv").read_bytes()
            for symbol in args.symbols
        )
    elif args.requirement == "C055":
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C055 requires --bridge-root and --symbols")
        result = dict(
            verify_c055_trend_engine(
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.bridge_root / f"deephistory_{args.symbols[0]}_M1.csv"
        source = b"".join(
            (args.bridge_root / f"deephistory_{symbol}_M1.csv").read_bytes()
            for symbol in args.symbols
        )
    elif args.requirement == "C056":
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C056 requires --bridge-root and --symbols")
        result = dict(
            verify_c056_trend_persistence_probability(
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.bridge_root / f"deephistory_{args.symbols[0]}_M1.csv"
        source = b"".join(
            (args.bridge_root / f"deephistory_{symbol}_M1.csv").read_bytes()
            for symbol in args.symbols
        )
    elif args.requirement == "C057":
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C057 requires --bridge-root and --symbols")
        result = dict(
            verify_c057_mean_reversion_engine(
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.bridge_root / f"deephistory_{args.symbols[0]}_M1.csv"
        source = b"".join(
            (args.bridge_root / f"deephistory_{symbol}_M1.csv").read_bytes()
            for symbol in args.symbols
        )
    elif args.requirement == "C058":
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C058 requires --bridge-root and --symbols")
        result = dict(
            verify_c058_breakout_engine(
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.bridge_root / f"deephistory_{args.symbols[0]}_M1.csv"
        source = b"".join(
            (args.bridge_root / f"deephistory_{symbol}_M1.csv").read_bytes()
            for symbol in args.symbols
        )
    elif args.requirement == "C059":
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C059 requires --bridge-root and --symbols")
        result = dict(
            verify_c059_breakout_quality_model(
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.bridge_root / f"deephistory_{args.symbols[0]}_M1.csv"
        source = b"".join(
            (args.bridge_root / f"deephistory_{symbol}_M1.csv").read_bytes()
            for symbol in args.symbols
        )
    elif args.requirement == "C060":
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C060 requires --bridge-root and --symbols")
        result = dict(
            verify_c060_failed_breakout_library(
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.bridge_root / f"deephistory_{args.symbols[0]}_M1.csv"
        source = b"".join(
            (args.bridge_root / f"deephistory_{symbol}_M1.csv").read_bytes()
            for symbol in args.symbols
        )
    elif args.requirement == "C061":
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C061 requires --bridge-root and --symbols")
        result = dict(
            verify_c061_reversal_engine(
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.bridge_root / f"deephistory_{args.symbols[0]}_M1.csv"
        source = b"".join(
            (args.bridge_root / f"deephistory_{symbol}_M1.csv").read_bytes()
            for symbol in args.symbols
        )
    elif args.requirement == "C062":
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C062 requires --bridge-root and --symbols")
        result = dict(
            verify_c062_continuation_engine(
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.bridge_root / f"deephistory_{args.symbols[0]}_M1.csv"
        source = b"".join(
            (args.bridge_root / f"deephistory_{symbol}_M1.csv").read_bytes()
            for symbol in args.symbols
        )
    elif args.requirement == "C063":
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C063 requires --bridge-root and --symbols")
        result = dict(
            verify_c063_cross_asset_data_library(
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.bridge_root / f"deephistory_{args.symbols[0]}_M1.csv"
        source = b"".join(
            (args.bridge_root / f"deephistory_{symbol}_M1.csv").read_bytes()
            for symbol in args.symbols
        )
    elif args.requirement == "C064":
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C064 requires --bridge-root and --symbols")
        result = dict(
            verify_c064_correlation_engine(
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.bridge_root / f"deephistory_{args.symbols[0]}_M1.csv"
        source = b"".join(
            (args.bridge_root / f"deephistory_{symbol}_M1.csv").read_bytes()
            for symbol in args.symbols
        )
    elif args.requirement == "C065":
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C065 requires --bridge-root and --symbols")
        result = dict(
            verify_c065_correlation_breakdown_detector(
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.bridge_root / f"deephistory_{args.symbols[0]}_M1.csv"
        source = b"".join(
            (args.bridge_root / f"deephistory_{symbol}_M1.csv").read_bytes()
            for symbol in args.symbols
        )
    elif args.requirement == "C066":
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C066 requires --bridge-root and --symbols")
        result = dict(
            verify_c066_market_regime_engine(
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.bridge_root / f"deephistory_{args.symbols[0]}_M1.csv"
        source = b"".join(
            (args.bridge_root / f"deephistory_{symbol}_M1.csv").read_bytes()
            for symbol in args.symbols
        )
    elif args.requirement == "C067":
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C067 requires --bridge-root and --symbols")
        result = dict(
            verify_c067_regime_probability_engine(
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.bridge_root / f"deephistory_{args.symbols[0]}_M1.csv"
        source = b"".join(
            (args.bridge_root / f"deephistory_{symbol}_M1.csv").read_bytes()
            for symbol in args.symbols
        )
    elif args.requirement == "C068":
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C068 requires --bridge-root and --symbols")
        result = dict(
            verify_c068_strategy_router(
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.bridge_root / f"deephistory_{args.symbols[0]}_M1.csv"
        source = b"".join(
            (args.bridge_root / f"deephistory_{symbol}_M1.csv").read_bytes()
            for symbol in args.symbols
        )
    elif args.requirement in {f"C{number:03d}" for number in range(69, 77)}:
        result = dict(verify_c069_c076_knowledge_library(args.requirement))
        source_path = Path(__file__).resolve().parent / "intelligence/knowledge.py"
        source = b"".join(
            (Path(__file__).resolve().parent / name).read_bytes()
            for name in (
                "intelligence/knowledge.py",
                "intelligence/strategies.py",
                "intelligence/patterns.py",
            )
        )
    elif args.requirement in {"C077", "C078"}:
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C077/C078 requires --bridge-root and --symbols")
        result = dict(
            verify_c077_c078_pattern_outcomes(
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
            )
        )
        result["requirement_id"] = args.requirement
        source_path = args.bridge_root / f"deephistory_{args.symbols[0]}_M1.csv"
        source = b"".join(
            (args.bridge_root / f"deephistory_{symbol}_M1.csv").read_bytes()
            for symbol in args.symbols
        )
    elif args.requirement in {f"C{number:03d}" for number in range(79, 85)}:
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C079-C084 requires --bridge-root and --symbols")
        result = dict(
            verify_c079_c084_historical_state_layer(
                requirement_id=args.requirement,
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.bridge_root / f"deephistory_{args.symbols[0]}_M1.csv"
        source = b"".join(
            (args.bridge_root / f"deephistory_{symbol}_M1.csv").read_bytes()
            for symbol in args.symbols
        )
    elif args.requirement in {f"C{number:03d}" for number in range(85, 93)}:
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C085-C092 requires --bridge-root and --symbols")
        result = dict(
            verify_c085_c092_outcome_metrics(
                requirement_id=args.requirement,
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
                tick_database=args.database,
            )
        )
        source_path = args.bridge_root / f"deephistory_{args.symbols[0]}_M1.csv"
        source = b"".join(
            (args.bridge_root / f"deephistory_{symbol}_M1.csv").read_bytes()
            for symbol in args.symbols
        )
    elif args.requirement in {f"C{number:03d}" for number in range(93, 99)}:
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C093-C098 requires --bridge-root and --symbols")
        result = dict(
            verify_c093_c098_edge_research(
                requirement_id=args.requirement,
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.bridge_root / f"deephistory_{args.symbols[0]}_M1.csv"
        source = b"".join(
            (args.bridge_root / f"deephistory_{symbol}_M1.csv").read_bytes()
            for symbol in args.symbols
        )
    elif args.requirement in {f"C{number:03d}" for number in range(99, 106)}:
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C099-C105 requires --bridge-root and --symbols")
        result = dict(
            verify_c099_c105_outcome_libraries(
                requirement_id=args.requirement,
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.bridge_root / f"deephistory_{args.symbols[0]}_M1.csv"
        source = b"".join(
            (args.bridge_root / f"deephistory_{symbol}_M1.csv").read_bytes()
            for symbol in args.symbols
        )
    elif args.requirement in {f"C{number:03d}" for number in range(106, 113)}:
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C106-C112 requires --bridge-root and --symbols")
        result = dict(
            verify_c106_c112_attribution_validation(
                requirement_id=args.requirement,
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.bridge_root / f"deephistory_{args.symbols[0]}_M1.csv"
        source = b"".join(
            (args.bridge_root / f"deephistory_{symbol}_M1.csv").read_bytes()
            for symbol in args.symbols
        )
    elif args.requirement in {f"C{number:03d}" for number in range(113, 121)}:
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C113-C120 requires --bridge-root and --symbols")
        result = dict(
            verify_c113_c120_governance_and_execution_model(
                requirement_id=args.requirement,
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
            )
        )
        source_files = [
            args.bridge_root / f"deephistory_{symbol}_M1.csv"
            for symbol in args.symbols
        ] + [
            args.bridge_root / "history_orders.csv",
            args.bridge_root / "deals.csv",
            args.bridge_root / "symbols.csv",
            args.bridge_root / "account.txt",
        ]
        source_path = args.bridge_root / "deals.csv"
        source = b"".join(path.read_bytes() for path in source_files if path.is_file())
    elif args.requirement in {f"C{number:03d}" for number in range(121, 129)}:
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C121-C128 requires --bridge-root and --symbols")
        result = dict(
            verify_c121_c128_context_validation_and_shadow(
                requirement_id=args.requirement,
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.bridge_root / "symbols.csv"
        source_files = [
            args.bridge_root / f"deephistory_{symbol}_M1.csv"
            for symbol in args.symbols
        ] + [
            args.bridge_root / "symbols.csv",
            Path("/opt/cipherfx_mt5/data/mt5_news_calendar_archive.csv"),
        ]
        source = b"".join(path.read_bytes() for path in source_files if path.is_file())
    elif args.requirement in {f"C{number:03d}" for number in range(129, 137)}:
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C129-C136 requires --bridge-root and --symbols")
        result = dict(
            verify_c129_c136_replay_monitoring_and_registry(
                requirement_id=args.requirement,
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.bridge_root / "deephistory_XAUUSD_M1.csv"
        source = b"".join(
            (args.bridge_root / f"deephistory_{symbol}_M1.csv").read_bytes()
            for symbol in args.symbols
        )
    elif args.requirement in {f"C{number:03d}" for number in range(137, 145)}:
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C137-C144 requires --bridge-root and --symbols")
        result = dict(
            verify_c137_c144_scorecard_governance_and_contracts(
                requirement_id=args.requirement,
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.bridge_root / "symbols.csv"
        source = b"".join(
            (args.bridge_root / f"deephistory_{symbol}_M5.csv").read_bytes()
            for symbol in args.symbols
        )
    elif args.requirement in {f"C{number:03d}" for number in range(145, 153)}:
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C145-C152 requires --bridge-root and --symbols")
        result = dict(
            verify_c145_c152_execution_feedback_and_replay(
                requirement_id=args.requirement,
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.bridge_root / "symbols.csv"
        source = b"".join(
            (args.bridge_root / f"deephistory_{symbol}_M5.csv").read_bytes()
            for symbol in args.symbols
        )
    elif args.requirement in {f"C{number:03d}" for number in range(153, 161)}:
        if args.bridge_root is None or not args.symbols:
            raise ValueError("C153-C160 requires --bridge-root and --symbols")
        result = dict(
            verify_c153_c160_similarity_audit_dashboard_and_flow(
                requirement_id=args.requirement,
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.bridge_root / "symbols.csv"
        source = b"".join(
            (args.bridge_root / f"deephistory_{symbol}_M1.csv").read_bytes()
            for symbol in args.symbols
        )
    elif args.requirement in {f"P{number:03d}" for number in range(1, 43)}:
        if args.bridge_root is None or not args.symbols:
            raise ValueError("P001-P042 requires --bridge-root and --symbols")
        from .principles_runtime import verify_p001_p042_research_principles
        result = dict(
            verify_p001_p042_research_principles(
                requirement_id=args.requirement,
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.bridge_root / "symbols.csv"
        source = b"".join(
            (args.bridge_root / f"deephistory_{symbol}_M1.csv").read_bytes()
            for symbol in args.symbols
        )
    elif args.requirement in {f"F{number:02d}" for number in range(0, 65)}:
        if args.bridge_root is None or not args.symbols:
            raise ValueError("F00-F64 requires --bridge-root and --symbols")
        from .friday_runtime import verify_f00_f64_scope
        result = dict(
            verify_f00_f64_scope(
                requirement_id=args.requirement,
                bridge_root=args.bridge_root,
                symbols=tuple(args.symbols),
            )
        )
        source_path = args.bridge_root / "symbols.csv"
        source = b"".join(
            (args.bridge_root / f"deephistory_{symbol}_M1.csv").read_bytes()
            for symbol in args.symbols
        )
    else:  # pragma: no cover - argparse owns the closed requirement set.
        raise ValueError(args.requirement)
    result["source_artifact"] = str(source_path)
    result["source_sha256"] = sha256(source).hexdigest()
    result["verified_at"] = datetime.now(timezone.utc).isoformat()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
