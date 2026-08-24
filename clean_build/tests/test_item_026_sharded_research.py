from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json

import pytest

from cipherfx_clean.candidate_universe import CandidateUniverse
from cipherfx_clean.historical_memory import HistoricalPath, HistoricalState, HorizonOutcome
from cipherfx_clean.research_pipeline import FeatureCondition, ResearchPolicy, run_research, sweep_specs
from cipherfx_clean.sharded_research import combine_shards, experiment_count, partition_universes, study_identity
from cipherfx_clean.store import EvidenceStore


UTC = timezone.utc


def _universe(number: int, conditions: int = 2) -> CandidateUniverse:
    condition_sets = ((), (FeatureCondition(number, "GE", 0.0),))[:conditions]
    return CandidateUniverse(
        f"universe-{number}", "XAUUSD", f"M{number + 1}",
        datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 2, 1, tzinfo=UTC),
        100, 4, condition_sets, f"source-{number}", (), "TEST",
    )


def _cases():
    start = datetime(2024, 1, 1, tzinfo=UTC)
    return tuple(
        (
            HistoricalState(
                f"state-{index}", "dataset", "XAUUSD", start + timedelta(days=index),
                (1.0,), ("SESSION:LONDON", "M5:REGIME:TREND_UP"), (f"source-{index}",),
            ),
            HistoricalPath(
                f"state-{index}", (HorizonOutcome(300, -.01, .01, -.02),),
                "HFM_EXECUTABLE_TICKS",
            ),
        )
        for index in range(20)
    )


def test_partition_covers_each_universe_once_and_global_identity_is_order_independent():
    rows = tuple(_universe(index) for index in range(7))
    shards = tuple(partition_universes(rows, index, 3) for index in range(3))
    ids = tuple(row.universe_id for shard in shards for row in shard)
    policy = ResearchPolicy(6, 3, 3, 1, 3, 3)

    assert len(ids) == len(set(ids)) == 7
    assert set(ids) == {row.universe_id for row in rows}
    assert experiment_count(rows, direction_count=2, horizon_count=2) == 56
    assert study_identity(
        rows, directions=("BUY", "SELL"), horizons=(300,), policy=policy,
        version=4, cost_return=0.0, cost_evidence_status="INCOMPLETE", cost_source_ids=(),
    ) == study_identity(
        tuple(reversed(rows)), directions=("BUY", "SELL"), horizons=(300,), policy=policy,
        version=4, cost_return=0.0, cost_evidence_status="INCOMPLETE", cost_source_ids=(),
    )


def test_shards_share_global_run_identity_and_merge_idempotently(tmp_path):
    policy = ResearchPolicy(6, 3, 3, 1, 3, 3, "HFM_EXECUTABLE_TICKS")
    conditions = ((), (FeatureCondition(0, "GT", 0.0),))
    all_specs = tuple(sweep_specs(
        families=("RAW",), symbols=("XAUUSD",), directions=("BUY", "SELL"),
        timeframes=("M5",), sessions=("ANY",), regimes=("ANY",), horizons=(300,),
        condition_sets=conditions, cost_return=0.0, version=4,
    ))
    identity = "one-global-study"
    reports = []
    databases = []
    report_paths = []
    for shard_index, specs in enumerate((all_specs[::2], all_specs[1::2])):
        database = tmp_path / f"shard-{shard_index}.sqlite3"
        store = EvidenceStore(database)
        report = run_research(
            store=store, specs=specs, cases=_cases(), policy=policy,
            multiple_test_count=len(all_specs), run_identity=identity,
        )
        store.close()
        path = tmp_path / f"shard-{shard_index}.json"
        path.write_text(json.dumps({
            "study_id": identity, "shard_index": shard_index, "shard_count": 2,
            "global_experiment_count": len(all_specs), "policy": asdict(policy),
            "version": 4, "symbols": ["XAUUSD"], "timeframes": ["M5"],
            "horizons": [300], "cost_return": 0.0,
            "cost_evidence_status": "INCOMPLETE_UNMEASURED", "cost_source_ids": [],
            "report": asdict(report),
        }, default=str), encoding="utf-8")
        reports.append(report)
        databases.append(database)
        report_paths.append(path)

    assert len({row.run_id for row in reports}) == 1
    destination = tmp_path / "destination.sqlite3"
    combined = combine_shards(
        destination_database=destination,
        shard_databases=databases,
        shard_reports=report_paths,
        output=tmp_path / "combined.json",
    )
    repeated = combine_shards(
        destination_database=destination,
        shard_databases=databases,
        shard_reports=report_paths,
        output=tmp_path / "combined-again.json",
    )
    store = EvidenceStore(destination)
    assert combined["report"]["tested"] == len(all_specs)
    assert repeated["report"] == combined["report"]
    assert store._conn.execute("SELECT COUNT(*) FROM research_hypotheses").fetchone()[0] == len(all_specs)
    assert store._conn.execute("SELECT COUNT(*) FROM research_assessments").fetchone()[0] == len(all_specs)
    assert store.integrity()
    store.close()


def test_combiner_rejects_missing_or_mismatched_shards(tmp_path):
    report = tmp_path / "only.json"
    report.write_text(json.dumps({
        "study_id": "study", "shard_index": 0, "shard_count": 2,
        "global_experiment_count": 1, "policy": {}, "version": 4,
        "symbols": [], "timeframes": [], "horizons": [], "cost_return": 0.0,
        "cost_evidence_status": "INCOMPLETE", "cost_source_ids": [],
        "report": {"run_id": "run", "complete": True, "experiment_ids": []},
    }), encoding="utf-8")
    database = tmp_path / "shard.sqlite3"
    EvidenceStore(database).close()

    with pytest.raises(ValueError, match="incomplete"):
        combine_shards(
            destination_database=tmp_path / "destination.sqlite3",
            shard_databases=(database,), shard_reports=(report,), output=tmp_path / "combined.json",
        )
