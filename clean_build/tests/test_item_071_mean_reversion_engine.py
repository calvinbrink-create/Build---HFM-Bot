from datetime import datetime, timedelta, timezone

from cipherfx_clean.contracts import Candle, MarketSnapshot, RawTick
from cipherfx_clean.dashboard import intelligence_view
from cipherfx_clean.intelligence.engine import IntelligenceEngine
from cipherfx_clean.intelligence.mean_reversion_engine import build_mean_reversion_library


UTC = timezone.utc


def _oscillating_rows(start: datetime, count: int) -> tuple[Candle, ...]:
    rows = []
    for index in range(count):
        start_at = start + timedelta(minutes=index)
        close = 104.0 if index % 12 == 0 else 100.0 + (index % 5) * 0.02
        rows.append(
            Candle(
                "XAUUSD", "M1", start_at, start_at + timedelta(minutes=1),
                close - 0.02, close + 0.08, close - 0.08, close,
                tick_count=10, tick_volume=10.0, spread_points=2.0,
                source="TEST", source_id=f"mean-reversion:{index}",
            )
        )
    return tuple(rows)


def test_mean_reversion_records_stretch_and_return_outcomes():
    start = datetime(2026, 6, 1, tzinfo=UTC)
    observed_at = start + timedelta(minutes=179, seconds=30)
    rows = _oscillating_rows(start, 180)
    library = build_mean_reversion_library(
        {"XAUUSD": {"M1": rows}}, observed_at=observed_at, lookback=20, forward_horizon=5
    )
    observation = library.observations_for_symbol("XAUUSD")["M1"]

    assert observation.historical_stretch_count >= 10
    assert observation.historical_reversion_count > 0
    assert observation.reversion_probability is not None
    assert 0.0 <= observation.reversion_probability <= 1.0
    assert len(observation.source_digest) == 64


def test_engine_and_dashboard_expose_mean_reversion_context():
    start = datetime(2026, 6, 1, tzinfo=UTC)
    observed_at = start + timedelta(minutes=179, seconds=30)
    rows = _oscillating_rows(start, 180)
    library = build_mean_reversion_library(
        {"XAUUSD": {"M1": rows}}, observed_at=observed_at, lookback=20, forward_horizon=5
    )
    snapshot = MarketSnapshot(
        "XAUUSD",
        observed_at,
        (RawTick("XAUUSD", observed_at, rows[-1].close, rows[-1].close + 0.1),),
        {"M1": rows[-30:]},
        {"M1": "COMPLETED"},
        tuple(item.source_id for item in rows[-30:]),
    )
    report = IntelligenceEngine(mean_reversion_library=library).analyse(snapshot)
    view = intelligence_view(report)

    assert report.mean_reversion_context == library.observations_for_symbol("XAUUSD")
    assert view["mean_reversion_context"]["M1"]["historical_stretch_count"] >= 10
    assert "MEAN_REVERSION_ENGINE_OBSERVED" in report.evidence
