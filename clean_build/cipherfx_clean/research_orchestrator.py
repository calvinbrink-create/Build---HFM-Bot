"""Deterministic HFM research pipeline from source audit to release verification.

The pipeline is restart-safe and never activates live execution. A blocked
certification is a valid pipeline result and remains visibly blocked.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import Mapping

from .candidate_universe import CandidateUniverse, build_candidate_universe
from .hfm_audit import build_report
from .historical_memory import HistoricalState
from .populate_memory import populate
from .release import EdgeCertification, build_release_bundle, verify_release_bundle
from .research_pipeline import ResearchPolicy, run_research, sweep_specs
from .snapshot import RESEARCH_TIMEFRAMES
from .store import EvidenceStore


@dataclass(frozen=True)
class PipelineConfiguration:
    source_root: str
    repository_root: str
    database: str
    evidence_directory: str
    symbols: tuple[str, ...]
    timeframes: tuple[str, ...]
    horizons: tuple[int, ...]
    observed_at_utc: str
    stride_m1_bars: int
    lookback_bars: int
    additional_cost_return: float
    cost_evidence_status: str
    cost_source_ids: tuple[str, ...]
    research_policy: ResearchPolicy
    candidate_quantiles: tuple[float, ...] = (.25, .50, .75)
    candidate_interaction_order: int = 2
    schema_version: int = 1


@dataclass(frozen=True)
class PipelineReport:
    pipeline_id: str
    status: str
    reasons: tuple[str, ...]
    source_audit: str
    population: str
    candidate_universes: str
    research: str
    release_manifest: str
    release_verification_status: str
    release_verification_reasons: tuple[str, ...]


def run_pipeline(configuration: PipelineConfiguration) -> PipelineReport:
    repository_root = Path(configuration.repository_root).resolve()
    source_root = Path(configuration.source_root).resolve()
    evidence = Path(configuration.evidence_directory).resolve()
    database = Path(configuration.database).resolve()
    if not source_root.is_dir() or not repository_root.is_dir():
        raise FileNotFoundError("source and repository roots must exist")
    if not evidence.is_relative_to(repository_root) or not database.is_relative_to(repository_root):
        raise ValueError("pipeline outputs must remain inside repository root")
    observed_at = datetime.fromisoformat(configuration.observed_at_utc).astimezone(timezone.utc)
    evidence.mkdir(parents=True, exist_ok=True)

    config_path = evidence / "pipeline_configuration.json"
    _write_json(config_path, asdict(configuration))
    audit_path = evidence / "hfm_dataset_audit_pipeline.json"
    _write_json(audit_path, build_report(source_root, configuration.symbols, observed_at))
    population_path = evidence / "historical_population_pipeline.json"
    population_report = populate(
        export_root=source_root,
        database=database,
        symbols=configuration.symbols,
        observed_at=observed_at,
        stride=configuration.stride_m1_bars,
        lookback=configuration.lookback_bars,
    )
    _write_json(population_path, population_report)

    store = EvidenceStore(database)
    try:
        cases = tuple(
            case
            for symbol in configuration.symbols
            for case in store.load_historical_cases(symbol)
        )
        universe_rows, universes = build_candidate_universes(
            tuple(case[0] for case in cases),
            configuration.symbols,
            configuration.timeframes,
            quantiles=configuration.candidate_quantiles,
            interaction_order=configuration.candidate_interaction_order,
        )
        universe_path = evidence / "candidate_universes.json"
        _write_json(universe_path, universes)
        for universe in universe_rows:
            store.write_candidate_universe(universe)
        specs = tuple(
            spec
            for universe in universe_rows
            for spec in sweep_specs(
                families=("RAW_MARKET_STATE",),
                symbols=(universe.symbol,),
                directions=("BUY", "SELL"),
                timeframes=(universe.timeframe,),
                sessions=("ANY",),
                regimes=("ANY",),
                horizons=configuration.horizons,
                condition_sets=universe.condition_sets,
                cost_return=configuration.additional_cost_return,
                cost_evidence_status=configuration.cost_evidence_status,
                cost_source_ids=configuration.cost_source_ids,
            )
        )
        research = run_research(
            store=store,
            specs=specs,
            cases=cases,
            policy=configuration.research_policy,
        )
        research_path = evidence / "research_pipeline.json"
        _write_json(research_path, {
            "report": asdict(research),
            "policy": asdict(configuration.research_policy),
            "case_count": len(cases),
            "experiment_count": len(specs),
            "candidate_universe_count": len(universe_rows),
            "candidate_source": "DISCOVERY_PERIOD_EMPIRICAL_ALL_SINGLES_AND_CROSS_FEATURE_PAIRS",
            "cost_evidence_status": configuration.cost_evidence_status,
            "cost_source_ids": configuration.cost_source_ids,
        })
        code_path = evidence / "clean_code_manifest.json"
        _write_json(code_path, code_manifest(repository_root / "clean_build"))
        certifications: tuple[EdgeCertification, ...] = ()
        release_path = evidence / "research_release.json"
        build_release_bundle(
            root=repository_root,
            artifacts={
                "CONFIGURATION": config_path,
                "HFM_SOURCE_AUDIT": audit_path,
                "HISTORICAL_POPULATION": population_path,
                "CANDIDATE_UNIVERSES": universe_path,
                "RESEARCH_REPORT": research_path,
                "CODE_MANIFEST": code_path,
            },
            certifications=certifications,
            output=release_path,
            store=store,
            created_at=observed_at,
        )
        verification = verify_release_bundle(repository_root, release_path)
        reasons = ["NO_CERTIFIED_EDGE"]
        if research.historical_pass == 0:
            reasons.append("NO_HISTORICALLY_VALIDATED_EDGE")
        if configuration.research_policy.required_fidelity != "HFM_EXECUTABLE_TICKS":
            reasons.append("EXECUTABLE_TICK_FIDELITY_NOT_REQUIRED_BY_POLICY")
        if configuration.cost_evidence_status != "COMPLETE":
            reasons.append("MEASURED_COST_EVIDENCE_INCOMPLETE")
        pipeline_id = _digest((asdict(configuration), asdict(research), universes))
        report = PipelineReport(
            pipeline_id,
            "BLOCKED" if reasons else "PASS",
            tuple(reasons),
            str(audit_path.relative_to(repository_root)),
            str(population_path.relative_to(repository_root)),
            str(universe_path.relative_to(repository_root)),
            str(research_path.relative_to(repository_root)),
            str(release_path.relative_to(repository_root)),
            verification.status,
            verification.reasons,
        )
        _write_json(evidence / "pipeline_report.json", asdict(report))
        store.checkpoint()
        return report
    finally:
        store.close()


def code_manifest(root: Path) -> Mapping[str, object]:
    rows = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or "evidence" in path.parts:
            continue
        data = path.read_bytes()
        rows.append({
            "path": str(path.relative_to(root)),
            "sha256": sha256(data).hexdigest(),
            "size": len(data),
        })
    return {
        "root": root.name,
        "files": rows,
        "manifest_digest": _digest(rows),
    }


def build_candidate_universes(
    states: tuple[HistoricalState, ...],
    symbols: tuple[str, ...],
    timeframes: tuple[str, ...],
    *,
    quantiles: tuple[float, ...] = (.25, .50, .75),
    interaction_order: int = 2,
) -> tuple[tuple[CandidateUniverse, ...], Mapping[str, object]]:
    universes: list[CandidateUniverse] = []
    rows = []
    for symbol in symbols:
        selected = tuple(row for row in states if row.symbol == symbol)
        if len(selected) < 100:
            rows.append({"symbol": symbol, "status": "INSUFFICIENT_DISCOVERY_STATES"})
            continue
        ordered = tuple(sorted(selected, key=lambda row: (row.observed_at, row.state_id)))
        cutoff_index = min(len(ordered) - 1, max(100, int(len(ordered) * .70)))
        discovery_end = ordered[cutoff_index].observed_at
        for timeframe in timeframes:
            universe = build_candidate_universe(
                ordered,
                symbol=symbol,
                timeframe=timeframe,
                discovery_end=discovery_end,
                quantiles=quantiles,
                interaction_order=interaction_order,
                minimum_samples=100,
            )
            universes.append(universe)
            rows.append({
                "universe_id": universe.universe_id,
                "symbol": symbol,
                "timeframe": timeframe,
                "discovery_start": universe.discovery_start.isoformat(),
                "discovery_end": universe.discovery_end.isoformat(),
                "discovery_samples": universe.discovery_samples,
                "feature_schema_version": universe.feature_schema_version,
                "condition_set_count": len(universe.condition_sets),
                "source_digest": universe.source_digest,
                "feature_names": universe.feature_names,
                "interaction_contract": universe.interaction_contract,
                "status": "GENERATED_NOT_PROMOTED",
            })
    return tuple(universes), {
        "schema_version": 2,
        "quantiles": quantiles,
        "interaction_order": interaction_order,
        "universe_count": len(universes),
        "total_condition_sets": sum(len(row.condition_sets) for row in universes),
        "universes": rows,
    }


def _write_json(path: Path, payload: object) -> None:
    data = json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n"
    if path.is_file() and path.read_text(encoding="utf-8") == data:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data, encoding="utf-8")


def _digest(value: object) -> str:
    return sha256(json.dumps(value, default=str, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--repository-root", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--evidence-directory", required=True)
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--timeframes", nargs="+", default=RESEARCH_TIMEFRAMES)
    parser.add_argument("--horizons", nargs="+", type=int, default=(300, 900))
    parser.add_argument("--observed-at", default=datetime.now(timezone.utc).isoformat())
    parser.add_argument("--stride", type=int, default=60)
    parser.add_argument("--lookback", type=int, default=100)
    parser.add_argument("--additional-cost-return", type=float, default=0.0)
    parser.add_argument("--cost-evidence-status", default="INCOMPLETE_UNMEASURED")
    parser.add_argument("--cost-source-id", action="append", default=[])
    parser.add_argument("--candidate-quantile", action="append", type=float)
    parser.add_argument("--candidate-interaction-order", type=int, choices=(1, 2), default=2)
    args = parser.parse_args()
    configuration = PipelineConfiguration(
        args.source_root,
        args.repository_root,
        args.database,
        args.evidence_directory,
        tuple(args.symbols),
        tuple(args.timeframes),
        tuple(args.horizons),
        datetime.fromisoformat(args.observed_at).astimezone(timezone.utc).isoformat(),
        args.stride,
        args.lookback,
        args.additional_cost_return,
        args.cost_evidence_status,
        tuple(args.cost_source_id),
        ResearchPolicy(200, 50, 50, 900, 100, 50, "HFM_EXECUTABLE_TICKS"),
        tuple(args.candidate_quantile or (.25, .50, .75)),
        args.candidate_interaction_order,
    )
    report = run_pipeline(configuration)
    print(json.dumps(asdict(report), indent=2, sort_keys=True))
    return 0 if report.status == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
