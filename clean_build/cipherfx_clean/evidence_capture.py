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
import sqlite3
import subprocess
import sys
from typing import Mapping, Sequence

from .compliance import EvidenceKind
from .requirement_runtime import (
    verify_c006_raw_tick_library,
    verify_c008_hfm_candle_builder,
    verify_c009_hfm_market_states,
    verify_c011_hfm_live_chart,
    verify_c012_hfm_chart_pack,
    verify_c013_hfm_annotations,
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


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument(
        "--requirement",
        choices=("C006", "C008", "C009", "C011", "C012", "C013"),
        default="C006",
    )
    parser.add_argument("--bridge-root", type=Path)
    parser.add_argument("--tick-database", type=Path)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--chart-directory", type=Path)
    parser.add_argument("--symbol", default="XAUUSD")
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
    else:
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
    print(json.dumps({
        "envelope": str(bundle.envelope),
        "manifest": str(bundle.manifest),
        "evidence_ids": bundle.evidence_ids,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
