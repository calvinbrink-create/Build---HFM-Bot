from datetime import datetime, time, timedelta, timezone

from cipherfx_clean.calendar import SessionWindow, TradingCalendar
from cipherfx_clean.hfm_data import HfmCsvMarketDataAdapter
from cipherfx_clean.observation import CsvTickObserver
from cipherfx_clean.store import EvidenceStore


UTC = timezone.utc


def _tick_file(root, epoch):
    (root / "tick_XAUUSD.txt").write_text(
        f"time_broker={epoch + 10800}\n"
        f"time_utc={epoch}\n"
        "broker_utc_offset_seconds=10800\n"
        f"export_receipt_local_epoch={epoch + 1}\n"
        "bid=2000.0\nask=2000.2\n"
    )


def test_observer_persists_fresh_ticks_once_and_survives_restart(tmp_path):
    now = datetime(2026, 8, 24, 10, tzinfo=UTC)
    _tick_file(tmp_path, int(now.timestamp()))
    store = EvidenceStore(tmp_path / "observer.sqlite3")
    calendar = TradingCalendar(
        timezone_name="UTC",
        sessions=(SessionWindow("ALL", tuple(range(7)), time(0), time(0)),),
    )
    first = CsvTickObserver(
        adapter=HfmCsvMarketDataAdapter(tmp_path), store=store,
        calendars={"XAUUSD": calendar}, maximum_age=timedelta(seconds=5),
    ).poll("XAUUSD", observed_at=now)
    second = CsvTickObserver(
        adapter=HfmCsvMarketDataAdapter(tmp_path), store=store,
        calendars={"XAUUSD": calendar}, maximum_age=timedelta(seconds=5),
    ).poll("XAUUSD", observed_at=now + timedelta(seconds=1))

    assert first.status == "VALID" and first.stored
    assert second.status == "DUPLICATE" and not second.stored
    assert store._conn.execute("SELECT COUNT(*) FROM raw_ticks").fetchone()[0] == 1
    assert store._conn.execute("SELECT COUNT(*) FROM tick_quality_events").fetchone()[0] == 1
    assert store._conn.execute("SELECT status FROM tick_quality_events").fetchone()[0] == "FRESH"
    store.close()


def test_expected_market_closure_is_not_reported_as_stale_feed(tmp_path):
    friday = datetime(2026, 8, 21, 20, tzinfo=UTC)
    sunday = datetime(2026, 8, 23, 20, tzinfo=UTC)
    _tick_file(tmp_path, int(friday.timestamp()))
    store = EvidenceStore(tmp_path / "closed.sqlite3")
    weekday = TradingCalendar(
        timezone_name="UTC",
        sessions=(SessionWindow("WEEKDAY", tuple(range(5)), time(0), time(0)),),
    )
    result = CsvTickObserver(
        adapter=HfmCsvMarketDataAdapter(tmp_path), store=store,
        calendars={"XAUUSD": weekday}, maximum_age=timedelta(seconds=5),
    ).poll("XAUUSD", observed_at=sunday)

    assert result.status == "MARKET_CLOSED"
    assert store._conn.execute("SELECT COUNT(*) FROM raw_ticks").fetchone()[0] == 0
    assert store._conn.execute("SELECT status FROM tick_quality_events").fetchone()[0] == "CLOSED"
    store.close()


def test_feed_health_is_logged_only_when_freshness_transitions(tmp_path):
    now = datetime(2026, 8, 24, 10, tzinfo=UTC)
    calendar = TradingCalendar(
        timezone_name="UTC",
        sessions=(SessionWindow("ALL", tuple(range(7)), time(0), time(0)),),
    )
    store = EvidenceStore(tmp_path / "transitions.sqlite3")
    observer = CsvTickObserver(
        adapter=HfmCsvMarketDataAdapter(tmp_path),
        store=store,
        calendars={"XAUUSD": calendar},
        maximum_age=timedelta(seconds=5),
    )
    _tick_file(tmp_path, int(now.timestamp()) - 30)
    assert observer.poll("XAUUSD", observed_at=now).status == "STALE"
    assert observer.poll("XAUUSD", observed_at=now + timedelta(seconds=1)).status == "STALE"
    _tick_file(tmp_path, int((now + timedelta(seconds=2)).timestamp()))
    assert observer.poll("XAUUSD", observed_at=now + timedelta(seconds=2)).status == "VALID"

    assert store._conn.execute(
        "SELECT status FROM tick_quality_events ORDER BY observed_at"
    ).fetchall() == [("STALE",), ("FRESH",)]
    store.close()


def test_missing_tick_export_is_classified_without_crashing_observer(tmp_path):
    now = datetime(2026, 8, 24, 10, tzinfo=UTC)
    calendar = TradingCalendar(
        timezone_name="UTC",
        sessions=(SessionWindow("ALL", tuple(range(7)), time(0), time(0)),),
    )
    store = EvidenceStore(tmp_path / "missing.sqlite3")
    observer = CsvTickObserver(
        adapter=HfmCsvMarketDataAdapter(tmp_path),
        store=store,
        calendars={"XAUUSD": calendar},
    )

    result = observer.poll("XAUUSD", observed_at=now)

    assert result.status == "MISSING"
    assert result.reason == "TICK_EXPORT_MISSING"
    assert result.tick_at is None
    assert store._conn.execute(
        "SELECT status FROM tick_quality_events"
    ).fetchall() == [("STALE",)]
    store.close()


def test_malformed_tick_export_is_classified_without_storing_quote(tmp_path):
    now = datetime(2026, 8, 24, 10, tzinfo=UTC)
    (tmp_path / "tick_XAUUSD.txt").write_text("time_utc=1\nbid=10\n")
    calendar = TradingCalendar(
        timezone_name="UTC",
        sessions=(SessionWindow("ALL", tuple(range(7)), time(0), time(0)),),
    )
    store = EvidenceStore(tmp_path / "malformed.sqlite3")
    observer = CsvTickObserver(
        adapter=HfmCsvMarketDataAdapter(tmp_path),
        store=store,
        calendars={"XAUUSD": calendar},
    )

    result = observer.poll("XAUUSD", observed_at=now)

    assert result.status == "MALFORMED"
    assert result.reason == "TICK_EXPORT_MALFORMED:ValueError"
    assert store._conn.execute("SELECT COUNT(*) FROM raw_ticks").fetchone()[0] == 0
    store.close()
