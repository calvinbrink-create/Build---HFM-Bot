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
    verify_c032_slippage_intelligence,
    verify_c033_latency_intelligence,
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
        timeframes = ("M1",) if snapshot_kind == "spread" else _HFM_RESEARCH_TIMEFRAMES
        snapshot = _snapshot_hfm_inputs(
            destination=output,
            requirement_id=requirement_id,
            bridge_root=source_root,
            symbols=symbols,
            timeframes=timeframes,
            include_combined_m1=snapshot_kind == "spread",
            include_ticks=snapshot_kind == "spread",
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
            "C006", "C008", "C009", "C011", "C012", "C013", "C014",
            "C015", "C016", "C017", "C018", "C019", "C020",
            "C021", "C022", "C023", "C024", "C025",
            "C026", "C027", "C028", "C029", "C030",
            "C031", "C034", "C035", "C037", "C038", "C039", "C040",
            "C032", "C033",
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
