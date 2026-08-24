"""Deterministic parallel research over isolated immutable SQLite shards."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
from typing import Mapping, Sequence

from .research_orchestrator import build_candidate_universes
from .research_pipeline import ResearchPolicy, run_research, sweep_specs
from .snapshot import RESEARCH_TIMEFRAMES
from .store import EvidenceStore


DEFAULT_POLICY = ResearchPolicy(200, 50, 50, 900, 100, 50, "HFM_EXECUTABLE_TICKS")
DEFAULT_SYMBOLS = ("XAUUSD", "UK100", "USA100", "USA500", "USA30")
DEFAULT_HORIZONS = (300, 900)


def partition_universes(rows: Sequence[object], shard_index: int, shard_count: int) -> tuple[object, ...]:
    if shard_count <= 0 or not 0 <= shard_index < shard_count:
        raise ValueError("invalid shard index or count")
    ordered = tuple(sorted(rows, key=lambda row: (row.symbol, row.timeframe, row.universe_id)))
    return tuple(row for index, row in enumerate(ordered) if index % shard_count == shard_index)


def study_identity(
    universes: Sequence[object],
    *,
    directions: Sequence[str],
    horizons: Sequence[int],
    policy: ResearchPolicy,
    version: int,
    cost_return: float,
    cost_evidence_status: str,
    cost_source_ids: Sequence[str],
) -> str:
    payload = {
        "universe_ids": tuple(sorted(row.universe_id for row in universes)),
        "directions": tuple(directions),
        "horizons": tuple(horizons),
        "policy": asdict(policy),
        "version": version,
        "cost_return": cost_return,
        "cost_evidence_status": cost_evidence_status,
        "cost_source_ids": tuple(cost_source_ids),
    }
    return _digest(payload)


def experiment_count(
    universes: Sequence[object],
    *,
    direction_count: int,
    horizon_count: int,
) -> int:
    if direction_count <= 0 or horizon_count <= 0:
        raise ValueError("direction and horizon counts must be positive")
    return sum(len(row.condition_sets) for row in universes) * direction_count * horizon_count


def run_shard(
    *,
    source_database: Path,
    shard_database: Path,
    output: Path,
    symbols: tuple[str, ...],
    timeframes: tuple[str, ...],
    horizons: tuple[int, ...],
    policy: ResearchPolicy,
    shard_index: int,
    shard_count: int,
    version: int,
    cost_return: float = 0.0,
    cost_evidence_status: str = "INCOMPLETE_UNMEASURED",
    cost_source_ids: tuple[str, ...] = (),
) -> Mapping[str, object]:
    source = EvidenceStore(source_database)
    try:
        cases = tuple(
            case
            for symbol in symbols
            for case in source.load_historical_cases(symbol)
        )
    finally:
        source.close()
    universes, universe_manifest = build_candidate_universes(
        tuple(case[0] for case in cases), symbols, timeframes,
    )
    if len(universes) != len(symbols) * len(timeframes):
        raise ValueError("candidate universe is incomplete")
    selected = partition_universes(universes, shard_index, shard_count)
    directions = ("BUY", "SELL")
    global_count = experiment_count(
        universes,
        direction_count=len(directions),
        horizon_count=len(horizons),
    )
    identity = study_identity(
        universes,
        directions=directions,
        horizons=horizons,
        policy=policy,
        version=version,
        cost_return=cost_return,
        cost_evidence_status=cost_evidence_status,
        cost_source_ids=cost_source_ids,
    )
    specs = tuple(
        spec
        for universe in selected
        for spec in sweep_specs(
            families=("RAW_MARKET_STATE",),
            symbols=(universe.symbol,),
            directions=directions,
            timeframes=(universe.timeframe,),
            sessions=("ANY",),
            regimes=("ANY",),
            horizons=horizons,
            condition_sets=universe.condition_sets,
            cost_return=cost_return,
            cost_evidence_status=cost_evidence_status,
            cost_source_ids=cost_source_ids,
            version=version,
        )
    )
    expected_shard_count = experiment_count(
        selected,
        direction_count=len(directions),
        horizon_count=len(horizons),
    )
    if len(specs) != expected_shard_count:
        raise ValueError("shard experiment enumeration is incomplete")
    shard = EvidenceStore(shard_database)
    try:
        report = run_research(
            store=shard,
            specs=specs,
            cases=cases,
            policy=policy,
            multiple_test_count=global_count,
            run_identity=identity,
        )
        integrity = shard.integrity()
        shard.checkpoint()
    finally:
        shard.close()
    result = {
        "schema_version": 1,
        "study_id": identity,
        "shard_index": shard_index,
        "shard_count": shard_count,
        "global_experiment_count": global_count,
        "shard_experiment_count": expected_shard_count,
        "candidate_universe_count": len(universes),
        "candidate_condition_set_count": universe_manifest["total_condition_sets"],
        "selected_universe_ids": tuple(row.universe_id for row in selected),
        "symbols": symbols,
        "timeframes": timeframes,
        "horizons": horizons,
        "policy": asdict(policy),
        "version": version,
        "cost_return": cost_return,
        "cost_evidence_status": cost_evidence_status,
        "cost_source_ids": cost_source_ids,
        "database_integrity": integrity,
        "report": asdict(report),
    }
    _write_json(output, result)
    return result


def combine_shards(
    *,
    destination_database: Path,
    shard_databases: Sequence[Path],
    shard_reports: Sequence[Path],
    output: Path,
) -> Mapping[str, object]:
    reports = tuple(json.loads(path.read_text(encoding="utf-8")) for path in shard_reports)
    if not reports or len(reports) != len(shard_databases):
        raise ValueError("every shard database requires one report")
    shard_count = reports[0]["shard_count"]
    indexes = {row["shard_index"] for row in reports}
    if len(reports) != shard_count or indexes != set(range(shard_count)):
        raise ValueError("shard report set is incomplete")
    invariant_fields = (
        "study_id", "shard_count", "global_experiment_count", "policy",
        "version", "symbols", "timeframes", "horizons", "cost_return",
        "cost_evidence_status", "cost_source_ids",
    )
    for field in invariant_fields:
        if len({_digest(row[field]) for row in reports}) != 1:
            raise ValueError(f"shard invariant mismatch: {field}")
    run_ids = {row["report"]["run_id"] for row in reports}
    if len(run_ids) != 1:
        raise ValueError("shards do not share one research run")
    if any(not row["report"]["complete"] for row in reports):
        raise ValueError("cannot combine incomplete shard")
    experiment_ids = tuple(
        experiment_id
        for row in sorted(reports, key=lambda item: item["shard_index"])
        for experiment_id in row["report"]["experiment_ids"]
    )
    global_count = reports[0]["global_experiment_count"]
    if len(experiment_ids) != global_count or len(set(experiment_ids)) != global_count:
        raise ValueError("shards do not cover the global experiment family exactly once")
    destination = EvidenceStore(destination_database)
    try:
        merge_counts = tuple(
            destination.merge_research_database(path)
            for path in shard_databases
        )
        integrity = destination.integrity()
        destination.checkpoint()
    finally:
        destination.close()
    count_fields = (
        "planned", "tested", "historical_pass", "no_edge", "insufficient",
        "blocked_fidelity", "blocked_cost_evidence", "no_observations",
        "graveyard_entries",
    )
    combined_report = {
        field: sum(row["report"][field] for row in reports)
        for field in count_fields
    }
    combined_report.update({
        "run_id": next(iter(run_ids)),
        "complete": True,
        "experiment_ids": experiment_ids,
    })
    if combined_report["planned"] != global_count or combined_report["tested"] != global_count:
        raise ValueError("combined report count mismatch")
    result = {
        "schema_version": 1,
        "study_id": reports[0]["study_id"],
        "shard_count": shard_count,
        "global_experiment_count": global_count,
        "source_reports": tuple(str(path) for path in shard_reports),
        "source_databases": tuple(str(path) for path in shard_databases),
        "merge_counts": merge_counts,
        "database_integrity": integrity,
        "policy": reports[0]["policy"],
        "version": reports[0]["version"],
        "cost_evidence_status": reports[0]["cost_evidence_status"],
        "report": combined_report,
        "promotion_status": "BLOCKED_PENDING_EVIDENCE_REVIEW",
    }
    _write_json(output, result)
    return result


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def _digest(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    shard = subparsers.add_parser("run-shard")
    shard.add_argument("--source-database", type=Path, required=True)
    shard.add_argument("--shard-database", type=Path, required=True)
    shard.add_argument("--output", type=Path, required=True)
    shard.add_argument("--shard-index", type=int, required=True)
    shard.add_argument("--shard-count", type=int, required=True)
    shard.add_argument("--version", type=int, default=4)
    shard.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    shard.add_argument("--timeframes", nargs="+", default=RESEARCH_TIMEFRAMES)
    shard.add_argument("--horizons", nargs="+", type=int, default=DEFAULT_HORIZONS)
    combine = subparsers.add_parser("combine")
    combine.add_argument("--destination-database", type=Path, required=True)
    combine.add_argument("--shard-databases", nargs="+", type=Path, required=True)
    combine.add_argument("--shard-reports", nargs="+", type=Path, required=True)
    combine.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "run-shard":
        run_shard(
            source_database=args.source_database,
            shard_database=args.shard_database,
            output=args.output,
            symbols=tuple(args.symbols),
            timeframes=tuple(args.timeframes),
            horizons=tuple(args.horizons),
            policy=DEFAULT_POLICY,
            shard_index=args.shard_index,
            shard_count=args.shard_count,
            version=args.version,
        )
        return 0
    combine_shards(
        destination_database=args.destination_database,
        shard_databases=tuple(args.shard_databases),
        shard_reports=tuple(args.shard_reports),
        output=args.output,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
