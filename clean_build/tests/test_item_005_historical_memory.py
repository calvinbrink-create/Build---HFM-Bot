from datetime import datetime, timedelta, timezone

import pytest

from cipherfx_clean.contracts import Candle, MarketSnapshot, RawTick
from cipherfx_clean.hfm_data import SourceDataset
from cipherfx_clean.historical_memory import (
    FEATURE_SCHEMA_VERSION,
    HistoricalAnalogueIndex,
    HistoricalState,
    feature_names,
    feature_vector,
    path_outcome,
    synchronized_snapshots,
    tick_path_outcome,
)
from cipherfx_clean.store import EvidenceStore
from cipherfx_clean.calendar import SessionWindow, TradingCalendar
from datetime import time


def _dataset():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    m1 = tuple(
        Candle("XAUUSD", "M1", start + timedelta(minutes=i), start + timedelta(minutes=i + 1), 100 + i, 101 + i, 99 + i, 100.5 + i, tick_volume=10 + i, source="HFM", source_id=f"m1:{i}")
        for i in range(20)
    )
    m5 = tuple(
        Candle("XAUUSD", "M5", start + timedelta(minutes=i * 5), start + timedelta(minutes=(i + 1) * 5), 100 + i * 5, 105 + i * 5, 99 + i * 5, 104 + i * 5, tick_volume=50, source="HFM", source_id=f"m5:{i}")
        for i in range(4)
    )
    return SourceDataset("dataset", "XAUUSD", start, start + timedelta(minutes=20), {"M1": m1, "M5": m5}, (), {"M1": "h1", "M5": "h5"}, (), {}, {})


def test_synchronized_history_never_uses_a_future_bar():
    dataset = _dataset()
    snapshots = tuple(synchronized_snapshots(dataset, timeframes=("M1", "M5"), warmup=5, lookback=5, stride=5))

    assert snapshots
    assert all(row.end <= snapshot.observed_at for snapshot in snapshots for rows in snapshot.candles.values() for row in rows)
    assert all(len(snapshot.candles["M1"]) <= 5 for snapshot in snapshots)


def test_feature_memory_paths_and_analogue_search_are_deterministic(tmp_path):
    dataset = _dataset()
    snapshots = tuple(synchronized_snapshots(dataset, timeframes=("M1", "M5"), warmup=5, lookback=5, stride=5))
    states = tuple(feature_vector(snapshot, ("M1", "M5")) for snapshot in snapshots)
    paths = tuple(path_outcome(state, dataset.bars["M1"], (60, 300)) for state in states)
    cached_ends = tuple(row.end for row in dataset.bars["M1"])
    assert path_outcome(states[0], dataset.bars["M1"], (60, 300), anchor_ends=cached_ends) == paths[0]
    with pytest.raises(ValueError, match="anchor_ends length"):
        path_outcome(states[0], dataset.bars["M1"], anchor_ends=cached_ends[:-1])
    index = HistoricalAnalogueIndex(states, paths)

    found = index.search(states[-1], limit=2)
    assert found
    assert found[0].state_id != states[-1].state_id
    assert found[0].path.fidelity == "BAR_OHLC_NO_EXECUTABLE_TICKS"

    store = EvidenceStore(tmp_path / "history.sqlite3")
    assert store.write_historical_cases(tuple(zip(states, paths))) == len(states) * 2
    assert store.write_historical_cases(tuple(zip(states, paths))) == 0
    loaded = store.load_historical_cases("XAUUSD")
    assert tuple(row[0] for row in loaded) == states
    assert all(row[0].feature_schema_version == FEATURE_SCHEMA_VERSION for row in loaded)

    legacy = HistoricalState(
        "legacy-v2",
        "legacy-dataset",
        "XAUUSD",
        states[0].observed_at - timedelta(days=1),
        tuple(0.0 for _ in range(24)),
        ("LEGACY",),
        ("legacy-source",),
        2,
    )
    legacy_path = type(paths[0])("legacy-v2", paths[0].outcomes, paths[0].fidelity)
    store.write_historical_cases(((legacy, legacy_path),))
    assert tuple(row[0] for row in store.load_historical_cases("XAUUSD")) == states
    assert tuple(row[0] for row in store.load_historical_cases("XAUUSD", feature_version=2)) == (legacy,)

    conflicting = HistoricalState(
        states[0].state_id,
        states[0].dataset_id,
        states[0].symbol,
        states[0].observed_at,
        states[0].vector + (999.0,),
        states[0].labels,
        states[0].source_ids,
    )
    with pytest.raises(ValueError, match="immutable record conflict"):
        store.write_historical_cases(((conflicting, paths[0]),))
    assert tuple(row[0] for row in store.load_historical_cases("XAUUSD")) == states
    store.close()


def test_feature_vector_labels_both_market_direction_and_no_trade_context():
    dataset = _dataset()
    snapshot = next(synchronized_snapshots(dataset, timeframes=("M1", "M5"), warmup=5, lookback=5))
    state = feature_vector(snapshot, ("M1", "M5"))

    assert len(state.vector) == len(feature_names(("M1", "M5")))
    assert state.feature_schema_version == FEATURE_SCHEMA_VERSION
    assert len(feature_names(("M1", "M5"))) == len(state.vector)
    assert any(label.startswith("M1:STRUCTURE:") for label in state.labels)
    assert state.source_ids
    assert len(state.source_ids) <= 3


def test_historical_population_excludes_contaminated_windows():
    dataset = _dataset()
    calendar = TradingCalendar(
        timezone_name="UTC",
        sessions=(SessionWindow("ALWAYS", tuple(range(7)), time(0), time(0)),),
    )
    clean = tuple(
        synchronized_snapshots(
            dataset,
            timeframes=("M1",),
            warmup=5,
            lookback=5,
            stride=5,
            window_calendars={"M1": calendar},
        )
    )
    broken_rows = dataset.bars["M1"][:8] + dataset.bars["M1"][9:]
    broken = SourceDataset(
        "broken", dataset.symbol, dataset.start, dataset.end,
        {**dataset.bars, "M1": broken_rows}, dataset.ticks, dataset.source_hashes,
        dataset.issues, dataset.terminal, dataset.account,
    )
    contaminated = tuple(
        synchronized_snapshots(
            broken,
            timeframes=("M1",),
            warmup=5,
            lookback=5,
            stride=5,
            window_calendars={"M1": calendar},
        )
    )
    assert clean
    assert len(contaminated) < len(clean)


def test_tick_outcomes_are_explicitly_distinct_from_bar_path_outcomes():
    dataset = _dataset()
    snapshot = next(synchronized_snapshots(dataset, timeframes=("M1", "M5"), warmup=5, lookback=5))
    state = feature_vector(snapshot, ("M1", "M5"))
    ticks = (
        RawTick("XAUUSD", state.observed_at + timedelta(seconds=1), 105.0, 105.2),
        RawTick("XAUUSD", state.observed_at + timedelta(seconds=5), 105.5, 105.7),
        RawTick("XAUUSD", state.observed_at + timedelta(seconds=15), 104.8, 105.0),
    )

    outcome = tick_path_outcome(state, ticks, horizons=(5, 15))

    assert outcome.fidelity == "HFM_EXECUTABLE_TICKS"
    assert tuple(row.horizon_seconds for row in outcome.outcomes) == (5, 15)
    assert outcome.outcomes[0].mfe_return > 0
