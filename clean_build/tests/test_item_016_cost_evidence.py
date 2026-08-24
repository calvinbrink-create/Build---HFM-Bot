from datetime import datetime, timedelta, timezone

import pytest

from cipherfx_clean.cost_evidence import ExecutionCostSample, build_cost_profile
from cipherfx_clean.historical_memory import HistoricalPath, HistoricalState, HorizonOutcome, feature_names
from cipherfx_clean.research_pipeline import ResearchPolicy, evaluate_experiment, sweep_specs
from cipherfx_clean.snapshot import RESEARCH_TIMEFRAMES
from cipherfx_clean.store import EvidenceStore


UTC = timezone.utc
NOW = datetime(2026, 8, 21, 12, tzinfo=UTC)


def _sample(index: int) -> ExecutionCostSample:
    return ExecutionCostSample(
        f"sample-{index}", f"decision-{index}", "XAUUSD", "BUY",
        NOW + timedelta(minutes=index), 2000.0, 2000.2, 2000.2,
        2000.25, 1.0, 100.0, -5.0, 0.0,
        NOW + timedelta(minutes=index),
        NOW + timedelta(minutes=index, milliseconds=250),
        (f"broker-event-{index}",),
    )


def test_measured_cost_profile_is_immutable_and_requires_sample_depth(tmp_path):
    rows = tuple(_sample(index) for index in range(3))
    incomplete = build_cost_profile("XAUUSD", rows, minimum_samples=4)
    complete = build_cost_profile("XAUUSD", rows, minimum_samples=3)

    assert incomplete.status == "INSUFFICIENT_SAMPLES"
    assert complete.status == "COMPLETE"
    assert complete.conservative_cost_return >= complete.median_spread_return
    assert complete.median_latency_seconds == pytest.approx(.25)

    store = EvidenceStore(tmp_path / "costs.sqlite3")
    for row in rows:
        store.write_cost_sample(row)
        store.write_cost_sample(row)
    store.write_cost_profile(complete)
    assert store._conn.execute("SELECT COUNT(*) FROM execution_cost_samples").fetchone()[0] == 3
    assert store._conn.execute("SELECT status FROM execution_cost_profiles").fetchone()[0] == "COMPLETE"
    store.close()


def test_positive_executable_history_stays_blocked_without_measured_cost_sources():
    spec = next(sweep_specs(
        families=("RAW",), symbols=("XAUUSD",), directions=("BUY",),
        timeframes=("M5",), sessions=("ANY",), regimes=("ANY",),
        horizons=(300,), condition_sets=((),), cost_return=.001,
    ))
    cases = tuple(
        (
            HistoricalState(
                f"state-{index}", "dataset", "XAUUSD", NOW + timedelta(days=index),
                tuple(0.0 for _ in feature_names(RESEARCH_TIMEFRAMES)), ("SESSION:LONDON", "M5:REGIME:TREND_UP"),
                (f"source-{index}",),
            ),
            HistoricalPath(
                f"state-{index}", (HorizonOutcome(300, .02, .03, -.01),),
                "HFM_EXECUTABLE_TICKS",
            ),
        )
        for index in range(20)
    )
    policy = ResearchPolicy(6, 3, 3, 1, 3, 3, "HFM_EXECUTABLE_TICKS")

    result = evaluate_experiment(spec, cases, policy=policy, multiple_test_count=1)

    assert result.validation.status == "PASS"
    assert result.status == "BLOCKED_INCOMPLETE_COST_EVIDENCE"
