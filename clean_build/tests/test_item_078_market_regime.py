from datetime import datetime, timedelta, timezone

from cipherfx_clean.contracts import MarketSnapshot, RawTick
from cipherfx_clean.dashboard import intelligence_view
from cipherfx_clean.intelligence.engine import IntelligenceEngine
from cipherfx_clean.intelligence.market_regime_engine import build_market_regime_library
from test_item_065_vwap_deviation import _days


UTC = timezone.utc


def test_market_regime_engine_classifies_completed_candles_and_probabilities():
    start = datetime(2026, 6, 1, tzinfo=UTC)
    observed_at = start + timedelta(days=49, hours=8)
    rows = _days(start, 50)
    library = build_market_regime_library(
        {"USA100": {"M1": rows}}, observed_at=observed_at
    )
    observation = library.observations_for_symbol("USA100")["M1"]

    assert observation.label == "TREND_UP"
    assert observation.sample_size == 20
    assert observation.news_state == "NOT_OBSERVED"
    assert set(observation.probabilities) >= {"TREND_UP", "RANGE", "NEWS"}
    assert abs(sum(observation.probabilities.values()) - 1.0) < 1e-9
    assert all(0.0 <= value <= 1.0 for value in observation.probabilities.values())
    assert len(observation.source_digest) == 64


def test_market_regime_engine_preserves_explicit_news_annotation_and_dashboard_context():
    start = datetime(2026, 6, 1, tzinfo=UTC)
    observed_at = start + timedelta(days=49, hours=8)
    rows = _days(start, 50)
    library = build_market_regime_library(
        {"USA100": {"M1": rows}},
        observed_at=observed_at,
        news_events={("USA100", "M1"): True},
    )
    snapshot = MarketSnapshot(
        "USA100",
        observed_at,
        (RawTick("USA100", observed_at, rows[-1].close, rows[-1].close + 0.1),),
        {"M1": rows[-30:]},
        {"M1": "COMPLETED"},
        tuple(item.source_id for item in rows[-30:]),
    )
    report = IntelligenceEngine(market_regime_library=library).analyse(snapshot)
    view = intelligence_view(report)

    assert report.market_regime_context == library.observations_for_symbol("USA100")
    assert report.market_regime_context["M1"].label == "NEWS"
    assert view["market_regime_context"]["M1"]["news_state"] == "NEWS"
    assert "MARKET_REGIME_ENGINE_OBSERVED" in report.evidence
