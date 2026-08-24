from datetime import datetime, timedelta, timezone

from cipherfx_clean.contracts import MarketSnapshot, RawTick
from cipherfx_clean.dashboard import intelligence_view
from cipherfx_clean.intelligence.engine import IntelligenceEngine
from cipherfx_clean.intelligence.tick_volume import build_tick_volume_library
from test_item_065_vwap_deviation import _days


UTC = timezone.utc


def test_tick_volume_library_preserves_broker_activity_values():
    start = datetime(2026, 6, 1, tzinfo=UTC)
    observed_at = start + timedelta(days=49, hours=8)
    rows = _days(start, 50)
    library = build_tick_volume_library(
        {"USA100": {"M1": rows}}, observed_at=observed_at
    )
    observation = library.observations_for_symbol("USA100")["M1"]

    expected_rows = tuple(item for item in rows if item.end <= observed_at)
    assert observation.candle_count == len(expected_rows)
    assert observation.nonzero_candle_count == len(expected_rows)
    assert observation.total_tick_volume > 0.0
    assert observation.latest_tick_volume == expected_rows[-1].tick_volume
    assert len(observation.source_digest) == 64


def test_engine_and_dashboard_expose_tick_volume_context():
    start = datetime(2026, 6, 1, tzinfo=UTC)
    observed_at = start + timedelta(days=49, hours=8)
    rows = _days(start, 50)
    library = build_tick_volume_library(
        {"USA100": {"M1": rows}}, observed_at=observed_at
    )
    snapshot = MarketSnapshot(
        "USA100",
        observed_at,
        (RawTick("USA100", observed_at, rows[-1].close, rows[-1].close + 0.1),),
        {"M1": rows[-30:]},
        {"M1": "COMPLETED"},
        tuple(item.source_id for item in rows[-30:]),
    )
    report = IntelligenceEngine(tick_volume_library=library).analyse(snapshot)
    view = intelligence_view(report)

    assert report.tick_volume_context["M1"] == library.observations_for_symbol("USA100")["M1"]
    assert view["tick_volume_context"]["M1"]["total_tick_volume"] > 0.0
    assert "TICK_VOLUME_ACTIVITY_OBSERVED" in report.evidence
