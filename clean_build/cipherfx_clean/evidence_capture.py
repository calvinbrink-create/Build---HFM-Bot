"""Produce strict, content-addressed evidence bundles for clean requirements.

The certification ledger accepts an evidence record only when the record and
the evidence envelope make the same requirement-specific claim.  This module
creates those envelopes from checked subjects; it does not certify an edge or
activate broker execution.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
from shutil import copyfile
import sqlite3
import subprocess
import sys
from typing import Mapping, Sequence
from zipfile import ZIP_DEFLATED, ZipFile

from .compliance import EvidenceKind
from .requirement_runtime import (
    verify_c006_raw_tick_library,
    verify_c007_tick_quality,
    verify_c008_hfm_candle_builder,
    verify_c009_hfm_market_states,
    verify_c011_hfm_live_chart,
    verify_c012_hfm_chart_pack,
    verify_c013_hfm_annotations,
    verify_c014_hfm_chart_history,
    verify_c015_hfm_price_structure,
    verify_c016_hfm_swing_hierarchy,
    verify_c017_hfm_supply_zones,
    verify_c018_hfm_demand_zones,
    verify_c019_hfm_zone_quality,
    verify_c020_hfm_liquidity,
    verify_c021_hfm_liquidity_sweeps,
    verify_c022_hfm_liquidity_failure_library,
    verify_c023_hfm_fair_value_gaps,
    verify_c024_hfm_fvg_lifecycle,
    verify_c025_hfm_displacement,
    verify_c026_tick_directions,
    verify_c027_tick_imbalance,
    verify_c028_tick_velocity,
    verify_c029_tick_acceleration,
    verify_c030_microstructure,
    verify_c031_spread_intelligence,
    verify_c034_momentum_intelligence,
    verify_c035_realized_volatility,
    verify_c037_volatility_regime,
    verify_c038_volatility_of_volatility,
    verify_c039_compression_detector,
    verify_c040_expansion_detector,
    verify_c041_session_engine,
    verify_c032_slippage_intelligence,
    verify_c033_latency_intelligence,
    verify_c042_session_profile_library,
    verify_c043_asia_range_engine,
    verify_c044_london_sweep_model,
    verify_c045_ny_continuation_model,
    verify_c046_ny_reversal_model,
    verify_c047_opening_range_engine,
    verify_c048_opening_range_breakout_model,
    verify_c049_false_opening_range_breakout_model,
    verify_c050_session_vwap_engine,
    verify_c052_vwap_reclaim_rejection_model,
    verify_c053_tick_volume_engine,
    verify_c055_trend_engine,
    verify_c056_trend_persistence_probability,
    verify_c057_mean_reversion_engine,
    verify_c058_breakout_engine,
    verify_c059_breakout_quality_model,
    verify_c060_failed_breakout_library,
    verify_c061_reversal_engine,
    verify_c062_continuation_engine,
    verify_c063_cross_asset_data_library,
    verify_c064_correlation_engine,
    verify_c065_correlation_breakdown_detector,
    verify_c066_market_regime_engine,
    verify_c067_regime_probability_engine,
    verify_c068_strategy_router,
    verify_c069_c076_knowledge_library,
    verify_c077_c078_pattern_outcomes,
    verify_c079_c084_historical_state_layer,
    verify_c085_c092_outcome_metrics,
    verify_c093_c098_edge_research,
    verify_c099_c105_outcome_libraries,
)


@dataclass(frozen=True)
class EvidenceSubject:
    kind: EvidenceKind
    path: Path


@dataclass(frozen=True)
class EvidenceBundle:
    envelope: Path
    manifest: Path
    evidence_ids: tuple[str, ...]


_LIVE_INTELLIGENCE_CAPTURE_TARGETS = {
    "C015": (
        "verify_c015_hfm_price_structure",
        "clean_build/cipherfx_clean/intelligence/structure.py",
        "clean_build/tests/test_item_030_requirement_runtime.py",
        ("-k", "c015"),
    ),
    "C016": (
        "verify_c016_hfm_swing_hierarchy",
        "clean_build/cipherfx_clean/intelligence/structure.py",
        "clean_build/tests/test_item_030_requirement_runtime.py",
        ("-k", "c016"),
    ),
    "C017": (
        "verify_c017_hfm_supply_zones",
        "clean_build/cipherfx_clean/intelligence/zones.py",
        "clean_build/tests/test_item_032_supply_zones.py",
        ("-k", "c017"),
    ),
    "C018": (
        "verify_c018_hfm_demand_zones",
        "clean_build/cipherfx_clean/intelligence/zones.py",
        "clean_build/tests/test_item_033_demand_zones.py",
        ("-k", "c018"),
    ),
    "C019": (
        "verify_c019_hfm_zone_quality",
        "clean_build/cipherfx_clean/intelligence/advanced.py",
        "clean_build/tests/test_item_034_zone_quality.py",
        ("-k", "c019"),
    ),
    "C020": (
        "verify_c020_hfm_liquidity",
        "clean_build/cipherfx_clean/intelligence/liquidity.py",
        "clean_build/tests/test_item_035_liquidity_engine.py",
        ("-k", "liquidity_engine"),
    ),
}

_LIVE_GEOMETRY_CAPTURE_TARGETS = {
    "C021": (
        "verify_c021_hfm_liquidity_sweeps",
        "clean_build/cipherfx_clean/intelligence/market_features.py",
        "clean_build/tests/test_item_036_liquidity_sweep_detector.py",
        (),
        True,
    ),
    "C022": (
        "verify_c022_hfm_liquidity_failure_library",
        "clean_build/cipherfx_clean/liquidity_failures.py",
        "clean_build/tests/test_item_037_liquidity_failure_library.py",
        (),
        False,
    ),
    "C023": (
        "verify_c023_hfm_fair_value_gaps",
        "clean_build/cipherfx_clean/intelligence/market_features.py",
        "clean_build/tests/test_item_038_fvg_engine.py",
        ("-k", "fvg"),
        False,
    ),
    "C024": (
        "verify_c024_hfm_fvg_lifecycle",
        "clean_build/cipherfx_clean/intelligence/market_features.py",
        "clean_build/tests/test_item_039_fvg_lifecycle.py",
        ("-k", "fvg"),
        False,
    ),
    "C025": (
        "verify_c025_hfm_displacement",
        "clean_build/cipherfx_clean/intelligence/market_features.py",
        "clean_build/tests/test_item_040_displacement_engine.py",
        ("-k", "displacement"),
        False,
    ),
}

_LIVE_TICK_INTELLIGENCE_CAPTURE_TARGETS = {
    "C026": (
        "verify_c026_tick_directions",
        "clean_build/cipherfx_clean/intelligence/market_features.py",
        "clean_build/tests/test_item_041_tick_direction_engine.py",
        ("-k", "tick_direction"),
    ),
    "C027": (
        "verify_c027_tick_imbalance",
        "clean_build/cipherfx_clean/intelligence/market_features.py",
        "clean_build/tests/test_item_042_tick_imbalance_engine.py",
        ("-k", "tick_imbalance"),
    ),
    "C028": (
        "verify_c028_tick_velocity",
        "clean_build/cipherfx_clean/intelligence/market_features.py",
        "clean_build/tests/test_item_043_tick_velocity_engine.py",
        ("-k", "tick_velocity"),
    ),
    "C029": (
        "verify_c029_tick_acceleration",
        "clean_build/cipherfx_clean/intelligence/market_features.py",
        "clean_build/tests/test_item_044_tick_acceleration_engine.py",
        ("-k", "tick_acceleration"),
    ),
    "C030": (
        "verify_c030_microstructure",
        "clean_build/cipherfx_clean/intelligence/microstructure.py",
        "clean_build/tests/test_item_045_microstructure_engine.py",
        ("-k", "microstructure"),
    ),
}

_LIVE_ANALYTICS_CAPTURE_TARGETS = {
    "C031": (
        "verify_c031_spread_intelligence",
        "clean_build/cipherfx_clean/intelligence/spread_intelligence.py",
        "clean_build/tests/test_item_046_spread_intelligence.py",
        ("-k", "spread"),
        "spread",
    ),
    "C034": (
        "verify_c034_momentum_intelligence",
        "clean_build/cipherfx_clean/intelligence/market_features.py",
        "clean_build/tests/test_item_049_momentum_intelligence.py",
        ("-k", "momentum"),
        None,
    ),
    "C035": (
        "verify_c035_realized_volatility",
        "clean_build/cipherfx_clean/intelligence/market_features.py",
        "clean_build/tests/test_item_050_realized_volatility.py",
        ("-k", "realized_volatility"),
        None,
    ),
    "C037": (
        "verify_c037_volatility_regime",
        "clean_build/cipherfx_clean/intelligence/volatility_regime.py",
        "clean_build/tests/test_item_052_volatility_regime.py",
        ("-k", "volatility_regime"),
        None,
    ),
    "C038": (
        "verify_c038_volatility_of_volatility",
        "clean_build/cipherfx_clean/intelligence/volatility_dynamics.py",
        "clean_build/tests/test_item_053_volatility_of_volatility.py",
        ("-k", "volatility"),
        None,
    ),
    "C039": (
        "verify_c039_compression_detector",
        "clean_build/cipherfx_clean/intelligence/volatility_dynamics.py",
        "clean_build/tests/test_item_054_compression_detector.py",
        ("-k", "compression"),
        "research",
    ),
    "C040": (
        "verify_c040_expansion_detector",
        "clean_build/cipherfx_clean/intelligence/volatility_dynamics.py",
        "clean_build/tests/test_item_055_expansion_detector.py",
        ("-k", "expansion"),
        None,
    ),
    "C042": (
        "verify_c042_session_profile_library",
        "clean_build/cipherfx_clean/intelligence/session_profiles.py",
        "clean_build/tests/test_item_057_session_profiles.py",
        ("-k", "session"),
        "session",
    ),
    "C043": (
        "verify_c043_asia_range_engine",
        "clean_build/cipherfx_clean/intelligence/session_profiles.py",
        "clean_build/tests/test_item_058_asia_ranges.py",
        ("-k", "asia"),
        None,
    ),
    "C044": (
        "verify_c044_london_sweep_model",
        "clean_build/cipherfx_clean/intelligence/london_sweeps.py",
        "clean_build/tests/test_item_059_london_sweeps.py",
        ("-k", "london"),
        None,
    ),
    "C045": (
        "verify_c045_ny_continuation_model",
        "clean_build/cipherfx_clean/intelligence/ny_transitions.py",
        "clean_build/tests/test_item_060_ny_transitions.py",
        (),
        None,
    ),
    "C046": (
        "verify_c046_ny_reversal_model",
        "clean_build/cipherfx_clean/intelligence/ny_transitions.py",
        "clean_build/tests/test_item_060_ny_transitions.py",
        (),
        None,
    ),
    "C047": (
        "verify_c047_opening_range_engine",
        "clean_build/cipherfx_clean/intelligence/opening_ranges.py",
        "clean_build/tests/test_item_061_opening_ranges.py",
        ("-k", "opening_range"),
        None,
    ),
    "C048": (
        "verify_c048_opening_range_breakout_model",
        "clean_build/cipherfx_clean/intelligence/opening_range_breakouts.py",
        "clean_build/tests/test_item_062_opening_range_breakouts.py",
        ("-k", "breakout"),
        None,
    ),
    "C049": (
        "verify_c049_false_opening_range_breakout_model",
        "clean_build/cipherfx_clean/intelligence/false_opening_range_breakouts.py",
        "clean_build/tests/test_item_063_false_opening_range_breakouts.py",
        ("-k", "breakout"),
        None,
    ),
    "C050": (
        "verify_c050_session_vwap_engine",
        "clean_build/cipherfx_clean/intelligence/session_vwap.py",
        "clean_build/tests/test_item_064_session_vwap.py",
        ("-k", "vwap"),
        None,
    ),
    "C052": (
        "verify_c052_vwap_reclaim_rejection_model",
        "clean_build/cipherfx_clean/intelligence/vwap_reclaim_rejection.py",
        "clean_build/tests/test_item_066_vwap_reclaim_rejection.py",
        ("-k", "reclaim"),
        "session",
    ),
    "C055": (
        "verify_c055_trend_engine",
        "clean_build/cipherfx_clean/intelligence/trend_engine.py",
        "clean_build/tests/test_item_069_trend_engine.py",
        ("-k", "trend"),
        None,
    ),
    "C056": (
        "verify_c056_trend_persistence_probability",
        "clean_build/cipherfx_clean/intelligence/trend_persistence.py",
        "clean_build/tests/test_item_070_trend_persistence.py",
        ("-k", "persistence"),
        "trend",
    ),
    "C057": (
        "verify_c057_mean_reversion_engine",
        "clean_build/cipherfx_clean/intelligence/mean_reversion_engine.py",
        "clean_build/tests/test_item_071_mean_reversion_engine.py",
        ("-k", "mean_reversion"),
        None,
    ),
    "C058": (
        "verify_c058_breakout_engine",
        "clean_build/cipherfx_clean/intelligence/breakout_engine.py",
        "clean_build/tests/test_item_072_breakout_engine.py",
        ("-k", "breakout"),
        None,
    ),
    "C059": (
        "verify_c059_breakout_quality_model",
        "clean_build/cipherfx_clean/intelligence/breakout_quality_engine.py",
        "clean_build/tests/test_item_073_breakout_quality_engine.py",
        ("-k", "breakout"),
        None,
    ),
    "C060": (
        "verify_c060_failed_breakout_library",
        "clean_build/cipherfx_clean/intelligence/failed_breakout.py",
        "clean_build/tests/test_item_074_failed_breakout_library.py",
        ("-k", "failed_breakout"),
        None,
    ),
    "C061": (
        "verify_c061_reversal_engine",
        "clean_build/cipherfx_clean/intelligence/reversal_engine.py",
        "clean_build/tests/test_item_075_reversal_engine.py",
        ("-k", "reversal"),
        None,
    ),
    "C062": (
        "verify_c062_continuation_engine",
        "clean_build/cipherfx_clean/intelligence/continuation_engine.py",
        "clean_build/tests/test_item_076_continuation_engine.py",
        ("-k", "continuation"),
        None,
    ),
    "C063": (
        "verify_c063_cross_asset_data_library",
        "clean_build/cipherfx_clean/intelligence/cross_asset.py",
        "clean_build/tests/test_item_077_cross_asset.py",
        ("-k", "cross_asset"),
        None,
    ),
    "C064": (
        "verify_c064_correlation_engine",
        "clean_build/cipherfx_clean/intelligence/cross_asset.py",
        "clean_build/tests/test_item_077_cross_asset.py",
        ("-k", "correlation"),
        None,
    ),
    "C065": (
        "verify_c065_correlation_breakdown_detector",
        "clean_build/cipherfx_clean/intelligence/cross_asset.py",
        "clean_build/tests/test_item_077_cross_asset.py",
        ("-k", "breakdown"),
        None,
    ),
    "C066": (
        "verify_c066_market_regime_engine",
        "clean_build/cipherfx_clean/intelligence/market_regime_engine.py",
        "clean_build/tests/test_item_078_market_regime.py",
        ("-k", "market_regime"),
        None,
    ),
    "C067": (
        "verify_c067_regime_probability_engine",
        "clean_build/cipherfx_clean/intelligence/regime_probability_engine.py",
        "clean_build/tests/test_item_079_regime_probability.py",
        ("-k", "regime_probability"),
        None,
    ),
    "C068": (
        "verify_c068_strategy_router",
        "clean_build/cipherfx_clean/intelligence/router.py",
        "clean_build/tests/test_item_080_strategy_router.py",
        ("-k", "strategy_router"),
        None,
    ),
}

_LIVE_BROKER_CAPTURE_TARGETS = {
    "C032": (
        "verify_c032_slippage_intelligence",
        "clean_build/cipherfx_clean/slippage.py",
        "clean_build/tests/test_item_047_slippage_intelligence.py",
        ("-k", "slippage"),
    ),
    "C033": (
        "verify_c033_latency_intelligence",
        "clean_build/cipherfx_clean/intelligence/latency.py",
        "clean_build/tests/test_item_048_latency_intelligence.py",
        ("-k", "latency"),
    ),
}

_LIVE_SESSION_BROKER_CAPTURE_TARGETS = {
    "C041": (
        "verify_c041_session_engine",
        "clean_build/cipherfx_clean/intelligence/session.py",
        "clean_build/tests/test_item_056_session_engine.py",
        (),
    ),
}

_LIVE_ACTIVITY_BROKER_CAPTURE_TARGETS = {
    "C053": (
        "verify_c053_tick_volume_engine",
        "clean_build/cipherfx_clean/intelligence/tick_volume.py",
        "clean_build/tests/test_item_067_tick_volume.py",
        ("-k", "tick_volume"),
    ),
}

_LIVE_KNOWLEDGE_CAPTURE_TARGETS = {
    "C069": ("clean_build/cipherfx_clean/intelligence/knowledge.py", "clean_build/tests/test_item_081_knowledge_libraries.py"),
    "C070": ("clean_build/cipherfx_clean/intelligence/knowledge.py", "clean_build/tests/test_item_081_knowledge_libraries.py"),
    "C071": ("clean_build/cipherfx_clean/intelligence/knowledge.py", "clean_build/tests/test_item_081_knowledge_libraries.py"),
    "C072": ("clean_build/cipherfx_clean/intelligence/knowledge.py", "clean_build/tests/test_item_081_knowledge_libraries.py"),
    "C073": ("clean_build/cipherfx_clean/intelligence/knowledge.py", "clean_build/tests/test_item_081_knowledge_libraries.py"),
    "C074": ("clean_build/cipherfx_clean/intelligence/knowledge.py", "clean_build/tests/test_item_081_knowledge_libraries.py"),
    "C075": ("clean_build/cipherfx_clean/intelligence/knowledge.py", "clean_build/tests/test_item_081_knowledge_libraries.py"),
    "C076": ("clean_build/cipherfx_clean/intelligence/strategies.py", "clean_build/tests/test_item_081_knowledge_libraries.py"),
}

_LIVE_PATTERN_OUTCOME_CAPTURE_TARGETS = {
    "C077": ("clean_build/cipherfx_clean/intelligence/pattern_outcomes.py", "clean_build/tests/test_item_082_pattern_outcomes.py"),
    "C078": ("clean_build/cipherfx_clean/intelligence/pattern_outcomes.py", "clean_build/tests/test_item_082_pattern_outcomes.py"),
}

_LIVE_HISTORICAL_STATE_CAPTURE_TARGETS = {
    "C079": ("clean_build/cipherfx_clean/intelligence/fingerprint_engine.py", "clean_build/tests/test_item_083_fingerprint_history.py"),
    "C080": ("clean_build/cipherfx_clean/intelligence/feature_store.py", "clean_build/tests/test_item_005_historical_memory.py"),
    "C081": ("clean_build/cipherfx_clean/historical_memory.py", "clean_build/tests/test_item_005_historical_memory.py"),
    "C082": ("clean_build/cipherfx_clean/historical_memory.py", "clean_build/tests/test_item_005_historical_memory.py"),
    "C083": ("clean_build/cipherfx_clean/historical_memory.py", "clean_build/tests/test_item_005_historical_memory.py"),
    "C084": ("clean_build/cipherfx_clean/historical_memory.py", "clean_build/tests/test_item_084_outcome_metrics.py"),
}

_LIVE_OUTCOME_METRIC_CAPTURE_TARGETS = {
    f"C{number:03d}": (
        "clean_build/cipherfx_clean/intelligence/outcome_metrics.py",
        "clean_build/tests/test_item_084_outcome_metrics.py",
    )
    for number in range(85, 93)
}

_LIVE_RESEARCH_CAPTURE_TARGETS = {
    "C093": ("verify_c093_c098_edge_research", "clean_build/cipherfx_clean/intelligence/edge_miner.py", "clean_build/tests/test_item_093_edge_research.py", False),
    "C094": ("verify_c093_c098_edge_research", "clean_build/cipherfx_clean/intelligence/edge_miner.py", "clean_build/tests/test_item_093_edge_research.py", False),
    "C095": ("verify_c093_c098_edge_research", "clean_build/cipherfx_clean/research_pipeline.py", "clean_build/tests/test_item_093_edge_research.py", True),
    "C096": ("verify_c093_c098_edge_research", "clean_build/cipherfx_clean/intelligence/hypothesis.py", "clean_build/tests/test_item_093_edge_research.py", False),
    "C097": ("verify_c093_c098_edge_research", "clean_build/cipherfx_clean/intelligence/research.py", "clean_build/tests/test_item_093_edge_research.py", False),
    "C098": ("verify_c093_c098_edge_research", "clean_build/cipherfx_clean/intelligence/validation_full.py", "clean_build/tests/test_item_093_edge_research.py", False),
    "C099": ("verify_c099_c105_outcome_libraries", "clean_build/cipherfx_clean/intelligence/hypothesis.py", "clean_build/tests/test_item_099_outcome_libraries.py", False),
    "C100": ("verify_c099_c105_outcome_libraries", "clean_build/cipherfx_clean/intelligence/data_library.py", "clean_build/tests/test_item_099_outcome_libraries.py", False),
    "C101": ("verify_c099_c105_outcome_libraries", "clean_build/cipherfx_clean/intelligence/data_library.py", "clean_build/tests/test_item_099_outcome_libraries.py", False),
    "C102": ("verify_c099_c105_outcome_libraries", "clean_build/cipherfx_clean/intelligence/data_library.py", "clean_build/tests/test_item_099_outcome_libraries.py", False),
    "C103": ("verify_c099_c105_outcome_libraries", "clean_build/cipherfx_clean/intelligence/data_library.py", "clean_build/tests/test_item_099_outcome_libraries.py", False),
    "C104": ("verify_c099_c105_outcome_libraries", "clean_build/cipherfx_clean/intelligence/data_library.py", "clean_build/tests/test_item_099_outcome_libraries.py", True),
    "C105": ("verify_c099_c105_outcome_libraries", "clean_build/cipherfx_clean/intelligence/data_library.py", "clean_build/tests/test_item_099_outcome_libraries.py", False),
}

_HFM_SNAPSHOT_TIMEFRAMES = ("M1", "M5", "M15", "H1", "H4")
_HFM_RESEARCH_TIMEFRAMES = ("MN1", "W1", "D1", "H4", "H1", "M30", "M15", "M5", "M3", "M1")


def write_evidence_bundle(
    *,
    workspace_root: Path,
    requirement_id: str,
    subjects: Sequence[EvidenceSubject],
    output_directory: Path,
    observed_at: datetime | None = None,
) -> EvidenceBundle:
    """Write one strict evidence envelope and a v2 manifest for one requirement."""

    root = workspace_root.resolve()
    destination = output_directory.resolve()
    _require_inside(destination, root, "evidence output")
    if not requirement_id or not subjects:
        raise ValueError("requirement id and at least one subject are required")
    observed = (observed_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    stamp = observed.strftime("%Y%m%dT%H%M%S%fZ")
    claims: dict[str, Mapping[str, object]] = {}
    for subject in subjects:
        subject_path = subject.path.resolve()
        _require_inside(subject_path, root, "evidence subject")
        if not subject_path.is_file():
            raise FileNotFoundError(subject_path)
        evidence_id = f"{requirement_id}:{subject.kind.value}:{stamp}"
        if evidence_id in claims:
            raise ValueError(f"duplicate evidence kind: {subject.kind.value}")
        claims[evidence_id] = {
            "evidence_id": evidence_id,
            "requirement_id": requirement_id,
            "kind": subject.kind.value,
            "result": "PASS",
            "observed_at": observed.isoformat(),
            "subject": {
                "path": str(subject_path.relative_to(root)),
                "digest": _file_digest(subject_path),
            },
        }

    destination.mkdir(parents=True, exist_ok=True)
    envelope = destination / f"{requirement_id.lower()}_evidence_{stamp}.json"
    _write_json(envelope, {"schema_version": 1, "evidence_claims": claims})
    envelope_digest = _file_digest(envelope)
    manifest = destination / f"{requirement_id.lower()}_manifest_{stamp}.json"
    records = tuple(
        {
            "evidence_id": evidence_id,
            "requirement_id": requirement_id,
            "kind": str(claim["kind"]),
            "location": f"{envelope.relative_to(root)}#{evidence_id}",
            "digest": envelope_digest,
            "observed_at": str(claim["observed_at"]),
            "result": "PASS",
        }
        for evidence_id, claim in sorted(claims.items())
    )
    _write_json(manifest, {"schema_version": 2, "evidence": records})
    return EvidenceBundle(envelope, manifest, tuple(sorted(claims)))


def capture_c006_live_ticks(
    *,
    workspace_root: Path,
    tick_database: Path,
    output_directory: Path,
    python_executable: Path,
) -> EvidenceBundle:
    """Capture real C006 raw-tick proof from the direct MT5 tick receiver."""

    root = workspace_root.resolve()
    database = tick_database.resolve()
    output = output_directory.resolve()
    _require_inside(database, root, "tick database")
    _require_inside(output, root, "evidence output")
    output.mkdir(parents=True, exist_ok=True)
    snapshot = output / f"c006_ticks_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.sqlite3"
    _snapshot_sqlite_database(database, snapshot)
    verification = verify_c006_raw_tick_library(snapshot)
    if verification.get("status") != "PASS":
        raise ValueError("C006 raw tick verification did not pass")
    return capture_verified_requirement(
        workspace_root=root,
        requirement_id="C006",
        verification=verification,
        code_subject=root / "clean_build/cipherfx_clean/tick_ingest.py",
        test_file=root / "clean_build/tests/test_item_006_websocket_tick_ingest.py",
        data_subjects=(snapshot,),
        output_directory=output,
        python_executable=python_executable,
    )


def capture_c007_live_tick_quality(
    *,
    workspace_root: Path,
    tick_database: Path,
    output_directory: Path,
    python_executable: Path,
    maximum_age_seconds: float,
) -> EvidenceBundle:
    """Capture genuine persisted fresh and stale tick classifications."""

    root = workspace_root.resolve()
    database = tick_database.resolve()
    output = output_directory.resolve()
    _require_inside(database, root, "tick-quality database")
    _require_inside(output, root, "evidence output")
    output.mkdir(parents=True, exist_ok=True)
    snapshot = output / f"c007_tick_quality_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.sqlite3"
    _snapshot_sqlite_database(database, snapshot)
    verification = dict(
        verify_c007_tick_quality(
            _c007_payload_from_quality_events(snapshot),
            maximum_age_seconds=maximum_age_seconds,
        )
    )
    verification["source_database"] = str(snapshot)
    return capture_verified_requirement(
        workspace_root=root,
        requirement_id="C007",
        verification=verification,
        code_subject=root / "clean_build/cipherfx_clean/observation.py",
        test_file=root / "clean_build/tests/test_item_030_requirement_runtime.py",
        data_subjects=(snapshot,),
        output_directory=output,
        python_executable=python_executable,
        test_arguments=("-k", "c007"),
    )


def capture_c008_market_snapshot(
    *,
    workspace_root: Path,
    bridge_root: Path,
    tick_database: Path,
    output_directory: Path,
    python_executable: Path,
    symbols: Sequence[str],
) -> EvidenceBundle:
    """Capture real C008 proof for native and tick-derived candle frames."""

    root = workspace_root.resolve()
    verification = verify_c008_hfm_candle_builder(
        bridge_root=bridge_root.resolve(),
        tick_database=tick_database.resolve(),
        symbols=tuple(symbols),
        observed_at=datetime.now(timezone.utc),
    )
    return capture_verified_requirement(
        workspace_root=root,
        requirement_id="C008",
        verification=verification,
        code_subject=root / "clean_build/cipherfx_clean/snapshot.py",
        test_file=root / "clean_build/tests/test_item_030_requirement_runtime.py",
        test_arguments=("-k", "c008"),
        output_directory=output_directory,
        python_executable=python_executable,
    )


def capture_c009_market_state(
    *,
    workspace_root: Path,
    bridge_root: Path,
    tick_database: Path,
    output_directory: Path,
    python_executable: Path,
    symbols: Sequence[str],
) -> EvidenceBundle:
    """Capture real C009 state proof from the same direct tick source as live."""

    root = workspace_root.resolve()
    verification = verify_c009_hfm_market_states(
        bridge_root=bridge_root.resolve(),
        tick_database=tick_database.resolve(),
        symbols=tuple(symbols),
        observed_at=datetime.now(timezone.utc),
    )
    return capture_verified_requirement(
        workspace_root=root,
        requirement_id="C009",
        verification=verification,
        code_subject=root / "clean_build/cipherfx_clean/snapshot.py",
        test_file=root / "clean_build/tests/test_item_030_requirement_runtime.py",
        test_arguments=("-k", "c009"),
        output_directory=output_directory,
        python_executable=python_executable,
    )


def capture_c011_live_chart(
    *,
    workspace_root: Path,
    bridge_root: Path,
    output_directory: Path,
    chart_directory: Path,
    python_executable: Path,
    symbol: str,
    timeframe: str,
) -> EvidenceBundle:
    """Capture a current completed-bar chart without involving broker execution."""

    root = workspace_root.resolve()
    verification = verify_c011_hfm_live_chart(
        bridge_root=bridge_root.resolve(),
        symbol=symbol,
        timeframe=timeframe,
        chart_directory=chart_directory.resolve(),
    )
    return capture_verified_requirement(
        workspace_root=root,
        requirement_id="C011",
        verification=verification,
        code_subject=root / "clean_build/cipherfx_clean/intelligence/renderer.py",
        test_file=root / "clean_build/tests/test_item_030_requirement_runtime.py",
        test_arguments=("-k", "c011"),
        data_subjects=(Path(str(verification["chart"]["chart_path"])),),
        output_directory=output_directory,
        python_executable=python_executable,
    )


def capture_c012_chart_pack(
    *,
    workspace_root: Path,
    bridge_root: Path,
    output_directory: Path,
    chart_directory: Path,
    python_executable: Path,
    symbol: str,
) -> EvidenceBundle:
    """Capture a linked multi-timeframe chart pack from one observation."""

    root = workspace_root.resolve()
    verification = verify_c012_hfm_chart_pack(
        bridge_root=bridge_root.resolve(),
        symbol=symbol,
        chart_directory=chart_directory.resolve(),
    )
    return capture_verified_requirement(
        workspace_root=root,
        requirement_id="C012",
        verification=verification,
        code_subject=root / "clean_build/cipherfx_clean/intelligence/chart.py",
        test_file=root / "clean_build/tests/test_item_030_requirement_runtime.py",
        test_arguments=("-k", "c012"),
        output_directory=output_directory,
        python_executable=python_executable,
    )


def capture_c013_chart_annotations(
    *,
    workspace_root: Path,
    bridge_root: Path,
    output_directory: Path,
    chart_directory: Path,
    python_executable: Path,
    symbol: str,
    timeframe: str,
) -> EvidenceBundle:
    """Capture annotation rendering evidence; the verifier never sends an order."""

    root = workspace_root.resolve()
    verification = verify_c013_hfm_annotations(
        bridge_root=bridge_root.resolve(),
        symbol=symbol,
        timeframe=timeframe,
        chart_directory=chart_directory.resolve(),
    )
    return capture_verified_requirement(
        workspace_root=root,
        requirement_id="C013",
        verification=verification,
        code_subject=root / "clean_build/cipherfx_clean/intelligence/chart.py",
        test_file=root / "clean_build/tests/test_item_030_requirement_runtime.py",
        test_arguments=("-k", "c013"),
        output_directory=output_directory,
        python_executable=python_executable,
    )


def capture_c014_chart_history(
    *,
    workspace_root: Path,
    bridge_root: Path,
    database: Path,
    output_directory: Path,
    chart_directory: Path,
    python_executable: Path,
    symbol: str,
    timeframe: str,
) -> EvidenceBundle:
    """Capture idempotent persistent-chart evidence in an isolated database."""

    root = workspace_root.resolve()
    database = database.resolve()
    _require_inside(database, root, "chart-history database")
    verification = verify_c014_hfm_chart_history(
        bridge_root=bridge_root.resolve(),
        database=database,
        symbol=symbol,
        timeframe=timeframe,
        chart_directory=chart_directory.resolve(),
    )
    data_bundle = _bundle_data_subject(
        output_directory.resolve(),
        "C014",
        (database, Path(str(verification["chart_path"]))),
    )
    return capture_verified_requirement(
        workspace_root=root,
        requirement_id="C014",
        verification=verification,
        code_subject=root / "clean_build/cipherfx_clean/chart_history.py",
        test_file=root / "clean_build/tests/test_item_031_chart_history.py",
        data_subjects=(data_bundle,),
        output_directory=output_directory,
        python_executable=python_executable,
    )


def capture_live_intelligence_requirement(
    *,
    workspace_root: Path,
    requirement_id: str,
    bridge_root: Path,
    output_directory: Path,
    python_executable: Path,
    symbols: Sequence[str],
) -> EvidenceBundle:
    """Capture a live HFM-backed market-intelligence verification receipt."""

    try:
        verifier_name, code_path, test_path, test_arguments = _LIVE_INTELLIGENCE_CAPTURE_TARGETS[requirement_id]
    except KeyError as exc:
        raise ValueError(f"unsupported live intelligence requirement: {requirement_id}") from exc
    root = workspace_root.resolve()
    verifier = globals()[verifier_name]
    verification = verifier(
        bridge_root=bridge_root.resolve(),
        symbols=tuple(symbols),
    )
    return capture_verified_requirement(
        workspace_root=root,
        requirement_id=requirement_id,
        verification=verification,
        code_subject=root / code_path,
        test_file=root / test_path,
        output_directory=output_directory,
        python_executable=python_executable,
        test_arguments=test_arguments,
    )


def capture_live_geometry_requirement(
    *,
    workspace_root: Path,
    requirement_id: str,
    bridge_root: Path,
    output_directory: Path,
    python_executable: Path,
    symbols: Sequence[str],
    database: Path | None = None,
) -> EvidenceBundle:
    """Capture live geometry evidence without letting moving source files drift."""

    try:
        verifier_name, code_path, test_path, test_arguments, snapshot_inputs = _LIVE_GEOMETRY_CAPTURE_TARGETS[requirement_id]
    except KeyError as exc:
        raise ValueError(f"unsupported live geometry requirement: {requirement_id}") from exc
    root = workspace_root.resolve()
    output = output_directory.resolve()
    source_root = bridge_root.resolve()
    data_subjects: tuple[Path, ...] = ()
    if snapshot_inputs:
        input_snapshot = _snapshot_hfm_inputs(
            destination=output,
            requirement_id=requirement_id,
            bridge_root=source_root,
            symbols=symbols,
        )
        source_root = input_snapshot
        data_subjects = (_bundle_data_subject(output, requirement_id, tuple(sorted(input_snapshot.iterdir()))),)
    verifier = globals()[verifier_name]
    arguments: dict[str, object] = {
        "bridge_root": source_root,
        "symbols": tuple(symbols),
    }
    if requirement_id == "C022":
        if database is None:
            raise ValueError("C022 requires an isolated evidence database")
        isolated_database = database.resolve()
        _require_inside(isolated_database, root, "liquidity-failure database")
        arguments["database"] = isolated_database
    verification = verifier(**arguments)
    return capture_verified_requirement(
        workspace_root=root,
        requirement_id=requirement_id,
        verification=verification,
        code_subject=root / code_path,
        test_file=root / test_path,
        output_directory=output,
        python_executable=python_executable,
        data_subjects=data_subjects,
        test_arguments=test_arguments,
    )


def capture_live_tick_intelligence_requirement(
    *,
    workspace_root: Path,
    requirement_id: str,
    tick_database: Path,
    output_directory: Path,
    python_executable: Path,
) -> EvidenceBundle:
    """Capture tick-intelligence evidence from an immutable receiver snapshot."""

    try:
        verifier_name, code_path, test_path, test_arguments = _LIVE_TICK_INTELLIGENCE_CAPTURE_TARGETS[requirement_id]
    except KeyError as exc:
        raise ValueError(f"unsupported live tick intelligence requirement: {requirement_id}") from exc
    root = workspace_root.resolve()
    source_database = tick_database.resolve()
    output = output_directory.resolve()
    _require_inside(source_database, root, "tick database")
    _require_inside(output, root, "evidence output")
    output.mkdir(parents=True, exist_ok=True)
    snapshot = output / f"{requirement_id.lower()}_ticks_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.sqlite3"
    _snapshot_sqlite_database(source_database, snapshot)
    verification = globals()[verifier_name](snapshot)
    return capture_verified_requirement(
        workspace_root=root,
        requirement_id=requirement_id,
        verification=verification,
        code_subject=root / code_path,
        test_file=root / test_path,
        output_directory=output,
        python_executable=python_executable,
        test_arguments=test_arguments,
    )


def capture_live_analytics_requirement(
    *,
    workspace_root: Path,
    requirement_id: str,
    bridge_root: Path,
    output_directory: Path,
    python_executable: Path,
    symbols: Sequence[str],
) -> EvidenceBundle:
    """Capture live analytical evidence, freezing required HFM inputs first."""

    try:
        verifier_name, code_path, test_path, test_arguments, snapshot_kind = _LIVE_ANALYTICS_CAPTURE_TARGETS[requirement_id]
    except KeyError as exc:
        raise ValueError(f"unsupported live analytics requirement: {requirement_id}") from exc
    root = workspace_root.resolve()
    output = output_directory.resolve()
    source_root = bridge_root.resolve()
    data_subjects: tuple[Path, ...] = ()
    if snapshot_kind is not None:
        if snapshot_kind in {"spread", "session"}:
            timeframes = ("M1",)
        elif snapshot_kind == "trend":
            timeframes = ("M1", "M5", "M15", "H1", "H4", "D1")
        else:
            timeframes = _HFM_RESEARCH_TIMEFRAMES
        snapshot = _snapshot_hfm_inputs(
            destination=output,
            requirement_id=requirement_id,
            bridge_root=source_root,
            symbols=symbols,
            timeframes=timeframes,
            include_combined_m1=snapshot_kind in {"spread", "session", "trend"},
            include_ticks=snapshot_kind in {"spread", "session", "trend"},
        )
        source_root = snapshot
        data_subjects = (_bundle_data_subject(output, requirement_id, tuple(sorted(snapshot.iterdir()))),)
    verification = globals()[verifier_name](bridge_root=source_root, symbols=tuple(symbols))
    return capture_verified_requirement(
        workspace_root=root,
        requirement_id=requirement_id,
        verification=verification,
        code_subject=root / code_path,
        test_file=root / test_path,
        output_directory=output,
        python_executable=python_executable,
        data_subjects=data_subjects,
        test_arguments=test_arguments,
    )


def capture_knowledge_library_requirement(
    *,
    workspace_root: Path,
    requirement_id: str,
    output_directory: Path,
    python_executable: Path,
) -> EvidenceBundle:
    """Capture a runtime receipt for a structured research vocabulary check."""

    try:
        code_path, test_path = _LIVE_KNOWLEDGE_CAPTURE_TARGETS[requirement_id]
    except KeyError as exc:
        raise ValueError(f"unsupported knowledge-library requirement: {requirement_id}") from exc
    root = workspace_root.resolve()
    verification = verify_c069_c076_knowledge_library(requirement_id)
    return capture_verified_requirement(
        workspace_root=root,
        requirement_id=requirement_id,
        verification=verification,
        code_subject=root / code_path,
        test_file=root / test_path,
        output_directory=output_directory,
        python_executable=python_executable,
    )


def _capture_historical_hfm_inputs(
    *,
    workspace_root: Path,
    requirement_id: str,
    bridge_root: Path,
    output_directory: Path,
    python_executable: Path,
    symbols: Sequence[str],
    code_path: str,
    test_path: str,
    verifier: object,
) -> EvidenceBundle:
    """Freeze six completed HFM frames before a history-based runtime check."""

    root = workspace_root.resolve()
    output = output_directory.resolve()
    snapshot = _snapshot_hfm_inputs(
        destination=output,
        requirement_id=requirement_id,
        bridge_root=bridge_root.resolve(),
        symbols=symbols,
        timeframes=("M1", "M5", "M15", "H1", "H4", "D1"),
        include_combined_m1=True,
        include_ticks=True,
    )
    data_subject = _bundle_data_subject(output, requirement_id, tuple(sorted(snapshot.iterdir())))
    verification = verifier(snapshot)
    return capture_verified_requirement(
        workspace_root=root,
        requirement_id=requirement_id,
        verification=verification,
        code_subject=root / code_path,
        test_file=root / test_path,
        output_directory=output,
        python_executable=python_executable,
        data_subjects=(data_subject,),
    )


def capture_pattern_outcome_requirement(
    *,
    workspace_root: Path,
    requirement_id: str,
    bridge_root: Path,
    output_directory: Path,
    python_executable: Path,
    symbols: Sequence[str],
) -> EvidenceBundle:
    """Capture one requirement-specific outcome receipt from frozen HFM inputs."""

    try:
        code_path, test_path = _LIVE_PATTERN_OUTCOME_CAPTURE_TARGETS[requirement_id]
    except KeyError as exc:
        raise ValueError(f"unsupported pattern-outcome requirement: {requirement_id}") from exc

    def verify(snapshot: Path) -> Mapping[str, object]:
        receipt = dict(verify_c077_c078_pattern_outcomes(bridge_root=snapshot, symbols=tuple(symbols)))
        receipt["requirement_id"] = requirement_id
        return receipt

    return _capture_historical_hfm_inputs(
        workspace_root=workspace_root,
        requirement_id=requirement_id,
        bridge_root=bridge_root,
        output_directory=output_directory,
        python_executable=python_executable,
        symbols=symbols,
        code_path=code_path,
        test_path=test_path,
        verifier=verify,
    )


def capture_historical_state_requirement(
    *,
    workspace_root: Path,
    requirement_id: str,
    bridge_root: Path,
    output_directory: Path,
    python_executable: Path,
    symbols: Sequence[str],
) -> EvidenceBundle:
    """Capture one history-state receipt from a frozen completed-candle snapshot."""

    try:
        code_path, test_path = _LIVE_HISTORICAL_STATE_CAPTURE_TARGETS[requirement_id]
    except KeyError as exc:
        raise ValueError(f"unsupported historical-state requirement: {requirement_id}") from exc

    def verify(snapshot: Path) -> Mapping[str, object]:
        return verify_c079_c084_historical_state_layer(
            requirement_id=requirement_id,
            bridge_root=snapshot,
            symbols=tuple(symbols),
        )

    return _capture_historical_hfm_inputs(
        workspace_root=workspace_root,
        requirement_id=requirement_id,
        bridge_root=bridge_root,
        output_directory=output_directory,
        python_executable=python_executable,
        symbols=symbols,
        code_path=code_path,
        test_path=test_path,
        verifier=verify,
    )


def capture_live_outcome_metric_requirement(
    *,
    workspace_root: Path,
    requirement_id: str,
    bridge_root: Path,
    tick_database: Path,
    output_directory: Path,
    python_executable: Path,
    symbols: Sequence[str],
) -> EvidenceBundle:
    """Capture outcome metrics using frozen HFM candles and raw-tick inputs."""

    try:
        code_path, test_path = _LIVE_OUTCOME_METRIC_CAPTURE_TARGETS[requirement_id]
    except KeyError as exc:
        raise ValueError(f"unsupported outcome-metric requirement: {requirement_id}") from exc
    root = workspace_root.resolve()
    output = output_directory.resolve()
    tick_source = tick_database.resolve()
    _require_inside(tick_source, root, "outcome-metric tick database")
    hfm_snapshot = _snapshot_hfm_inputs(
        destination=output,
        requirement_id=requirement_id,
        bridge_root=bridge_root.resolve(),
        symbols=symbols,
        timeframes=("M1",),
        include_combined_m1=True,
        include_ticks=True,
    )
    tick_snapshot = output / f"{requirement_id.lower()}_ticks_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.sqlite3"
    _snapshot_sqlite_database(tick_source, tick_snapshot)
    verification = verify_c085_c092_outcome_metrics(
        requirement_id=requirement_id,
        bridge_root=hfm_snapshot,
        tick_database=tick_snapshot,
        symbols=tuple(symbols),
    )
    data_subjects: tuple[Path, ...] = ()
    if requirement_id in {"C085", "C090"}:
        data_subjects = (
            _bundle_data_subject(
                output,
                requirement_id,
                (*tuple(sorted(hfm_snapshot.iterdir())), tick_snapshot),
            ),
        )
    return capture_verified_requirement(
        workspace_root=root,
        requirement_id=requirement_id,
        verification=verification,
        code_subject=root / code_path,
        test_file=root / test_path,
        output_directory=output,
        python_executable=python_executable,
        data_subjects=data_subjects,
    )


def capture_live_research_requirement(
    *,
    workspace_root: Path,
    requirement_id: str,
    bridge_root: Path,
    output_directory: Path,
    python_executable: Path,
    symbols: Sequence[str],
) -> EvidenceBundle:
    """Capture research results using an immutable completed-M1 HFM snapshot."""

    try:
        verifier_name, code_path, test_path, requires_data = _LIVE_RESEARCH_CAPTURE_TARGETS[requirement_id]
    except KeyError as exc:
        raise ValueError(f"unsupported research requirement: {requirement_id}") from exc
    root = workspace_root.resolve()
    output = output_directory.resolve()
    snapshot = _snapshot_hfm_inputs(
        destination=output,
        requirement_id=requirement_id,
        bridge_root=bridge_root.resolve(),
        symbols=symbols,
        timeframes=("M1",),
        include_combined_m1=True,
        include_ticks=True,
    )
    verification = globals()[verifier_name](
        requirement_id=requirement_id,
        bridge_root=snapshot,
        symbols=tuple(symbols),
    )
    data_subjects: tuple[Path, ...] = ()
    if requires_data:
        data_subjects = (_bundle_data_subject(output, requirement_id, tuple(sorted(snapshot.iterdir()))),)
    return capture_verified_requirement(
        workspace_root=root,
        requirement_id=requirement_id,
        verification=verification,
        code_subject=root / code_path,
        test_file=root / test_path,
        output_directory=output,
        python_executable=python_executable,
        data_subjects=data_subjects,
    )


def capture_live_broker_requirement(
    *,
    workspace_root: Path,
    requirement_id: str,
    bridge_root: Path,
    database: Path,
    output_directory: Path,
    python_executable: Path,
    symbols: Sequence[str],
) -> EvidenceBundle:
    """Capture read-only, exact HFM order/deal evidence for broker measurements."""

    try:
        verifier_name, code_path, test_path, test_arguments = _LIVE_BROKER_CAPTURE_TARGETS[requirement_id]
    except KeyError as exc:
        raise ValueError(f"unsupported live broker requirement: {requirement_id}") from exc
    root = workspace_root.resolve()
    output = output_directory.resolve()
    _require_inside(output, root, "evidence output")
    isolated_database = database.resolve()
    _require_inside(isolated_database, root, "broker-evidence database")
    broker_snapshot = _snapshot_broker_exports(
        destination=output,
        requirement_id=requirement_id,
        bridge_root=bridge_root.resolve(),
    )
    broker_bundle = _bundle_data_subject(output, requirement_id, tuple(sorted(broker_snapshot.iterdir())))
    verification = globals()[verifier_name](
        bridge_root=broker_snapshot,
        database=isolated_database,
        symbols=tuple(symbols),
    )
    return capture_verified_requirement(
        workspace_root=root,
        requirement_id=requirement_id,
        verification=verification,
        code_subject=root / code_path,
        test_file=root / test_path,
        output_directory=output,
        python_executable=python_executable,
        broker_subjects=(broker_bundle,),
        test_arguments=test_arguments,
    )


def capture_live_session_broker_requirement(
    *,
    workspace_root: Path,
    requirement_id: str,
    bridge_root: Path,
    output_directory: Path,
    python_executable: Path,
    symbols: Sequence[str],
) -> EvidenceBundle:
    """Capture exact HFM tick-time exports for broker-clock session evidence."""

    try:
        verifier_name, code_path, test_path, test_arguments = _LIVE_SESSION_BROKER_CAPTURE_TARGETS[requirement_id]
    except KeyError as exc:
        raise ValueError(f"unsupported live session broker requirement: {requirement_id}") from exc
    root = workspace_root.resolve()
    output = output_directory.resolve()
    _require_inside(output, root, "evidence output")
    tick_snapshot = _snapshot_broker_tick_exports(
        destination=output,
        requirement_id=requirement_id,
        bridge_root=bridge_root.resolve(),
        symbols=symbols,
    )
    broker_bundle = _bundle_data_subject(output, requirement_id, tuple(sorted(tick_snapshot.iterdir())))
    verification = globals()[verifier_name](bridge_root=tick_snapshot, symbols=tuple(symbols))
    return capture_verified_requirement(
        workspace_root=root,
        requirement_id=requirement_id,
        verification=verification,
        code_subject=root / code_path,
        test_file=root / test_path,
        output_directory=output,
        python_executable=python_executable,
        broker_subjects=(broker_bundle,),
        test_arguments=test_arguments,
    )


def capture_live_activity_broker_requirement(
    *,
    workspace_root: Path,
    requirement_id: str,
    bridge_root: Path,
    output_directory: Path,
    python_executable: Path,
    symbols: Sequence[str],
) -> EvidenceBundle:
    """Capture HFM broker activity exports required by tick-volume evidence."""

    try:
        verifier_name, code_path, test_path, test_arguments = _LIVE_ACTIVITY_BROKER_CAPTURE_TARGETS[requirement_id]
    except KeyError as exc:
        raise ValueError(f"unsupported live activity broker requirement: {requirement_id}") from exc
    root = workspace_root.resolve()
    output = output_directory.resolve()
    _require_inside(output, root, "evidence output")
    source_snapshot = _snapshot_hfm_inputs(
        destination=output,
        requirement_id=requirement_id,
        bridge_root=bridge_root.resolve(),
        symbols=symbols,
        timeframes=("M1",),
        include_combined_m1=True,
        include_ticks=True,
    )
    broker_bundle = _bundle_data_subject(output, requirement_id, tuple(sorted(source_snapshot.iterdir())))
    verification = globals()[verifier_name](bridge_root=source_snapshot, symbols=tuple(symbols))
    return capture_verified_requirement(
        workspace_root=root,
        requirement_id=requirement_id,
        verification=verification,
        code_subject=root / code_path,
        test_file=root / test_path,
        output_directory=output,
        python_executable=python_executable,
        broker_subjects=(broker_bundle,),
        test_arguments=test_arguments,
    )


def capture_verified_requirement(
    *,
    workspace_root: Path,
    requirement_id: str,
    verification: Mapping[str, object],
    code_subject: Path,
    test_file: Path,
    output_directory: Path,
    python_executable: Path,
    data_subjects: Sequence[Path] = (),
    broker_subjects: Sequence[Path] = (),
    test_arguments: Sequence[str] = (),
) -> EvidenceBundle:
    """Capture checked code, test, runtime, and optional data evidence."""

    root = workspace_root.resolve()
    output = output_directory.resolve()
    _require_inside(output, root, "evidence output")
    if verification.get("requirement_id") != requirement_id or verification.get("status") != "PASS":
        raise ValueError(f"{requirement_id} verification did not produce a passing receipt")
    observed_at = datetime.now(timezone.utc)
    stamp = observed_at.strftime("%Y%m%dT%H%M%S%fZ")
    output.mkdir(parents=True, exist_ok=True)
    runtime_receipt = output / f"{requirement_id.lower()}_{stamp}_runtime_receipt.json"
    _write_json(runtime_receipt, {
        "schema_version": 1,
        "requirement_id": requirement_id,
        "status": "PASS",
        "observed_at": observed_at.isoformat(),
        "verification": verification,
    })
    completed = _run_pytest(python_executable, root, test_file, test_arguments)
    test_receipt = output / f"{requirement_id.lower()}_{stamp}_test_receipt.json"
    _write_json(test_receipt, {
        "schema_version": 1,
        "requirement_id": requirement_id,
        "status": "PASS" if completed.returncode == 0 else "FAIL",
        "observed_at": observed_at.isoformat(),
        "command": tuple(completed.args),
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    })
    if completed.returncode != 0:
        raise RuntimeError(f"{requirement_id} test failed; evidence was not captured")
    subjects = [
        EvidenceSubject(EvidenceKind.CODE, code_subject),
        EvidenceSubject(EvidenceKind.TEST, test_receipt),
        EvidenceSubject(EvidenceKind.RUNTIME, runtime_receipt),
    ]
    subjects.extend(EvidenceSubject(EvidenceKind.DATA, path) for path in data_subjects)
    subjects.extend(EvidenceSubject(EvidenceKind.BROKER, path) for path in broker_subjects)
    return write_evidence_bundle(
        workspace_root=root,
        requirement_id=requirement_id,
        subjects=tuple(subjects),
        output_directory=output,
        observed_at=observed_at,
    )


def _run_pytest(
    python_executable: Path,
    workspace_root: Path,
    test_file: Path,
    test_arguments: Sequence[str] = (),
) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(workspace_root / "clean_build")
    return subprocess.run(
        (str(python_executable), "-m", "pytest", "-q", str(test_file), *test_arguments),
        cwd=workspace_root,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def _require_inside(path: Path, root: Path, label: str) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label} must remain inside workspace") from exc


def _file_digest(path: Path) -> str:
    return f"sha256:{sha256(path.read_bytes()).hexdigest()}"


def _snapshot_sqlite_database(source: Path, destination: Path) -> None:
    """Freeze a coherent SQLite evidence subject while the live writer continues."""

    if destination.exists():
        raise FileExistsError(destination)
    reader = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    writer = sqlite3.connect(destination)
    try:
        reader.backup(writer)
        writer.commit()
    finally:
        writer.close()
        reader.close()


def _c007_payload_from_quality_events(database: Path) -> Mapping[str, object]:
    """Build the C007 verifier payload from immutable live-observation events."""

    connection = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT symbol, observed_at, payload FROM tick_quality_events ORDER BY observed_at,event_id"
        ).fetchall()
    finally:
        connection.close()
    results = []
    for symbol, observed_at, payload_text in rows:
        payload = json.loads(str(payload_text))
        status = str(payload.get("quality") or payload.get("poll_status") or "")
        if status not in {"VALID", "STALE"}:
            continue
        tick = payload.get("tick")
        tick_at = tick.get("timestamp") if isinstance(tick, Mapping) else None
        results.append(
            {
                "symbol": str(symbol),
                "status": status,
                "reason": str(payload.get("quality_reason") or payload.get("reason") or "UNSPECIFIED"),
                "observed_at": str(observed_at),
                "tick_at": tick_at,
                "source_path": payload.get("source_path"),
            }
        )
    if not results:
        raise ValueError("C007 requires persisted VALID or STALE tick observations")
    return {"results": tuple(results)}


def _snapshot_hfm_inputs(
    *,
    destination: Path,
    requirement_id: str,
    bridge_root: Path,
    symbols: Sequence[str],
    timeframes: Sequence[str] = _HFM_SNAPSHOT_TIMEFRAMES,
    include_combined_m1: bool = False,
    include_ticks: bool = False,
) -> Path:
    """Copy the exact bar files used by a rolling HFM verifier before it runs."""

    source_root = bridge_root.resolve()
    if not source_root.is_dir():
        raise FileNotFoundError(source_root)
    destination.mkdir(parents=True, exist_ok=True)
    snapshot = destination / f"{requirement_id.lower()}_hfm_inputs_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}"
    snapshot.mkdir()
    sources = [source_root / "symbols.csv"]
    for symbol in symbols:
        for timeframe in timeframes:
            sources.append(source_root / f"rates_{symbol}_{timeframe}.csv")
            if include_combined_m1 and timeframe == "M1":
                sources.extend(
                    candidate
                    for candidate in (
                        source_root / f"deephistory_{symbol}_M1.csv",
                        source_root / f"rates_{symbol}_M1_HISTORY.csv",
                    )
                    if candidate.is_file()
                )
        if include_ticks:
            sources.append(source_root / f"tick_{symbol}.txt")
    for source in sources:
        if not source.is_file():
            raise FileNotFoundError(source)
        copyfile(source, snapshot / source.name)
    return snapshot


def _snapshot_broker_exports(
    *,
    destination: Path,
    requirement_id: str,
    bridge_root: Path,
) -> Path:
    """Freeze the exact historical HFM order and deal exports used as broker proof."""

    source_root = bridge_root.resolve()
    if not source_root.is_dir():
        raise FileNotFoundError(source_root)
    destination.mkdir(parents=True, exist_ok=True)
    snapshot = destination / f"{requirement_id.lower()}_broker_exports_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}"
    snapshot.mkdir()
    for name in ("history_orders.csv", "deals.csv"):
        source = source_root / name
        if not source.is_file():
            raise FileNotFoundError(source)
        copyfile(source, snapshot / name)
    return snapshot


def _snapshot_broker_tick_exports(
    *,
    destination: Path,
    requirement_id: str,
    bridge_root: Path,
    symbols: Sequence[str],
) -> Path:
    """Freeze the exact HFM tick exports that carry broker and UTC clocks."""

    source_root = bridge_root.resolve()
    if not source_root.is_dir():
        raise FileNotFoundError(source_root)
    destination.mkdir(parents=True, exist_ok=True)
    snapshot = destination / f"{requirement_id.lower()}_broker_ticks_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}"
    snapshot.mkdir()
    for symbol in symbols:
        source = source_root / f"tick_{symbol}.txt"
        if not source.is_file():
            raise FileNotFoundError(source)
        copyfile(source, snapshot / source.name)
    return snapshot


def _bundle_data_subject(destination: Path, requirement_id: str, paths: Sequence[Path]) -> Path:
    """Create one immutable data subject when a requirement has several artifacts."""

    if not paths:
        raise ValueError("at least one data artifact is required")
    destination.mkdir(parents=True, exist_ok=True)
    bundle = destination / f"{requirement_id.lower()}_data_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.zip"
    with ZipFile(bundle, "x", compression=ZIP_DEFLATED) as archive:
        for index, path in enumerate(paths):
            resolved = path.resolve()
            if not resolved.is_file():
                raise FileNotFoundError(resolved)
            archive.write(resolved, arcname=f"{index:02d}_{resolved.name}")
    return bundle


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument(
        "--requirement",
        choices=(
            "C006", "C007", "C008", "C009", "C011", "C012", "C013", "C014",
            "C015", "C016", "C017", "C018", "C019", "C020",
            "C021", "C022", "C023", "C024", "C025",
            "C026", "C027", "C028", "C029", "C030",
            "C031", "C034", "C035", "C037", "C038", "C039", "C040",
            "C032", "C033", "C041",
            "C042", "C043", "C044", "C045", "C046", "C047", "C048", "C049", "C050",
            "C052", "C053", "C055", "C056", "C057", "C058", "C059", "C060",
            "C061", "C062", "C063", "C064", "C065", "C066", "C067", "C068",
            "C069", "C070", "C071", "C072", "C073", "C074", "C075", "C076",
            "C077", "C078", "C079", "C080", "C081", "C082", "C083", "C084",
            "C085", "C086", "C087", "C088", "C089", "C090", "C091", "C092",
            "C093", "C094", "C095", "C096", "C097", "C098", "C099", "C100", "C101", "C102", "C103", "C104", "C105",
        ),
        default="C006",
    )
    parser.add_argument("--bridge-root", type=Path)
    parser.add_argument("--tick-database", type=Path)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--chart-directory", type=Path)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--symbol", default="XAUUSD")
    parser.add_argument("--symbols", nargs="+", default=("XAUUSD", "UK100", "USA100", "USA500", "USA30"))
    parser.add_argument("--timeframe", default="M5")
    parser.add_argument("--python-executable", type=Path, default=Path(sys.executable))
    args = parser.parse_args()
    if args.requirement == "C006":
        if args.tick_database is None:
            parser.error("--tick-database is required for C006")
        bundle = capture_c006_live_ticks(
            workspace_root=args.workspace_root,
            tick_database=args.tick_database,
            output_directory=args.output_directory,
            python_executable=args.python_executable,
        )
    elif args.requirement == "C007":
        if args.tick_database is None:
            parser.error("--tick-database is required for C007")
        bundle = capture_c007_live_tick_quality(
            workspace_root=args.workspace_root,
            tick_database=args.tick_database,
            output_directory=args.output_directory,
            python_executable=args.python_executable,
            maximum_age_seconds=60.0,
        )
    elif args.requirement == "C008":
        if args.bridge_root is None or args.tick_database is None:
            parser.error("--bridge-root and --tick-database are required for C008")
        bundle = capture_c008_market_snapshot(
            workspace_root=args.workspace_root,
            bridge_root=args.bridge_root,
            tick_database=args.tick_database,
            output_directory=args.output_directory,
            python_executable=args.python_executable,
            symbols=("XAUUSD", "UK100", "USA100", "USA500", "USA30"),
        )
    elif args.requirement == "C009":
        if args.bridge_root is None or args.tick_database is None:
            parser.error("--bridge-root and --tick-database are required for C009")
        bundle = capture_c009_market_state(
            workspace_root=args.workspace_root,
            bridge_root=args.bridge_root,
            tick_database=args.tick_database,
            output_directory=args.output_directory,
            python_executable=args.python_executable,
            symbols=("XAUUSD", "UK100", "USA100", "USA500", "USA30"),
        )
    elif args.requirement in _LIVE_INTELLIGENCE_CAPTURE_TARGETS:
        if args.bridge_root is None:
            parser.error("--bridge-root is required for C015-C020")
        bundle = capture_live_intelligence_requirement(
            workspace_root=args.workspace_root,
            requirement_id=args.requirement,
            bridge_root=args.bridge_root,
            output_directory=args.output_directory,
            python_executable=args.python_executable,
            symbols=tuple(args.symbols),
        )
    elif args.requirement in _LIVE_GEOMETRY_CAPTURE_TARGETS:
        if args.bridge_root is None:
            parser.error("--bridge-root is required for C021-C025")
        if args.requirement == "C022" and args.database is None:
            parser.error("--database is required for C022")
        bundle = capture_live_geometry_requirement(
            workspace_root=args.workspace_root,
            requirement_id=args.requirement,
            bridge_root=args.bridge_root,
            output_directory=args.output_directory,
            python_executable=args.python_executable,
            symbols=tuple(args.symbols),
            database=args.database,
        )
    elif args.requirement in _LIVE_TICK_INTELLIGENCE_CAPTURE_TARGETS:
        if args.tick_database is None:
            parser.error("--tick-database is required for C026-C030")
        bundle = capture_live_tick_intelligence_requirement(
            workspace_root=args.workspace_root,
            requirement_id=args.requirement,
            tick_database=args.tick_database,
            output_directory=args.output_directory,
            python_executable=args.python_executable,
        )
    elif args.requirement in _LIVE_ANALYTICS_CAPTURE_TARGETS:
        if args.bridge_root is None:
            parser.error("--bridge-root is required for C031, C034-C035, and C037-C040")
        bundle = capture_live_analytics_requirement(
            workspace_root=args.workspace_root,
            requirement_id=args.requirement,
            bridge_root=args.bridge_root,
            output_directory=args.output_directory,
            python_executable=args.python_executable,
            symbols=tuple(args.symbols),
        )
    elif args.requirement in _LIVE_BROKER_CAPTURE_TARGETS:
        if args.bridge_root is None or args.database is None:
            parser.error("--bridge-root and --database are required for C032-C033")
        bundle = capture_live_broker_requirement(
            workspace_root=args.workspace_root,
            requirement_id=args.requirement,
            bridge_root=args.bridge_root,
            database=args.database,
            output_directory=args.output_directory,
            python_executable=args.python_executable,
            symbols=tuple(args.symbols),
        )
    elif args.requirement in _LIVE_SESSION_BROKER_CAPTURE_TARGETS:
        if args.bridge_root is None:
            parser.error("--bridge-root is required for C041")
        bundle = capture_live_session_broker_requirement(
            workspace_root=args.workspace_root,
            requirement_id=args.requirement,
            bridge_root=args.bridge_root,
            output_directory=args.output_directory,
            python_executable=args.python_executable,
            symbols=tuple(args.symbols),
        )
    elif args.requirement in _LIVE_ACTIVITY_BROKER_CAPTURE_TARGETS:
        if args.bridge_root is None:
            parser.error("--bridge-root is required for C053")
        bundle = capture_live_activity_broker_requirement(
            workspace_root=args.workspace_root,
            requirement_id=args.requirement,
            bridge_root=args.bridge_root,
            output_directory=args.output_directory,
            python_executable=args.python_executable,
            symbols=tuple(args.symbols),
        )
    elif args.requirement in _LIVE_KNOWLEDGE_CAPTURE_TARGETS:
        bundle = capture_knowledge_library_requirement(
            workspace_root=args.workspace_root,
            requirement_id=args.requirement,
            output_directory=args.output_directory,
            python_executable=args.python_executable,
        )
    elif args.requirement in _LIVE_PATTERN_OUTCOME_CAPTURE_TARGETS:
        if args.bridge_root is None:
            parser.error("--bridge-root is required for C077-C078")
        bundle = capture_pattern_outcome_requirement(
            workspace_root=args.workspace_root,
            requirement_id=args.requirement,
            bridge_root=args.bridge_root,
            output_directory=args.output_directory,
            python_executable=args.python_executable,
            symbols=tuple(args.symbols),
        )
    elif args.requirement in _LIVE_HISTORICAL_STATE_CAPTURE_TARGETS:
        if args.bridge_root is None:
            parser.error("--bridge-root is required for C079-C084")
        bundle = capture_historical_state_requirement(
            workspace_root=args.workspace_root,
            requirement_id=args.requirement,
            bridge_root=args.bridge_root,
            output_directory=args.output_directory,
            python_executable=args.python_executable,
            symbols=tuple(args.symbols),
        )
    elif args.requirement in _LIVE_OUTCOME_METRIC_CAPTURE_TARGETS:
        if args.bridge_root is None or args.tick_database is None:
            parser.error("--bridge-root and --tick-database are required for C085-C092")
        bundle = capture_live_outcome_metric_requirement(
            workspace_root=args.workspace_root,
            requirement_id=args.requirement,
            bridge_root=args.bridge_root,
            tick_database=args.tick_database,
            output_directory=args.output_directory,
            python_executable=args.python_executable,
            symbols=tuple(args.symbols),
        )
    elif args.requirement in _LIVE_RESEARCH_CAPTURE_TARGETS:
        if args.bridge_root is None:
            parser.error("--bridge-root is required for C093-C105")
        bundle = capture_live_research_requirement(
            workspace_root=args.workspace_root,
            requirement_id=args.requirement,
            bridge_root=args.bridge_root,
            output_directory=args.output_directory,
            python_executable=args.python_executable,
            symbols=tuple(args.symbols),
        )
    elif args.requirement in {"C011", "C012", "C013"}:
        if args.bridge_root is None or args.chart_directory is None:
            parser.error("--bridge-root and --chart-directory are required for C011-C013")
        chart_arguments = {
            "workspace_root": args.workspace_root,
            "bridge_root": args.bridge_root,
            "output_directory": args.output_directory,
            "chart_directory": args.chart_directory,
            "python_executable": args.python_executable,
            "symbol": args.symbol,
        }
        if args.requirement == "C011":
            bundle = capture_c011_live_chart(
                **chart_arguments,
                timeframe=args.timeframe,
            )
        elif args.requirement == "C012":
            bundle = capture_c012_chart_pack(**chart_arguments)
        else:
            bundle = capture_c013_chart_annotations(
                **chart_arguments,
                timeframe=args.timeframe,
            )
    else:
        if args.bridge_root is None or args.chart_directory is None or args.database is None:
            parser.error("--bridge-root, --chart-directory, and --database are required for C014")
        bundle = capture_c014_chart_history(
            workspace_root=args.workspace_root,
            bridge_root=args.bridge_root,
            database=args.database,
            output_directory=args.output_directory,
            chart_directory=args.chart_directory,
            python_executable=args.python_executable,
            symbol=args.symbol,
            timeframe=args.timeframe,
        )
    print(json.dumps({
        "envelope": str(bundle.envelope),
        "manifest": str(bundle.manifest),
        "evidence_ids": bundle.evidence_ids,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
