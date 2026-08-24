from datetime import datetime, timedelta, timezone

from cipherfx_clean.contracts import Candle, MarketSnapshot, RawTick
from cipherfx_clean.dashboard import intelligence_view
from cipherfx_clean.intelligence.cross_asset import build_cross_asset_library
from cipherfx_clean.intelligence.engine import IntelligenceEngine


UTC = timezone.utc


def _rows(symbol: str, start: datetime, count: int, base: float, slope: float) -> tuple[Candle, ...]:
    rows = []
    for index in range(count):
        start_at = start + timedelta(minutes=index)
        close = base + index * slope + (index % 3) * slope * 0.25
        rows.append(
            Candle(
                symbol, "M1", start_at, start_at + timedelta(minutes=1),
                close - slope * 0.2, close + slope * 0.4, close - slope * 0.4, close,
                tick_count=10, tick_volume=10.0, spread_points=2.0,
                source="TEST", source_id=f"cross:{symbol}:{index}",
            )
        )
    return tuple(rows)


def _library():
    start = datetime(2026, 6, 1, tzinfo=UTC)
    observed_at = start + timedelta(minutes=79, seconds=30)
    return build_cross_asset_library(
        {
            "USA100": {"M1": _rows("USA100", start, 80, 100.0, 0.1)},
            "USA500": {"M1": _rows("USA500", start, 80, 200.0, 0.2)},
            "XAUUSD": {"M1": _rows("XAUUSD", start, 80, 300.0, -0.1)},
        },
        observed_at=observed_at,
        window=20,
        baseline_window=60,
    ), observed_at


def test_cross_asset_library_exposes_aligned_correlations_and_breakdowns():
    library, _ = _library()
    observations = library.observations_for_symbol("USA100")
    correlations = library.correlations_for_symbol("USA100")
    breakdowns = library.breakdowns_for_symbol("USA100")

    assert observations["M1"].peer_symbols == ("USA500", "XAUUSD")
    assert observations["M1"].aligned_sample_size == 78
    assert len(correlations) == 2
    assert len(breakdowns) == 2
    assert all(item.sample_size == 20 for item in correlations.values())
    assert all(len(item.source_digest) == 64 for item in breakdowns.values())


def test_engine_and_dashboard_expose_cross_asset_layers():
    library, observed_at = _library()
    rows = _rows("USA100", observed_at - timedelta(minutes=79, seconds=30), 80, 100.0, 0.1)
    snapshot = MarketSnapshot(
        "USA100",
        observed_at,
        (RawTick("USA100", observed_at, rows[-1].close, rows[-1].close + 0.1),),
        {"M1": rows[-30:]},
        {"M1": "COMPLETED"},
        tuple(item.source_id for item in rows[-30:]),
    )
    report = IntelligenceEngine(cross_asset_library=library).analyse(snapshot)
    view = intelligence_view(report)

    assert report.cross_asset_context == library.observations_for_symbol("USA100")
    assert report.correlation_context == library.correlations_for_symbol("USA100")
    assert report.correlation_breakdown_context == library.breakdowns_for_symbol("USA100")
    assert "CROSS_ASSET_DATA_OBSERVED" in report.evidence
    assert "CORRELATION_ENGINE_OBSERVED" in report.evidence
    assert view["correlation_context"]
