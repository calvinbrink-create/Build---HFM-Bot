from datetime import datetime, timedelta, timezone

from cipherfx_clean.contracts import MarketSnapshot, RawTick
from cipherfx_clean.dashboard import intelligence_view
from cipherfx_clean.intelligence.engine import IntelligenceEngine
from cipherfx_clean.intelligence.trend_persistence import build_trend_persistence_library
from test_item_065_vwap_deviation import _days


UTC = timezone.utc


def test_trend_persistence_uses_directional_historical_analogues():
    start = datetime(2026, 6, 1, tzinfo=UTC)
    observed_at = start + timedelta(days=49, hours=8)
    rows = _days(start, 50)
    library = build_trend_persistence_library(
        {"USA100": {"M1": rows}}, observed_at=observed_at, lookback=20, forward_horizon=5
    )
    probability = library.observations_for_symbol("USA100")["M1"]

    assert probability.current_direction == "UP"
    assert probability.analogue_count >= 20
    assert probability.continuation_count > probability.analogue_count * 0.8
    assert probability.continuation_probability > 0.75
    assert 0.0 <= probability.interval_low <= probability.interval_high <= 1.0
    assert len(probability.source_digest) == 64


def test_engine_and_dashboard_expose_trend_persistence_probability():
    start = datetime(2026, 6, 1, tzinfo=UTC)
    observed_at = start + timedelta(days=49, hours=8)
    rows = _days(start, 50)
    library = build_trend_persistence_library(
        {"USA100": {"M1": rows}}, observed_at=observed_at, lookback=20, forward_horizon=5
    )
    snapshot = MarketSnapshot(
        "USA100",
        observed_at,
        (RawTick("USA100", observed_at, rows[-1].close, rows[-1].close + 0.1),),
        {"M1": rows[-30:]},
        {"M1": "COMPLETED"},
        tuple(item.source_id for item in rows[-30:]),
    )
    report = IntelligenceEngine(trend_persistence_library=library).analyse(snapshot)
    view = intelligence_view(report)

    assert report.trend_persistence_context == library.observations_for_symbol("USA100")
    assert view["trend_persistence_context"]["M1"]["analogue_count"] >= 20
    assert "TREND_PERSISTENCE_OBSERVED" in report.evidence
