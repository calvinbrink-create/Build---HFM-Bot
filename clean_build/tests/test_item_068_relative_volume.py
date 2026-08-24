from datetime import datetime, timedelta, timezone

from cipherfx_clean.contracts import MarketSnapshot, RawTick
from cipherfx_clean.dashboard import intelligence_view
from cipherfx_clean.intelligence.engine import IntelligenceEngine
from cipherfx_clean.intelligence.relative_volume import build_relative_volume_library
from cipherfx_clean.intelligence.session_vwap import build_session_vwap_library
from test_item_065_vwap_deviation import _days


UTC = timezone.utc


def test_relative_volume_compares_current_session_with_completed_baseline():
    start = datetime(2026, 6, 1, tzinfo=UTC)
    observed_at = start + timedelta(days=49, hours=8)
    rows = _days(start, 50)
    session_vwap = build_session_vwap_library({"USA100": rows}, observed_at=observed_at)
    library = build_relative_volume_library(
        {"USA100": rows}, observed_at=observed_at, session_vwap_library=session_vwap
    )
    baselines = library.baselines_for_symbol("USA100")
    current = library.observations_for_symbol("USA100")

    assert set(baselines) == {"ASIA", "LONDON", "NEW_YORK"}
    assert all(item.sample_size >= 30 for item in baselines.values())
    assert current["LONDON"].candle_count > 0
    assert current["LONDON"].relative_volume_ratio is not None
    assert current["LONDON"].baseline_percentile is not None
    assert 0.0 <= current["LONDON"].baseline_percentile <= 1.0


def test_engine_and_dashboard_expose_relative_volume_context():
    start = datetime(2026, 6, 1, tzinfo=UTC)
    observed_at = start + timedelta(days=49, hours=8)
    rows = _days(start, 50)
    session_vwap = build_session_vwap_library({"USA100": rows}, observed_at=observed_at)
    library = build_relative_volume_library(
        {"USA100": rows}, observed_at=observed_at, session_vwap_library=session_vwap
    )
    snapshot = MarketSnapshot(
        "USA100",
        observed_at,
        (RawTick("USA100", observed_at, rows[-1].close, rows[-1].close + 0.1),),
        {"M1": rows[-30:]},
        {"M1": "COMPLETED"},
        tuple(item.source_id for item in rows[-30:]),
    )
    report = IntelligenceEngine(
        session_vwap_library=session_vwap,
        relative_volume_library=library,
    ).analyse(snapshot)
    view = intelligence_view(report)

    assert report.relative_volume_context == library.observations_for_symbol("USA100")
    assert view["relative_volume_context"]["LONDON"]["relative_volume_ratio"] is not None
    assert "RELATIVE_VOLUME_OBSERVED" in report.evidence
