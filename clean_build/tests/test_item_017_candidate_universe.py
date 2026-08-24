from datetime import datetime, timedelta, timezone

from cipherfx_clean.candidate_universe import build_candidate_universe
from cipherfx_clean.historical_memory import HistoricalState, feature_kinds, feature_names
from cipherfx_clean.snapshot import RESEARCH_TIMEFRAMES


UTC = timezone.utc


def _states(holdout_shift: float = 0.0):
    start = datetime(2024, 1, 1, tzinfo=UTC)
    names = feature_names(RESEARCH_TIMEFRAMES)
    kinds = feature_kinds(RESEARCH_TIMEFRAMES)
    rows = []
    for index in range(140):
        value = float(index) if index < 120 else float(index) + holdout_shift
        vector = [value + feature / 1000 for feature in range(len(names))]
        for feature, kind in enumerate(kinds):
            if kind == "CATEGORICAL":
                vector[feature] = float(index % 3 - 1)
        rows.append(HistoricalState(
            f"state-{index}", "dataset", "XAUUSD",
            start + timedelta(hours=index), tuple(vector),
            ("SESSION:LONDON", "M5:REGIME:TREND_UP"),
            (f"source-{index}",),
        ))
    return tuple(rows)


def test_candidate_thresholds_never_see_holdout_data_and_interactions_are_complete():
    cutoff = datetime(2024, 1, 6, tzinfo=UTC)
    baseline = build_candidate_universe(
        _states(), symbol="XAUUSD", timeframe="M5", discovery_end=cutoff,
        minimum_samples=100,
    )
    changed_holdout = build_candidate_universe(
        _states(1_000_000.0), symbol="XAUUSD", timeframe="M5",
        discovery_end=cutoff, minimum_samples=100,
    )

    assert baseline.condition_sets == changed_holdout.condition_sets
    assert baseline.source_digest == changed_holdout.source_digest
    assert baseline.discovery_samples == 120
    singles = tuple(row for row in baseline.condition_sets if len(row) == 1)
    pairs = tuple(row for row in baseline.condition_sets if len(row) == 2)
    represented_features = {row[0].feature_index for row in singles}
    represented_pairs = {
        tuple(sorted((left.feature_index, right.feature_index)))
        for left, right in pairs
    }
    assert len(pairs) == len(represented_features) * (len(represented_features) - 1) // 2
    assert len(represented_pairs) == len(pairs)
    assert baseline.interaction_contract == "ALL_FEATURE_SINGLES_PLUS_ONE_BALANCED_SPLIT_PER_DISTINCT_FEATURE_PAIR_V1"
    assert baseline.condition_sets[0] == ()


def test_candidate_universe_is_symbol_and_timeframe_specific():
    cutoff = datetime(2024, 1, 6, tzinfo=UTC)
    m5 = build_candidate_universe(
        _states(), symbol="XAUUSD", timeframe="M5", discovery_end=cutoff,
        minimum_samples=100,
    )
    h1 = build_candidate_universe(
        _states(), symbol="XAUUSD", timeframe="H1", discovery_end=cutoff,
        minimum_samples=100,
    )

    assert m5.universe_id != h1.universe_id
    assert all(name.startswith(("M5:", "GLOBAL:")) for name in m5.feature_names)
    assert all(name.startswith(("H1:", "GLOBAL:")) for name in h1.feature_names)
    assert any(name.startswith("GLOBAL:") for name in m5.feature_names)


def test_unbounded_pattern_count_is_treated_as_numeric_not_categorical():
    rows = list(_states())
    changed = []
    pattern_index = feature_names(RESEARCH_TIMEFRAMES).index("M5:candle_pattern_count_log")
    for index, row in enumerate(rows):
        vector = list(row.vector)
        vector[pattern_index] = float(index)
        changed.append(HistoricalState(
            row.state_id, row.dataset_id, row.symbol, row.observed_at,
            tuple(vector), row.labels, row.source_ids,
        ))
    universe = build_candidate_universe(
        tuple(changed), symbol="XAUUSD", timeframe="M5",
        discovery_end=datetime(2024, 1, 6, tzinfo=UTC), minimum_samples=100,
    )
    assert universe.condition_sets
