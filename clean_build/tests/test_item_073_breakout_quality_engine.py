from datetime import datetime, timedelta, timezone

from cipherfx_clean.contracts import Candle, MarketSnapshot, RawTick
from cipherfx_clean.dashboard import intelligence_view
from cipherfx_clean.intelligence.breakout_quality_engine import build_breakout_quality_library
from cipherfx_clean.intelligence.engine import IntelligenceEngine
from test_item_072_breakout_engine import _breakout_rows


UTC = timezone.utc


def test_breakout_quality_records_follow_through_outcomes():
    start = datetime(2026, 6, 1, tzinfo=UTC)
    observed_at = start + timedelta(minutes=119, seconds=30)
    rows = _breakout_rows(start, 120)
    library = build_breakout_quality_library(
        {"USA100": {"M1": rows}}, observed_at=observed_at, lookback=20, forward_horizon=5
    )
    observation = library.observations_for_symbol("USA100")["M1"]

    assert len(library.outcomes_for_symbol("USA100")) >= 3
    assert observation.present is False
    assert observation.comparable_sample_size == 0
    assert len(observation.source_digest) == 64


def test_engine_and_dashboard_expose_breakout_quality_context():
    start = datetime(2026, 6, 1, tzinfo=UTC)
    observed_at = start + timedelta(minutes=119, seconds=30)
    rows = _breakout_rows(start, 120)
    library = build_breakout_quality_library(
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
    report = IntelligenceEngine(breakout_quality_library=library).analyse(snapshot)
    view = intelligence_view(report)

    assert report.breakout_quality_context == library.observations_for_symbol("USA100")
    assert view["breakout_quality_context"]["M1"]["present"] is False
    assert "BREAKOUT_QUALITY_OBSERVED" in report.evidence
