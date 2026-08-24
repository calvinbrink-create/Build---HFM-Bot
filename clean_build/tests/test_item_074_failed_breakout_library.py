from datetime import datetime, timedelta, timezone

from cipherfx_clean.contracts import MarketSnapshot, RawTick
from cipherfx_clean.dashboard import intelligence_view
from cipherfx_clean.intelligence.engine import IntelligenceEngine
from cipherfx_clean.intelligence.failed_breakout import build_failed_breakout_library
from test_item_072_breakout_engine import _breakout_rows


UTC = timezone.utc


def test_failed_breakout_library_preserves_non_follow_through_cases():
    start = datetime(2026, 6, 1, tzinfo=UTC)
    observed_at = start + timedelta(minutes=119, seconds=30)
    rows = _breakout_rows(start, 120)
    library = build_failed_breakout_library(
        {"USA100": {"M1": rows}}, observed_at=observed_at, lookback=20, forward_horizon=5
    )
    records = library.records_for_symbol("USA100")
    summary = library.observations_for_symbol("USA100")["M1"]

    assert records
    assert summary.failed_count == len(records)
    assert summary.failed_count == summary.invalidated_count + summary.no_follow_through_count
    assert all(item.failure_reason in {"LEVEL_INVALIDATED", "NO_FOLLOW_THROUGH"} for item in records)


def test_engine_and_dashboard_expose_failed_breakout_context():
    start = datetime(2026, 6, 1, tzinfo=UTC)
    observed_at = start + timedelta(minutes=119, seconds=30)
    rows = _breakout_rows(start, 120)
    library = build_failed_breakout_library(
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
    report = IntelligenceEngine(failed_breakout_library=library).analyse(snapshot)
    view = intelligence_view(report)

    assert report.failed_breakout_context == library.observations_for_symbol("USA100")
    assert view["failed_breakout_context"]["M1"]["failed_count"] > 0
    assert "FAILED_BREAKOUT_LIBRARY_OBSERVED" in report.evidence
