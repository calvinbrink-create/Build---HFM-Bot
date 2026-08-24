from datetime import datetime, timedelta, timezone

from cipherfx_clean.contracts import Candle, MarketSnapshot, RawTick
from cipherfx_clean.dashboard import intelligence_view
from cipherfx_clean.intelligence.engine import IntelligenceEngine
from cipherfx_clean.intelligence.reversal_engine import build_reversal_library


UTC = timezone.utc


def _reversal_rows(start: datetime, count: int) -> tuple[Candle, ...]:
    rows = []
    event_indices = {25, 55, 85}
    for index in range(count):
        start_at = start + timedelta(minutes=index)
        open_price = 100.0
        close = 100.0
        high = 100.1
        low = 99.9
        if index in event_indices:
            open_price = 99.8
            close = 100.2
            high = 100.25
            low = 98.0 - (index // 30) * 0.5
        elif any(event < index <= event + 5 for event in event_indices):
            close = 104.0
            high = 104.1
            low = 99.95
        rows.append(
            Candle(
                "USA100", "M1", start_at, start_at + timedelta(minutes=1),
                open_price, high, low, close,
                tick_count=10, tick_volume=10.0, spread_points=2.0,
                source="TEST", source_id=f"reversal:{index}",
            )
        )
    return tuple(rows)


def test_reversal_library_detects_sweep_exhaustion_rejection_reclaim():
    start = datetime(2026, 6, 1, tzinfo=UTC)
    observed_at = start + timedelta(minutes=119, seconds=30)
    rows = _reversal_rows(start, 120)
    library = build_reversal_library(
        {"USA100": {"M1": rows}}, observed_at=observed_at, lookback=20, forward_horizon=5
    )
    outcomes = library.outcomes_for_symbol("USA100")
    current = library.observations_for_symbol("USA100")["M1"]

    assert len(outcomes) >= 3
    assert all(item.sweep and item.exhaustion and item.rejection and item.reclaim for item in outcomes)
    assert sum(item.follow_through for item in outcomes) >= 3
    assert current.present is False
    assert current.forward_horizon == 5
    assert len(current.source_digest) == 64


def test_engine_and_dashboard_expose_reversal_context():
    start = datetime(2026, 6, 1, tzinfo=UTC)
    observed_at = start + timedelta(minutes=119, seconds=30)
    rows = _reversal_rows(start, 120)
    library = build_reversal_library(
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
    report = IntelligenceEngine(reversal_library=library).analyse(snapshot)
    view = intelligence_view(report)

    assert report.reversal_engine_context == library.observations_for_symbol("USA100")
    assert view["reversal_engine_context"]["M1"]["exhaustion"] is False
    assert "REVERSAL_ENGINE_OBSERVED" in report.evidence
