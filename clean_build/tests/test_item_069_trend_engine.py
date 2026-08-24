from datetime import datetime, timedelta, timezone

from cipherfx_clean.contracts import MarketSnapshot, RawTick
from cipherfx_clean.dashboard import intelligence_view
from cipherfx_clean.intelligence.engine import IntelligenceEngine
from cipherfx_clean.intelligence.trend_engine import build_trend_engine_library
from test_item_065_vwap_deviation import _days


UTC = timezone.utc


def test_trend_engine_standardizes_slope_fit_and_persistence():
    start = datetime(2026, 6, 1, tzinfo=UTC)
    observed_at = start + timedelta(days=49, hours=8)
    rows = _days(start, 50)
    library = build_trend_engine_library(
        {"USA100": {"M1": rows}}, observed_at=observed_at
    )
    observation = library.observations_for_symbol("USA100")["M1"]

    assert observation.sample_size == 20
    assert observation.direction == "UP"
    assert 0.0 <= observation.fit <= 1.0
    assert 0.0 <= observation.persistence <= 1.0
    assert len(observation.source_digest) == 64


def test_engine_and_dashboard_expose_trend_engine_context():
    start = datetime(2026, 6, 1, tzinfo=UTC)
    observed_at = start + timedelta(days=49, hours=8)
    rows = _days(start, 50)
    library = build_trend_engine_library(
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
    report = IntelligenceEngine(trend_engine_library=library).analyse(snapshot)
    view = intelligence_view(report)

    assert report.trend_engine_context == library.observations_for_symbol("USA100")
    assert view["trend_engine_context"]["M1"]["direction"] == "UP"
    assert "TREND_ENGINE_OBSERVED" in report.evidence
