from datetime import datetime, timedelta, timezone

from cipherfx_clean.contracts import Candle, MarketSnapshot, RawTick
from cipherfx_clean.dashboard import intelligence_view
from cipherfx_clean.intelligence.engine import IntelligenceEngine
from cipherfx_clean.intelligence.session_vwap import build_session_vwap_library
from cipherfx_clean.intelligence.vwap_deviation_stats import build_vwap_deviation_library


UTC = timezone.utc


def _days(start: datetime, count: int) -> tuple[Candle, ...]:
    rows = []
    for day_index in range(count):
        day = start + timedelta(days=day_index)
        for minute in range(24 * 60):
            start_at = day + timedelta(minutes=minute)
            price = 100.0 + day_index * 0.2 + minute / 1000.0
            rows.append(
                Candle(
                    "USA100", "M1", start_at, start_at + timedelta(minutes=1),
                    price, price + 0.2, price - 0.1, price + 0.1,
                    tick_count=minute + 1, tick_volume=float(minute + 1), spread_points=2.0,
                    source="TEST", source_id=f"{day.date()}:{minute}",
                )
            )
    return tuple(rows)


def test_vwap_deviation_uses_completed_session_distributions_and_current_session_separately():
    start = datetime(2026, 6, 1, tzinfo=UTC)
    observed_at = start + timedelta(days=49, hours=8)
    rows = _days(start, 50)
    session_library = build_session_vwap_library({"USA100": rows}, observed_at=observed_at)
    library = build_vwap_deviation_library(session_library, minimum_sample_size=30)

    current = library.current_for_symbol("USA100", observed_at)
    assert current is not None
    assert current.session == "LONDON"
    assert current.historical_sample_size >= 30
    assert current.zscore is not None
    assert current.absolute_distance_percentile is not None
    assert 0.0 <= current.absolute_distance_percentile <= 1.0
    assert {item.session for item in library.distributions} == {"ASIA", "LONDON", "NEW_YORK"}
    assert all(item.sample_size >= 30 for item in library.distributions)
    assert all(item.complete_session is False for item in session_library.observations if item.observed_through_utc == observed_at.isoformat())


def test_engine_and_dashboard_expose_vwap_deviation_statistics():
    start = datetime(2026, 6, 1, tzinfo=UTC)
    observed_at = start + timedelta(days=49, hours=8)
    rows = _days(start, 50)
    session_library = build_session_vwap_library({"USA100": rows}, observed_at=observed_at)
    deviation_library = build_vwap_deviation_library(session_library, minimum_sample_size=30)
    current = deviation_library.current_for_symbol("USA100", observed_at)
    snapshot = MarketSnapshot(
        "USA100",
        observed_at,
        (RawTick("USA100", observed_at, rows[-1].close, rows[-1].close + 0.1),),
        {"M1": rows[-30:]},
        {"M1": "COMPLETED"},
        tuple(item.source_id for item in rows[-30:]),
    )
    report = IntelligenceEngine(
        session_vwap_library=session_library,
        vwap_deviation_library=deviation_library,
    ).analyse(snapshot)
    view = intelligence_view(report)

    assert report.vwap_deviation_context == current
    assert view["vwap_deviation_context"]["historical_sample_size"] >= 30
    assert "VWAP_DEVIATION_STATISTICS_OBSERVED" in report.evidence
