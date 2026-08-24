from datetime import datetime, timedelta, timezone

from cipherfx_clean.contracts import Candle, MarketSnapshot, RawTick
from cipherfx_clean.dashboard import intelligence_view
from cipherfx_clean.intelligence.continuation_engine import build_continuation_library
from cipherfx_clean.intelligence.engine import IntelligenceEngine


UTC = timezone.utc


def _continuation_rows(start: datetime, count: int) -> tuple[Candle, ...]:
    rows = []
    event_indices = {25, 55, 85}
    for index in range(count):
        start_at = start + timedelta(minutes=index)
        open_price = 100.0
        close = 100.0
        high = 100.1
        low = 99.9
        for event in event_indices:
            if index == event - 3:
                open_price = 100.8
                close = 101.0
                high = 101.1
                low = 100.7
            elif index == event - 2:
                open_price = 100.7
                close = 100.7
                high = 100.8
                low = 100.6
            elif index == event - 1:
                open_price = 100.9
                close = 100.8
                high = 100.95
                low = 100.7
            elif index == event:
                open_price = 101.0
                close = 101.2
                high = 101.3
                low = 100.95
            elif event < index <= event + 5:
                open_price = 101.8
                close = 102.0
                high = 102.1
                low = 101.7
        rows.append(
            Candle(
                "USA100", "M1", start_at, start_at + timedelta(minutes=1),
                open_price, high, low, close,
                tick_count=10, tick_volume=10.0, spread_points=2.0,
                source="TEST", source_id=f"continuation:{index}",
            )
        )
    return tuple(rows)


def test_continuation_library_detects_pullback_resumption():
    start = datetime(2026, 6, 1, tzinfo=UTC)
    observed_at = start + timedelta(minutes=119, seconds=30)
    rows = _continuation_rows(start, 120)
    library = build_continuation_library(
        {"USA100": {"M1": rows}}, observed_at=observed_at, lookback=20, forward_horizon=5
    )
    outcomes = library.outcomes_for_symbol("USA100")
    current = library.observations_for_symbol("USA100")["M1"]

    assert len(outcomes) >= 3
    assert all(item.impulse and item.pullback and item.resumed for item in outcomes)
    assert sum(item.follow_through for item in outcomes) >= 3
    assert current.present is False
    assert current.forward_horizon == 5
    assert len(current.source_digest) == 64


def test_engine_and_dashboard_expose_continuation_context():
    start = datetime(2026, 6, 1, tzinfo=UTC)
    observed_at = start + timedelta(minutes=119, seconds=30)
    rows = _continuation_rows(start, 120)
    library = build_continuation_library(
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
    report = IntelligenceEngine(continuation_library=library).analyse(snapshot)
    view = intelligence_view(report)

    assert report.continuation_engine_context == library.observations_for_symbol("USA100")
    assert view["continuation_engine_context"]["M1"]["present"] is False
    assert "CONTINUATION_ENGINE_OBSERVED" in report.evidence
