from datetime import datetime, timedelta, timezone

from cipherfx_clean.contracts import MarketSnapshot, RawTick
from cipherfx_clean.dashboard import intelligence_view
from cipherfx_clean.intelligence.engine import IntelligenceEngine
from cipherfx_clean.intelligence.regime_probability_engine import build_regime_probability_library
from test_item_065_vwap_deviation import _days


UTC = timezone.utc


def test_regime_probability_uses_prior_states_and_normalizes():
    start = datetime(2026, 6, 1, tzinfo=UTC)
    observed_at = start + timedelta(days=49, hours=8)
    rows = _days(start, 50)
    library = build_regime_probability_library(
        {"USA100": {"M1": rows}}, observed_at=observed_at
    )
    observation = library.observations_for_symbol("USA100")["M1"]

    assert observation.current_label == "TREND_UP"
    assert observation.sample_size > 0
    assert sum(observation.counts.values()) == observation.sample_size
    assert abs(sum(observation.probabilities.values()) - 1.0) < 1e-9
    assert observation.probabilities["TREND_UP"] > observation.probabilities["RANGE"]
    assert 0.0 <= observation.interval_low <= observation.interval_high <= 1.0
    assert len(observation.source_digest) == 64


def test_engine_and_dashboard_expose_regime_probability_context():
    start = datetime(2026, 6, 1, tzinfo=UTC)
    observed_at = start + timedelta(days=49, hours=8)
    rows = _days(start, 50)
    library = build_regime_probability_library(
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
    report = IntelligenceEngine(regime_probability_library=library).analyse(snapshot)
    view = intelligence_view(report)

    assert report.regime_probability_context == library.observations_for_symbol("USA100")
    assert view["regime_probability_context"]["M1"]["sample_size"] > 0
    assert "REGIME_PROBABILITY_ENGINE_OBSERVED" in report.evidence
