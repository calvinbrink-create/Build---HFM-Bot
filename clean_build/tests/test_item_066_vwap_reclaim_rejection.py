from datetime import datetime, timedelta, timezone
from math import sin

from cipherfx_clean.contracts import Candle, MarketSnapshot, RawTick
from cipherfx_clean.dashboard import intelligence_view
from cipherfx_clean.intelligence.engine import IntelligenceEngine
from cipherfx_clean.intelligence.session_vwap import build_session_vwap_library
from cipherfx_clean.intelligence.vwap_reclaim_rejection import build_vwap_reclaim_rejection_library


UTC = timezone.utc


def _rows(start: datetime, count: int) -> tuple[Candle, ...]:
    rows = []
    for day_index in range(count):
        day = start + timedelta(days=day_index)
        for minute in range(24 * 60):
            start_at = day + timedelta(minutes=minute)
            price = 100.0 + day_index * 0.05 + 0.45 * sin(minute / 4.0) + minute / 10000.0
            rows.append(
                Candle(
                    "USA100", "M1", start_at, start_at + timedelta(minutes=1),
                    price, price + 0.18, price - 0.18, price + 0.04 * sin(minute),
                    tick_count=100, tick_volume=100.0, spread_points=2.0,
                    source="TEST", source_id=f"{day.date()}:{minute}",
                )
            )
    return tuple(rows)


def test_reclaim_rejection_library_records_historical_forward_outcomes():
    start = datetime(2026, 6, 1, tzinfo=UTC)
    observed_at = start + timedelta(days=49, hours=8)
    rows = _rows(start, 50)
    session_vwap = build_session_vwap_library({"USA100": rows}, observed_at=observed_at)
    library = build_vwap_reclaim_rejection_library(
        {"USA100": rows},
        observed_at=observed_at,
        session_vwap_library=session_vwap,
    )

    outcomes = library.outcomes_for_symbol("USA100")
    observed = tuple(item for item in library.statistics_for_symbol("USA100").values() if item.sample_count)
    assert outcomes
    assert len({item.event_type for item in outcomes}) >= 2
    assert observed
    assert all(item.forward_bars == 15 for item in outcomes)
    assert all(item.source_digest and len(item.source_digest) == 64 for item in outcomes)
    assert all(0.0 <= item.favourable_rate <= 1.0 for item in observed if item.favourable_rate is not None)


def test_engine_and_dashboard_expose_reclaim_rejection_history():
    start = datetime(2026, 6, 1, tzinfo=UTC)
    observed_at = start + timedelta(days=49, hours=8)
    rows = _rows(start, 50)
    session_vwap = build_session_vwap_library({"USA100": rows}, observed_at=observed_at)
    library = build_vwap_reclaim_rejection_library(
        {"USA100": rows},
        observed_at=observed_at,
        session_vwap_library=session_vwap,
    )
    snapshot = MarketSnapshot(
        "USA100",
        observed_at,
        (RawTick("USA100", observed_at, rows[-1].close, rows[-1].close + 0.1),),
        {"M1": rows[-30:]},
        {"M1": "COMPLETED"},
        tuple(item.source_id for item in rows[-30:]),
    )
    report = IntelligenceEngine(
        session_vwap_library=session_vwap,
        vwap_reclaim_rejection_library=library,
    ).analyse(snapshot)
    view = intelligence_view(report)

    assert report.vwap_reclaim_rejection_statistics == library.statistics_for_symbol("USA100")
    assert len(report.vwap_reclaim_rejection_outcomes) == len(library.outcomes_for_symbol("USA100"))
    assert view["vwap_reclaim_rejection_statistics"]
    assert "VWAP_RECLAIM_REJECTION_STATISTICS_OBSERVED" in report.evidence
