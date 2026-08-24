from datetime import datetime, timedelta, timezone

from cipherfx_clean.contracts import Candle, MarketSnapshot
from cipherfx_clean.intelligence.market_features import detect_liquidity_sweeps
from cipherfx_clean.liquidity_failures import build_liquidity_failure_records
from cipherfx_clean.store import EvidenceStore


BASE = datetime(2026, 8, 21, tzinfo=timezone.utc)


def _snapshot():
    rows = (
        (10.0, 10.5, 9.5, 10.2),
        (10.2, 10.4, 9.2, 9.8),
        (9.8, 10.1, 9.0, 9.5),
        (9.5, 9.7, 8.7, 8.9),
        (8.9, 9.0, 8.4, 8.6),
        (8.6, 9.2, 8.5, 9.1),
    )
    candles = tuple(
        Candle(
            "XAUUSD", "M5",
            BASE + timedelta(minutes=index * 5),
            BASE + timedelta(minutes=(index + 1) * 5),
            *row,
            source="TEST",
            source_id=f"m5:{index}",
        )
        for index, row in enumerate(rows)
    )
    return MarketSnapshot(
        "XAUUSD", candles[-1].end, (), {"M5": candles}, {"M5": "COMPLETED"}, ("dataset:1",),
    )


def test_failure_library_keeps_negative_examples_and_excludes_reclaims_and_pending():
    snapshot = _snapshot()
    sweeps = detect_liquidity_sweeps(snapshot.candles["M5"], lookback=3, outcome_window=1)
    records = build_liquidity_failure_records(snapshot=snapshot, sweeps={"M5": sweeps})
    assert records
    assert {row.outcome for row in records}.issubset({"CONTINUATION", "UNRESOLVED"})
    assert all(row.reason and row.source_candle_id for row in records)
    assert all(row.reversal_excursion >= 0 and row.continuation_excursion >= 0 for row in records)
    assert not any(row.outcome in {"RECLAIM", "PENDING"} for row in records)


def test_failure_library_is_immutable_idempotent_and_survives_restart(tmp_path):
    snapshot = _snapshot()
    sweeps = detect_liquidity_sweeps(snapshot.candles["M5"], lookback=3, outcome_window=1)
    records = build_liquidity_failure_records(snapshot=snapshot, sweeps={"M5": sweeps})
    database = tmp_path / "evidence.sqlite3"
    store = EvidenceStore(database)
    assert store.write_liquidity_failures(records) == len(records)
    assert store.write_liquidity_failures(records) == 0
    assert store.integrity()
    expected = store.load_liquidity_failures("XAUUSD")
    store.close()

    reopened = EvidenceStore(database)
    assert reopened.load_liquidity_failures("XAUUSD") == expected
    assert reopened.integrity()
    reopened.close()
