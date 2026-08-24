from datetime import datetime, timedelta, timezone

from cipherfx_clean.contracts import Candle, MarketState, TradeDecision, TradeOutcome
from cipherfx_clean.hfm_data import SourceDataset
from cipherfx_clean.market import RawTick
from cipherfx_clean.store import EvidenceStore
import pytest


def test_store_persists_complete_evidence_and_is_idempotent(tmp_path):
    store = EvidenceStore(tmp_path / "clean-evidence.sqlite3")
    tick = RawTick("XAUUSD", datetime.now(timezone.utc), 10.0, 10.2)
    store.write_tick(tick)
    store.write_tick(tick)
    state = MarketState("XAUUSD", tick.timestamp, {"timeframes": ["M1", "M5"]})
    decision = TradeDecision("d-1", "XAUUSD", "BUY", tick.timestamp, edge_id="e-1")
    store.write_snapshot(state)
    store.write_snapshot(state)
    store.write_decision(decision)
    store.write_outcome(TradeOutcome("d-1", "ACCEPTED", "broker-1"))
    store.audit("DECISION", tick.timestamp, {"decision_id": "d-1"})

    assert store.integrity()
    assert store._conn.execute("SELECT COUNT(*) FROM raw_ticks").fetchone()[0] == 1
    assert store._conn.execute("SELECT COUNT(*) FROM trade_outcomes").fetchone()[0] == 1
    store.close()


def test_store_rejects_changed_payload_for_an_existing_immutable_id(tmp_path):
    store = EvidenceStore(tmp_path / "immutable.sqlite3")
    now = datetime.now(timezone.utc)
    store.write_decision(TradeDecision("d-1", "XAUUSD", "NO_TRADE", now))
    with pytest.raises(ValueError, match="immutable record conflict"):
        store.write_decision(TradeDecision("d-1", "UK100", "NO_TRADE", now))
    store.close()


def test_dataset_storage_is_a_compact_content_hashed_manifest(tmp_path):
    store = EvidenceStore(tmp_path / "manifest.sqlite3")
    now = datetime.now(timezone.utc)
    bars = tuple(
        Candle("XAUUSD", "M1", now, now, 1.0, 2.0, .5, 1.5, source_id=f"bar:{index}")
        for index in range(1_000)
    )
    dataset = SourceDataset(
        "dataset-1", "XAUUSD", now, now, {"M1": bars}, (), {"M1": "content-hash"}, (), {}, {}
    )

    store.write_dataset(dataset)

    payload = store._conn.execute("SELECT payload FROM datasets WHERE dataset_id='dataset-1'").fetchone()[0]
    assert '"storage_contract":"CONTENT_HASHED_SOURCE_MANIFEST_V1"' in payload
    assert '"frame_counts":{"M1":1000}' in payload
    assert "bar:999" not in payload
    assert len(payload) < 2_000
    store.close()


def test_raw_tick_library_persists_quote_event_and_derived_microstructure(tmp_path):
    store = EvidenceStore(tmp_path / "ticks.sqlite3")
    start = datetime(2026, 8, 24, 6, tzinfo=timezone.utc)
    first = RawTick("XAUUSD", start, 100.0, 100.2)
    second = RawTick("XAUUSD", start + timedelta(seconds=2), 100.3, 100.5)

    assert store.write_ticks((first, second)) == 2
    assert store.write_ticks((first, second)) == 0
    rows = store.load_tick_observations(
        "XAUUSD", start_at=start, end_at=start + timedelta(seconds=2)
    )

    assert len(rows) == 2
    assert rows[0]["spread"] == pytest.approx(.2)
    assert rows[0]["price_change"] == 0.0
    assert rows[0]["direction"] == "FIRST"
    assert rows[0]["event_type"] == "QUOTE"
    assert len(rows[0]["event_id"]) == 64
    assert rows[1]["price_change"] == pytest.approx(.3)
    assert rows[1]["interarrival_seconds"] == 2.0
    assert rows[1]["tick_velocity"] == pytest.approx(.15)
    assert rows[1]["direction"] == "UP"
    store.close()


def test_existing_clean_tick_database_is_migrated_without_losing_quotes(tmp_path):
    import sqlite3

    path = tmp_path / "old.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE raw_ticks (symbol TEXT, timestamp TEXT, bid REAL, ask REAL, "
        "PRIMARY KEY(symbol,timestamp,bid,ask))"
    )
    connection.execute(
        "INSERT INTO raw_ticks VALUES (?,?,?,?)",
        ("XAUUSD", "2026-08-24T06:00:00+00:00", 100.0, 100.2),
    )
    connection.commit()
    connection.close()

    store = EvidenceStore(path)
    columns = {
        row[1] for row in store._conn.execute("PRAGMA table_info(raw_ticks)")
    }

    assert {"spread", "price_change", "tick_velocity", "event_id"}.issubset(columns)
    assert store._conn.execute("SELECT COUNT(*) FROM raw_ticks").fetchone()[0] == 1
    store.close()
