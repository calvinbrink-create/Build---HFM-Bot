from datetime import datetime, timedelta, timezone
import json

from cipherfx_clean.historical_memory import HistoricalPath, HistoricalState, HorizonOutcome, feature_names
from cipherfx_clean.snapshot import RESEARCH_TIMEFRAMES
from cipherfx_clean.research_pipeline import (
    FeatureCondition,
    ResearchPolicy,
    default_condition_sets,
    evaluate_experiment,
    run_research,
    sweep_specs,
)
from cipherfx_clean.store import EvidenceStore


UTC = timezone.utc


def _cases(fidelity="BAR_OHLC_NO_EXECUTABLE_TICKS"):
    rows = []
    start = datetime(2024, 1, 1, tzinfo=UTC)
    for index in range(20):
        state = HistoricalState(
            f"state-{index}", "dataset", "XAUUSD", start + timedelta(days=index),
            (1.0, .2), ("SESSION:LONDON", "M5:REGIME:TREND_UP"), (f"source-{index}",),
        )
        path = HistoricalPath(state.state_id, (HorizonOutcome(300, .02, .03, -.01),), fidelity)
        rows.append((state, path))
    return tuple(rows)


def _policy(required_fidelity="HFM_EXECUTABLE_TICKS"):
    return ResearchPolicy(6, 3, 3, 1, 3, 3, required_fidelity)


def test_sweep_covers_every_declared_dimension_without_silent_truncation():
    specs = tuple(sweep_specs(
        families=("RAW", "BREAKOUT"), symbols=("XAUUSD", "UK100"),
        directions=("BUY", "SELL"), timeframes=("M5",), sessions=("ANY", "LONDON"),
        regimes=("ANY", "TREND_UP"), horizons=(300,),
        condition_sets=((), (FeatureCondition(0, "GT", 0.0),)), cost_return=.001,
    ))

    assert len(specs) == 64
    assert len({item.experiment_id for item in specs}) == len(specs)


def test_default_raw_feature_candidates_are_timeframe_specific_and_complete():
    h1 = default_condition_sets("H1")
    m5 = default_condition_sets("M5")

    assert len(h1) == 11
    assert len(m5) == 11
    assert h1[1][0].feature_index != m5[1][0].feature_index


def test_positive_bar_only_research_cannot_be_promoted_as_executable_edge():
    spec = next(sweep_specs(
        families=("RAW",), symbols=("XAUUSD",), directions=("BUY",),
        timeframes=("M5",), sessions=("LONDON",), regimes=("TREND_UP",),
        horizons=(300,), condition_sets=((FeatureCondition(0, "GT", 0.0),),), cost_return=.001,
    ))

    result = evaluate_experiment(spec, _cases(), policy=_policy(), multiple_test_count=1)

    assert result.validation.status == "PASS"
    assert result.status == "BLOCKED_NON_EXECUTABLE_EVIDENCE"
    assert "BAR_OHLC_NO_EXECUTABLE_TICKS" in result.fidelities


def test_historical_broker_spread_is_charged_for_each_observation():
    spec = next(sweep_specs(
        families=("RAW",), symbols=("XAUUSD",), directions=("BUY",),
        timeframes=("M5",), sessions=("LONDON",), regimes=("TREND_UP",),
        horizons=(300,), condition_sets=((),), cost_return=0.0,
    ))
    names = feature_names(RESEARCH_TIMEFRAMES)
    spread_index = names.index("M5:spread_return")
    costly = []
    for state, path in _cases("HFM_EXECUTABLE_TICKS"):
        vector = [0.0] * len(names)
        vector[spread_index] = .03
        costly.append((HistoricalState(
            state.state_id, state.dataset_id, state.symbol, state.observed_at,
            tuple(vector), state.labels, state.source_ids,
        ), path))

    result = evaluate_experiment(spec, tuple(costly), policy=_policy(), multiple_test_count=1)

    assert result.status == "NO_EDGE"
    assert result.validation.out_of_sample.mean_value < 0


def test_every_experiment_is_persisted_and_no_edge_enters_graveyard(tmp_path):
    states = []
    for state, path in _cases("HFM_EXECUTABLE_TICKS"):
        loss = HistoricalPath(state.state_id, (HorizonOutcome(300, -.02, .01, -.03),), path.fidelity)
        states.append((state, loss))
    specs = tuple(sweep_specs(
        families=("RAW",), symbols=("XAUUSD",), directions=("BUY",),
        timeframes=("M5",), sessions=("LONDON",), regimes=("TREND_UP",),
        horizons=(300,), condition_sets=((), (FeatureCondition(0, "GT", 0.0),)), cost_return=.001,
    ))
    store = EvidenceStore(tmp_path / "research.sqlite3")

    report = run_research(store=store, specs=specs, cases=tuple(states), policy=_policy())

    assert report.complete
    assert report.tested == 2
    assert report.no_edge == 2
    assert store._conn.execute("SELECT COUNT(*) FROM research_hypotheses").fetchone()[0] == 2
    assert store._conn.execute("SELECT COUNT(*) FROM research_assessments").fetchone()[0] == 2
    assert store._conn.execute("SELECT COUNT(*) FROM strategy_graveyard").fetchone()[0] == 2
    validation_payloads = tuple(
        row[0] for row in store._conn.execute("SELECT payload FROM validation_reports")
    )
    assert validation_payloads
    assert all(len(payload) < 20_000 for payload in validation_payloads)
    assert all(
        json.loads(payload)["storage_contract"] == "COMPACT_PURGED_WALK_FORWARD_V1"
        for payload in validation_payloads
    )
    assert all("net_value" not in payload for payload in validation_payloads)
    store.close()


def test_insufficient_candidates_keep_compact_summary_without_full_validation_blob(tmp_path):
    specs = tuple(sweep_specs(
        families=("RAW",), symbols=("XAUUSD",), directions=("BUY",),
        timeframes=("M5",), sessions=("LONDON",), regimes=("TREND_UP",),
        horizons=(300,), condition_sets=((),), cost_return=.001,
    ))
    store = EvidenceStore(tmp_path / "compact.sqlite3")

    report = run_research(
        store=store,
        specs=specs,
        cases=_cases("HFM_EXECUTABLE_TICKS")[:4],
        policy=_policy(),
    )

    assert report.insufficient == 1
    assert store._conn.execute("SELECT COUNT(*) FROM validation_reports").fetchone()[0] == 0
    payload = json.loads(store._conn.execute("SELECT payload FROM research_assessments").fetchone()[0])
    assert payload["validation_summary"]["status"] == "INSUFFICIENT_EVIDENCE"
    assert payload["storage_contract"] == "COMPACT_REPRODUCIBLE_EXPERIMENT_V2"
    store.close()


def test_same_hypothesis_can_be_reassessed_in_a_larger_test_family_without_conflict(tmp_path):
    cases = _cases("HFM_EXECUTABLE_TICKS")
    first_specs = tuple(sweep_specs(
        families=("RAW",), symbols=("XAUUSD",), directions=("BUY",),
        timeframes=("M5",), sessions=("LONDON",), regimes=("TREND_UP",),
        horizons=(300,), condition_sets=((),), cost_return=.001,
    ))
    expanded_specs = tuple(sweep_specs(
        families=("RAW",), symbols=("XAUUSD",), directions=("BUY",),
        timeframes=("M5",), sessions=("LONDON",), regimes=("TREND_UP",),
        horizons=(300,), condition_sets=((), (FeatureCondition(0, "GT", 0.0),)), cost_return=.001,
    ))
    store = EvidenceStore(tmp_path / "reassessment.sqlite3")
    run_research(store=store, specs=first_specs, cases=cases, policy=_policy())
    run_research(store=store, specs=expanded_specs, cases=cases, policy=_policy())

    assert store._conn.execute("SELECT COUNT(*) FROM research_hypotheses").fetchone()[0] == 2
    assert store._conn.execute("SELECT COUNT(*) FROM research_assessments").fetchone()[0] == 3
    store.close()
