from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from cipherfx_clean.contracts import RawTick
from cipherfx_clean.contracts import Candle, MarketSnapshot, TradeDecision
from cipherfx_clean.requirement_runtime import (
    C008_TIMEFRAMES,
    C008_TICK_WINDOW,
    _c008_recent_ticks,
    verify_c006_raw_tick_library,
    verify_c026_tick_directions,
    verify_c007_tick_quality,
    verify_c008_snapshot,
    verify_c009_market_state,
    verify_c010_event_snapshots,
    verify_c011_svg_chart,
    verify_c012_chart_pack,
    verify_c013_annotations,
    verify_c015_structure_report,
    verify_c016_swing_hierarchy,
)
from cipherfx_clean.intelligence.chart import build_chart_pack, with_trade_annotations
from cipherfx_clean.intelligence.engine import IntelligenceEngine
from cipherfx_clean.intelligence.model import ChartPack
from cipherfx_clean.snapshot import (
    EVENT_SNAPSHOT_PHASES,
    MARKET_STATE_TIMEFRAMES,
    freeze_event_snapshot,
    market_state_from_snapshot,
)
from cipherfx_clean.store import EvidenceStore


UTC = timezone.utc


def _row(symbol, status, observed, tick_at, reason):
    return {
        "symbol": symbol,
        "status": status,
        "reason": reason,
        "observed_at": observed.isoformat(),
        "tick_at": tick_at.isoformat() if tick_at is not None else None,
        "source_path": f"/terminal/tick_{symbol}.txt",
    }


def test_c007_runtime_verifier_requires_and_proves_fresh_and_stale_classification():
    now = datetime(2026, 8, 24, 6, tzinfo=UTC)
    payload = {
        "results": [
            _row("XAUUSD", "VALID", now, now - timedelta(seconds=1), "VALID_TICK"),
            _row("UK100", "STALE", now, now - timedelta(seconds=20), "OUTSIDE_FRESHNESS_WINDOW"),
        ]
    }

    result = verify_c007_tick_quality(payload, maximum_age_seconds=5)

    assert result["status"] == "PASS"
    assert result["observed_classifications"] == ("STALE", "VALID")
    assert all(row["classification_valid"] for row in result["checks"])


def test_c007_runtime_verifier_rejects_timestamp_classification_mismatch():
    now = datetime(2026, 8, 24, 6, tzinfo=UTC)
    payload = {
        "results": [
            _row("XAUUSD", "VALID", now, now - timedelta(seconds=20), "VALID_TICK"),
            _row("UK100", "STALE", now, now - timedelta(seconds=20), "OUTSIDE_FRESHNESS_WINDOW"),
        ]
    }

    with pytest.raises(ValueError, match="contradicts timestamps"):
        verify_c007_tick_quality(payload, maximum_age_seconds=5)


def test_c007_runtime_verifier_does_not_pass_without_live_fresh_tick():
    now = datetime(2026, 8, 24, 6, tzinfo=UTC)
    payload = {
        "results": [
            _row("UK100", "STALE", now, now - timedelta(seconds=20), "OUTSIDE_FRESHNESS_WINDOW"),
        ]
    }

    with pytest.raises(ValueError, match="fresh broker tick"):
        verify_c007_tick_quality(payload, maximum_age_seconds=5)


def test_c006_runtime_verifier_proves_complete_raw_tick_storage(tmp_path):
    path = tmp_path / "ticks.sqlite3"
    store = EvidenceStore(path)
    start = datetime(2026, 8, 24, 6, tzinfo=UTC)
    store.write_ticks(
        (
            RawTick("XAUUSD", start, 100.0, 100.2),
            RawTick("XAUUSD", start + timedelta(seconds=1), 100.1, 100.3),
        )
    )
    store.close()

    result = verify_c006_raw_tick_library(path)

    assert result["status"] == "PASS"
    assert result["tick_count"] == 2
    assert result["symbols"] == ("XAUUSD",)
    assert result["incomplete_rows"] == 0
    assert result["inconsistent_rows"] == 0


def test_c026_runtime_verifier_retains_one_provenance_sample_for_each_direction(tmp_path):
    path = tmp_path / "ticks.sqlite3"
    store = EvidenceStore(path)
    start = datetime(2026, 8, 24, 6, tzinfo=UTC)
    store.write_ticks(
        (
            RawTick("XAUUSD", start, 100.0, 100.2),
            RawTick("XAUUSD", start + timedelta(seconds=1), 100.1, 100.3),
            RawTick("XAUUSD", start + timedelta(seconds=2), 100.0, 100.2),
            RawTick("XAUUSD", start + timedelta(seconds=3), 100.0, 100.2),
        )
    )
    store.close()

    result = verify_c026_tick_directions(path)

    frame = result["symbols"]["XAUUSD"]
    assert frame["event_count"] == 3
    assert set(frame["direction_samples"]) == {"UP", "DOWN", "UNCHANGED"}
    assert all(item["event_id"] for item in frame["direction_samples"].values())


def test_c029_runtime_verifier_bounds_historical_windows_without_losing_provenance(tmp_path, monkeypatch):
    import cipherfx_clean.requirement_runtime as runtime

    monkeypatch.setattr(runtime, "_C029_MAX_TICKS_PER_SYMBOL", 24)
    path = tmp_path / "ticks.sqlite3"
    store = EvidenceStore(path)
    start = datetime(2026, 8, 24, 6, tzinfo=UTC)
    for symbol in ("XAUUSD", "UK100", "USA100", "USA500", "USA30"):
        ticks = []
        mid = 100.0
        for index in range(30):
            mid += 0.1 if index != 26 else 10.0
            ticks.append(RawTick(symbol, start + timedelta(seconds=index), mid - 0.1, mid + 0.1))
        store.write_ticks(ticks)
    store.close()

    result = runtime.verify_c029_tick_acceleration(path)

    assert result["status"] == "PASS"
    assert all(frame["source_tick_count"] == 30 for frame in result["symbols"].values())
    assert all(frame["evidence_tick_count"] == 24 for frame in result["symbols"].values())


def test_c006_runtime_verifier_rejects_legacy_incomplete_tick_rows(tmp_path):
    path = tmp_path / "ticks.sqlite3"
    store = EvidenceStore(path)
    store._conn.execute(
        "INSERT INTO raw_ticks(symbol,timestamp,bid,ask) VALUES (?,?,?,?)",
        ("XAUUSD", "2026-08-24T06:00:00+00:00", 100.0, 100.2),
    )
    store._conn.commit()
    store.close()

    with pytest.raises(ValueError, match="incomplete=1"):
        verify_c006_raw_tick_library(path)


def _c008_snapshot(*, missing=()):
    observed = datetime(2026, 8, 24, 6, tzinfo=UTC)
    frames = {}
    statuses = {}
    for index, timeframe in enumerate(C008_TIMEFRAMES):
        if timeframe in missing:
            statuses[timeframe] = "MISSING"
            continue
        source = "DERIVED_FROM_HFM_TICKS" if timeframe in {"1s", "5s", "15s"} else "HFM_MT5_NATIVE"
        frames[timeframe] = (
            Candle(
                "XAUUSD", timeframe, observed - timedelta(seconds=index + 2),
                observed - timedelta(seconds=index + 1), 100, 101, 99, 100.5,
                source=source, source_id=f"source:{timeframe}",
            ),
        )
        statuses[timeframe] = "COMPLETED"
    tick = RawTick("XAUUSD", observed - timedelta(milliseconds=100), 100.0, 100.2)
    return MarketSnapshot("XAUUSD", observed, (tick,), frames, statuses, ("dataset",))


def test_c008_snapshot_verifier_requires_all_eleven_completed_resolutions():
    result = verify_c008_snapshot(_c008_snapshot())

    assert result["symbol"] == "XAUUSD"
    assert tuple(row["timeframe"] for row in result["frames"]) == C008_TIMEFRAMES
    assert all(row["source_ids_present"] for row in result["frames"])


def test_c008_snapshot_verifier_rejects_any_missing_required_resolution():
    with pytest.raises(ValueError, match="missing completed timeframe M3"):
        verify_c008_snapshot(_c008_snapshot(missing=("M3",)))


def test_c008_tick_window_is_bounded_to_current_microstructure(tmp_path):
    store = EvidenceStore(tmp_path / "ticks.sqlite3")
    observed = datetime(2026, 8, 24, 6, tzinfo=UTC)
    store.write_ticks((
        RawTick("XAUUSD", observed - C008_TICK_WINDOW - timedelta(seconds=1), 100.0, 100.2),
        RawTick("XAUUSD", observed - timedelta(seconds=2), 100.1, 100.3),
    ))
    ticks = _c008_recent_ticks(store._conn, "XAUUSD", observed)
    store.close()

    assert len(ticks) == 1
    assert ticks[0].timestamp == observed - timedelta(seconds=2)


def test_c009_market_state_is_exact_tick_to_d1_completed_context():
    state = market_state_from_snapshot(_c008_snapshot())

    result = verify_c009_market_state(state)

    assert result["state_id"] == state.state_id
    assert result["latest_tick"]["bid"] == 100.0
    assert tuple(row["timeframe"] for row in result["timeframes"]) == MARKET_STATE_TIMEFRAMES


def test_c009_market_state_builder_rejects_missing_required_context():
    with pytest.raises(ValueError, match="missing completed H1"):
        market_state_from_snapshot(_c008_snapshot(missing=("H1",)))


def _c010_snapshots():
    state = market_state_from_snapshot(_c008_snapshot())
    return tuple(
        freeze_event_snapshot(
            event_id="event-1",
            phase=phase,
            state=state,
            captured_at=state.observed_at + timedelta(seconds=index),
            decision_id=None if phase == "PRE_EVENT" else "decision-1",
        )
        for index, phase in enumerate(EVENT_SNAPSHOT_PHASES)
    )


def test_c010_freezes_persists_and_reloads_complete_event_sequence(tmp_path):
    snapshots = _c010_snapshots()
    store = EvidenceStore(tmp_path / "event-snapshots.sqlite3")
    for snapshot in snapshots:
        store.write_event_market_snapshot(snapshot)
    persisted = store.load_event_market_snapshots("event-1")

    result = verify_c010_event_snapshots(persisted)

    assert result["event_id"] == "event-1"
    assert tuple(row["phase"] for row in result["phases"]) == EVENT_SNAPSHOT_PHASES
    assert all(len(row["canonical_state_sha256"]) == 64 for row in result["phases"])
    assert store.integrity()
    store.close()


def test_c010_rejects_tampered_snapshot_identity():
    snapshots = _c010_snapshots()
    tampered = (snapshots[0], replace(snapshots[1], snapshot_id="0" * 64), snapshots[2])

    with pytest.raises(ValueError, match="identity mismatch for ENTRY"):
        verify_c010_event_snapshots(tampered)


def test_c011_live_chart_verifier_requires_matching_completed_candles(tmp_path):
    snapshot = _c008_snapshot()
    rows = tuple(snapshot.candles["M5"])
    from cipherfx_clean.intelligence.renderer import render_svg

    result = verify_c011_svg_chart(
        snapshot,
        timeframe="M5",
        svg=render_svg(rows),
        output=tmp_path / "live.svg",
        maximum_bar_age_seconds=60,
    )

    assert result["rendered_candles"] == 1
    assert result["chart_bytes"] > 0
    assert len(result["chart_sha256"]) == 64


def test_c011_live_chart_verifier_rejects_wrong_chart_identity(tmp_path):
    with pytest.raises(ValueError, match="identity is missing"):
        verify_c011_svg_chart(
            _c008_snapshot(),
            timeframe="M5",
            svg='<svg xmlns="http://www.w3.org/2000/svg"></svg>',
            output=tmp_path / "invalid.svg",
            maximum_bar_age_seconds=60,
        )


def test_c012_chart_pack_links_five_frames_to_one_snapshot():
    full = _c008_snapshot()
    frames = {timeframe: full.candles[timeframe] for timeframe in ("M1", "M5", "M15", "H1", "H4")}
    statuses = {timeframe: "COMPLETED" for timeframe in frames}
    snapshot = MarketSnapshot(
        full.symbol,
        full.observed_at,
        full.ticks,
        frames,
        statuses,
        full.source_ids,
    )
    chart_pack = build_chart_pack(snapshot, {}, {}, {})
    from cipherfx_clean.intelligence.renderer import render_svg
    rendered = {timeframe: render_svg(rows) for timeframe, rows in frames.items()}

    result = verify_c012_chart_pack(snapshot, chart_pack, rendered)

    assert tuple(row["timeframe"] for row in result["frames"]) == ("M1", "M5", "M15", "H1", "H4")
    assert result["pack_id"] == chart_pack.pack_id


def test_c012_chart_pack_rejects_missing_linked_frame():
    full = _c008_snapshot()
    frames = {timeframe: full.candles[timeframe] for timeframe in ("M1", "M5", "M15", "H1")}
    snapshot = MarketSnapshot(
        full.symbol,
        full.observed_at,
        full.ticks,
        frames,
        {timeframe: "COMPLETED" for timeframe in frames},
        full.source_ids,
    )
    chart_pack = build_chart_pack(snapshot, {}, {}, {})
    from cipherfx_clean.intelligence.renderer import render_svg

    with pytest.raises(ValueError, match="requires linked"):
        verify_c012_chart_pack(
            snapshot,
            chart_pack,
            {timeframe: render_svg(rows) for timeframe, rows in frames.items()},
        )


def test_c013_annotation_engine_plots_every_required_family():
    full = _c008_snapshot()
    base = ChartPack(
        symbol="XAUUSD",
        observed_at=full.observed_at.isoformat(),
        timeframes={"M5": full.candles["M5"]},
        annotations={
            "M5": (
                {"type": "STRUCTURE", "price": 100.5},
                {"type": "DEMAND", "low": 99.0, "high": 100.0},
                {"type": "SWING_HIGH", "price": 101.0},
                {"type": "FVG_BULLISH", "low": 99.5, "high": 100.1},
                {"type": "VWAP", "price": 100.2},
                {"type": "SESSION_ASIA_HIGH", "price": 101.2},
            )
        },
        pack_id="pack-1",
        source_ids=("dataset",),
    )
    decision = TradeDecision(
        "decision-1",
        "XAUUSD",
        "BUY",
        full.observed_at,
        entry=100.5,
        stop=99.5,
        target=102.5,
    )
    annotated = with_trade_annotations(base, decision)
    from cipherfx_clean.intelligence.renderer import render_svg
    svg = render_svg(full.candles["M5"], annotations=annotated.annotations["M5"])

    result = verify_c013_annotations(annotated, timeframe="M5", svg=svg)

    assert all(result["required_families"].values())
    assert {"ENTRY", "STOP_LOSS", "TAKE_PROFIT"}.issubset(result["annotation_types"])


def test_c013_annotation_engine_rejects_incomplete_chart():
    full = _c008_snapshot()
    incomplete = ChartPack(
        "XAUUSD",
        full.observed_at.isoformat(),
        {"M5": full.candles["M5"]},
        {"M5": ({"type": "STRUCTURE", "price": 100.5},)},
        "pack-1",
        ("dataset",),
    )

    with pytest.raises(ValueError, match="missing chart annotations"):
        verify_c013_annotations(incomplete, timeframe="M5", svg="<svg></svg>")


def test_c015_live_report_contract_uses_ordered_labels_and_consistent_events():
    snapshot = _c008_snapshot()
    report = IntelligenceEngine().analyse(snapshot)
    result = verify_c015_structure_report(report)
    assert result["state_id"] == report.state_id
    assert set(result["frames"]) == set(snapshot.candles)
    for frame in result["frames"].values():
        assert set(frame["swing_sequence"]).issubset({"HH", "HL", "LH", "LL", "EH", "EL"})
        assert frame["break_of_structure"] == (frame["bos_direction"] is not None)


def test_c016_swing_hierarchy_has_bounded_significance():
    snapshot = _c008_snapshot()
    values = (
        (9.5, 10, 9, 9.8),
        (10, 12, 10, 11),
        (10, 11, 8, 9),
        (11, 14, 10, 13),
        (10, 12, 9, 10),
        (11, 13, 10, 12),
        (8, 12, 7, 9),
        (9, 11, 8, 10),
    )
    rows = tuple(
        Candle(
            "XAUUSD",
            "M5",
            snapshot.observed_at - timedelta(minutes=5 * (len(values) - index)),
            snapshot.observed_at - timedelta(minutes=5 * (len(values) - index - 1)),
            *value,
            source="HFM_MT5_NATIVE",
            source_id=f"M5:{index}",
        )
        for index, value in enumerate(values)
    )
    snapshot = replace(snapshot, candles={**snapshot.candles, "M5": rows})
    result = verify_c016_swing_hierarchy(IntelligenceEngine().analyse(snapshot))
    assert result["state_id"]
    points = tuple(point for frame in result["frames"].values() for point in frame)
    assert points
    assert all(point["hierarchy"] in {"MAJOR", "MINOR"} for point in points)
    assert all(0.0 <= point["significance"] <= 1.0 for point in points)
