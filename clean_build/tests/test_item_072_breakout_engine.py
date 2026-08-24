from datetime import datetime, timedelta, timezone

from cipherfx_clean.contracts import Candle, MarketSnapshot, RawTick
from cipherfx_clean.dashboard import intelligence_view
from cipherfx_clean.intelligence.breakout_engine import build_breakout_engine_library
from cipherfx_clean.intelligence.engine import IntelligenceEngine


UTC = timezone.utc


def _breakout_rows(start: datetime, count: int) -> tuple[Candle, ...]:
    rows = []
    for index in range(count):
        start_at = start + timedelta(minutes=index)
        close = 100.0
        if index in {25, 50, 75, 100}:
            close = 105.0 + index / 25.0
        rows.append(
            Candle(
                "USA100", "M1", start_at, start_at + timedelta(minutes=1),
                close - 0.05, close + 0.1, close - 0.1, close,
                tick_count=10, tick_volume=10.0, spread_points=2.0,
                source="TEST", source_id=f"breakout:{index}",
            )
        )
    return tuple(rows)


def test_breakout_engine_detects_prior_structure_crossings():
    start = datetime(2026, 6, 1, tzinfo=UTC)
    observed_at = start + timedelta(minutes=119, seconds=30)
    rows = _breakout_rows(start, 120)
    library = build_breakout_engine_library(
        {"USA100": {"M1": rows}}, observed_at=observed_at, lookback=20
    )
    current = library.observations_for_symbol("USA100")["M1"]

    assert len(library.history_for_symbol("USA100")) >= 3
    assert current.present is False
    assert current.range_ratio >= 0.0
    assert len(current.source_digest) == 64


def test_engine_and_dashboard_expose_breakout_context():
    start = datetime(2026, 6, 1, tzinfo=UTC)
    observed_at = start + timedelta(minutes=119, seconds=30)
    rows = _breakout_rows(start, 120)
    library = build_breakout_engine_library(
        {"USA100": {"M1": rows}}, observed_at=observed_at, lookback=20
    )
    snapshot = MarketSnapshot(
        "USA100",
        observed_at,
        (RawTick("USA100", observed_at, rows[-1].close, rows[-1].close + 0.1),),
        {"M1": rows[-30:]},
        {"M1": "COMPLETED"},
        tuple(item.source_id for item in rows[-30:]),
    )
    report = IntelligenceEngine(breakout_engine_library=library).analyse(snapshot)
    view = intelligence_view(report)

    assert report.breakout_engine_context == library.observations_for_symbol("USA100")
    assert view["breakout_engine_context"]["M1"]["present"] is False
    assert "BREAKOUT_ENGINE_OBSERVED" in report.evidence
