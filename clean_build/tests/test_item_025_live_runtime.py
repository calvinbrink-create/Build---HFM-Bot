from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import pytest

from cipherfx_clean.calendar import SessionWindow, TradingCalendar
from cipherfx_clean.hfm_data import HfmCsvMarketDataAdapter
from cipherfx_clean.historical_memory import HistoricalAnalogueIndex
from cipherfx_clean.live_runtime import LiveResearchCollector, load_historical_memory
from cipherfx_clean.snapshot import RESEARCH_TIMEFRAMES
from cipherfx_clean.store import EvidenceStore
from cipherfx_clean.contracts import RawTick


UTC = timezone.utc


def _server_epoch(value):
    return int(value.timestamp()) + 10800


def _exports(root, observed):
    (root / "symbols.csv").write_text(
        "symbol,visible,description,path,digits,point,volume_min,volume_max,volume_step,contract_size,tick_value,tick_size,trade_mode,currency_profit\n"
        "XAUUSD,1,Gold,Metals,2,0.01,0.01,60,0.01,100,1,0.01,4,USD\n"
    )
    header = "time,open,high,low,close,volume,spread_points,spread_price\n"
    seconds = {
        "MN1": 2678400,
        "W1": 604800,
        "D1": 86400,
        "H4": 14400,
        "H1": 3600,
        "M30": 1800,
        "M15": 900,
        "M5": 300,
        "M3": 180,
        "M1": 60,
    }
    for timeframe in RESEARCH_TIMEFRAMES:
        rows = []
        if timeframe == "MN1":
            starts = [datetime(2024 + (index // 12), index % 12 + 1, 1, tzinfo=UTC) for index in range(30)]
        else:
            interval = timedelta(seconds=seconds[timeframe])
            end = observed - timedelta(seconds=int(observed.timestamp()) % seconds[timeframe])
            starts = [end - interval * (30 - index) for index in range(30)]
        for index, start in enumerate(starts):
            rows.append(f"{_server_epoch(start)},{100+index},{101+index},{99+index},{100.5+index},100,2,0.02")
        (root / f"rates_XAUUSD_{timeframe}.csv").write_text(header + "\n".join(rows) + "\n")
    epoch = int(observed.timestamp())
    (root / "tick_XAUUSD.txt").write_text(
        f"time_broker={epoch + 10800}\ntime_utc={epoch}\nbroker_utc_offset_seconds=10800\n"
        f"export_receipt_local_epoch={epoch}\nbid=129.9\nask=130.0\n"
    )


def _collector(root, store):
    calendar = TradingCalendar(
        timezone_name="UTC",
        sessions=(SessionWindow("ALL", tuple(range(7)), time(0), time(0)),),
    )
    return LiveResearchCollector(
        adapter=HfmCsvMarketDataAdapter(root),
        store=store,
        calendars={"XAUUSD": calendar},
        memory=HistoricalAnalogueIndex(),
        symbols=("XAUUSD",),
    )


def test_live_collector_evaluates_each_completed_m5_bar_once_across_restart(tmp_path):
    observed = datetime(2026, 8, 24, 10, 2, tzinfo=UTC)
    _exports(tmp_path, observed)
    path = tmp_path / "live.sqlite3"
    store = EvidenceStore(path)
    first = _collector(tmp_path, store).poll_once(observed_at=observed)
    second = _collector(tmp_path, store).poll_once(observed_at=observed + timedelta(seconds=1))

    assert first.symbols[0].outcome_status == "NO_TRADE"
    assert first.symbols[0].evaluation_event_id.startswith("XAUUSD:M5:")
    assert second.symbols[0].evaluation_event_id is None
    assert store._conn.execute("SELECT COUNT(*) FROM trade_decisions").fetchone()[0] == 1
    assert store._conn.execute("SELECT COUNT(*) FROM broker_events").fetchone()[0] == 0
    store.close()

    restarted = EvidenceStore(path)
    third = _collector(tmp_path, restarted).poll_once(observed_at=observed + timedelta(seconds=2))
    assert third.symbols[0].evaluation_event_id is None
    assert restarted._conn.execute("SELECT COUNT(*) FROM trade_decisions").fetchone()[0] == 1
    restarted.close()


def test_live_collector_skips_full_snapshot_until_trigger_bar_changes(tmp_path):
    class CountingAdapter:
        def __init__(self, root):
            self._delegate = HfmCsvMarketDataAdapter(root)
            self.dataset_calls = 0

        def dataset(self, *args, **kwargs):
            self.dataset_calls += 1
            return self._delegate.dataset(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._delegate, name)

    observed = datetime(2026, 8, 24, 10, 2, tzinfo=UTC)
    _exports(tmp_path, observed)
    store = EvidenceStore(tmp_path / "trigger.sqlite3")
    calendar = TradingCalendar(
        timezone_name="UTC",
        sessions=(SessionWindow("ALL", tuple(range(7)), time(0), time(0)),),
    )
    adapter = CountingAdapter(tmp_path)
    collector = LiveResearchCollector(
        adapter=adapter,
        store=store,
        calendars={"XAUUSD": calendar},
        memory=HistoricalAnalogueIndex(),
        symbols=("XAUUSD",),
    )

    first = collector.poll_once(observed_at=observed)
    second = collector.poll_once(observed_at=observed + timedelta(seconds=1))

    assert first.symbols[0].outcome_status == "NO_TRADE"
    assert second.symbols[0].evaluation_event_id is None
    assert adapter.dataset_calls == 1
    store.close()


def test_stale_tick_is_explicit_and_never_reaches_intelligence_or_execution(tmp_path):
    observed = datetime(2026, 8, 24, 10, 2, tzinfo=UTC)
    _exports(tmp_path, observed)
    tick = tmp_path / "tick_XAUUSD.txt"
    tick.write_text(tick.read_text().replace(f"time_utc={int(observed.timestamp())}", f"time_utc={int(observed.timestamp()) - 60}"))
    store = EvidenceStore(tmp_path / "stale.sqlite3")

    result = _collector(tmp_path, store).poll_once(observed_at=observed)

    assert result.symbols[0].tick_status == "STALE"
    assert result.symbols[0].evaluation_event_id is None
    assert store._conn.execute("SELECT COUNT(*) FROM trade_decisions").fetchone()[0] == 0
    assert store._conn.execute("SELECT COUNT(*) FROM broker_events").fetchone()[0] == 0
    store.close()


def test_tick_export_refreshed_during_cycle_uses_its_own_observed_time(tmp_path):
    observed = datetime(2026, 8, 24, 10, 2, tzinfo=UTC)
    _exports(tmp_path, observed)
    tick = tmp_path / "tick_XAUUSD.txt"
    epoch = int(observed.timestamp())
    tick.write_text(
        tick.read_text()
        .replace(f"time_broker={epoch + 10800}", f"time_broker={epoch + 10802}")
        .replace(f"time_utc={epoch}", f"time_utc={epoch + 2}")
    )
    store = EvidenceStore(tmp_path / "rollover.sqlite3")

    result = _collector(tmp_path, store).poll_once(observed_at=observed)

    assert result.symbols[0].tick_status == "VALID"
    assert result.symbols[0].outcome_status == "NO_TRADE"
    assert store._conn.execute("SELECT COUNT(*) FROM market_states").fetchone()[0] == 1
    store.close()


def test_live_collector_source_has_no_broker_or_legacy_runtime_import():
    source = Path(__import__("cipherfx_clean.live_runtime").live_runtime.__file__).read_text()
    assert "MetaTrader5" not in source
    assert "order_send" not in source
    assert "legacy" not in source.lower()


def test_clean_research_service_runs_only_the_read_only_collector():
    unit = Path(__file__).parents[2] / "ops" / "systemd" / "cipherfx-clean-research.service"
    source = unit.read_text()

    assert "-m cipherfx_clean.live_runtime" in source
    assert "--cycles 0" in source
    assert "--memory-database" in source
    assert "clean_analogue_memory_v1.sqlite3" in source
    assert "--database /opt/cipherfx_mt5/clean_build/runtime/" in source
    assert "--maximum-tick-age-seconds 60" in source
    assert "order_send" not in source
    assert "MetaTrader5" not in source
    assert "mt5_xm_gateway" not in source
    assert "CipherFxBridge" not in source


def test_empty_persistent_memory_loads_as_a_real_searchable_index(tmp_path):
    store = EvidenceStore(tmp_path / "memory.sqlite3")
    memory = load_historical_memory(store, ("XAUUSD",))

    assert isinstance(memory, HistoricalAnalogueIndex)
    assert memory.states() == ()
    store.close()


def test_live_evaluation_uses_bounded_persisted_tick_window(tmp_path):
    observed = datetime(2026, 8, 24, 10, 2, tzinfo=UTC)
    _exports(tmp_path, observed)
    store = EvidenceStore(tmp_path / "window.sqlite3")
    store.write_ticks(
        tuple(
            RawTick("XAUUSD", observed - timedelta(seconds=3 - index), 129.6 + index * 0.1, 129.7 + index * 0.1)
            for index in range(3)
        )
    )

    result = _collector(tmp_path, store).poll_once(observed_at=observed)

    assert result.symbols[0].outcome_status == "NO_TRADE"
    payload = store._conn.execute(
        "SELECT payload FROM datasets ORDER BY rowid DESC LIMIT 1"
    ).fetchone()[0]
    assert '"tick_count":4' in payload
    state_id = store._conn.execute(
        "SELECT state_id FROM market_states ORDER BY rowid DESC LIMIT 1"
    ).fetchone()[0]
    report = store.load_market_state(state_id)["intelligence"]
    assert report["metadata"]["microstructure_status"] == "OBSERVED"
    assert report["metadata"]["microstructure_tick_count"] == 4
    assert report["metadata"]["cross_asset_context"]["available"] == 0.0
    store.close()


def test_live_collector_bounds_repeated_pattern_context_only_in_persisted_evidence(tmp_path):
    observed = datetime(2026, 8, 24, 10, 2, tzinfo=UTC)
    _exports(tmp_path, observed)
    store = EvidenceStore(tmp_path / "bounded-context.sqlite3")
    calendar = TradingCalendar(
        timezone_name="UTC",
        sessions=(SessionWindow("ALL", tuple(range(7)), time(0), time(0)),),
    )
    collector = LiveResearchCollector(
        adapter=HfmCsvMarketDataAdapter(tmp_path),
        store=store,
        calendars={"XAUUSD": calendar},
        memory=HistoricalAnalogueIndex(),
        symbols=("XAUUSD",),
        persisted_context_record_limit=1,
    )

    result = collector.poll_once(observed_at=observed)

    assert result.symbols[0].outcome_status == "NO_TRADE"
    state_id = store._conn.execute(
        "SELECT state_id FROM market_states ORDER BY rowid DESC LIMIT 1"
    ).fetchone()[0]
    persisted = store.load_market_state(state_id)["intelligence"]
    projection = persisted["metadata"]["persistence_projection"]
    assert projection["version"] == "CONTEXT_BOUNDED_V1"
    assert projection["pattern_outcome_context"]["retained_count"] == 1
    assert projection["pattern_outcome_context"]["total_count"] > 1
    assert len(persisted["pattern_outcome_context"]) == 1
    store.close()


def test_read_only_live_cache_retention_preserves_broker_evidence_by_refusing_to_prune(tmp_path):
    observed = datetime(2026, 8, 24, 10, 2, tzinfo=UTC)
    _exports(tmp_path, observed)
    store = EvidenceStore(tmp_path / "retention.sqlite3")
    result = _collector(tmp_path, store).poll_once(observed_at=observed)
    assert result.symbols[0].outcome_status == "NO_TRADE"

    removed = store.prune_read_only_live_cache(
        retain_after=observed + timedelta(minutes=1)
    )
    assert removed["market_states"] == 1
    assert store._conn.execute("SELECT COUNT(*) FROM market_states").fetchone()[0] == 0

    store.write_broker_event("broker-event", "decision", "SUBMITTED", observed, {})
    with pytest.raises(RuntimeError, match="refuses broker"):
        store.prune_read_only_live_cache(retain_after=observed + timedelta(hours=1))
    assert store._conn.execute("SELECT COUNT(*) FROM broker_events").fetchone()[0] == 1
    store.close()


def test_evaluation_retry_is_persistent_and_logs_each_unique_error_once(tmp_path):
    path = tmp_path / "retry.sqlite3"
    event = "XAUUSD:M5:2026-08-24T10:00:00+00:00"
    observed = datetime(2026, 8, 24, 10, 0, tzinfo=UTC)
    store = EvidenceStore(path)

    assert store.evaluation_retry_due(event, observed_at=observed, retry_after_seconds=5)
    assert store.record_evaluation_attempt(event, "XAUUSD", observed, "FAIL", "same-error")
    assert not store.evaluation_retry_due(
        event, observed_at=observed + timedelta(seconds=1), retry_after_seconds=5
    )
    assert not store.record_evaluation_attempt(
        event, "XAUUSD", observed + timedelta(seconds=5), "FAIL", "same-error"
    )
    assert store.record_evaluation_attempt(
        event, "XAUUSD", observed + timedelta(seconds=10), "FAIL", "new-error"
    )
    store.close()

    restarted = EvidenceStore(path)
    assert not restarted.evaluation_retry_due(
        event, observed_at=observed + timedelta(seconds=11), retry_after_seconds=5
    )
    assert restarted.evaluation_retry_due(
        event, observed_at=observed + timedelta(seconds=16), retry_after_seconds=5
    )
    restarted.close()
