from datetime import datetime, timedelta, timezone

from cipherfx_clean.contracts import Candle, MarketSnapshot, RawTick
from cipherfx_clean.dashboard import intelligence_view
from cipherfx_clean.intelligence.engine import IntelligenceEngine
from cipherfx_clean.intelligence.opening_ranges import build_opening_range_library


UTC = timezone.utc


def _day(day: datetime) -> tuple[Candle, ...]:
    rows = []
    for minute in range(24 * 60):
        start = day + timedelta(minutes=minute)
        opening = 100.0 + minute / 100.0
        closing = opening + 0.05
        rows.append(
            Candle(
                "USA100", "M1", start, start + timedelta(minutes=1),
                opening, closing + 0.02, opening - 0.02, closing,
                tick_count=10, tick_volume=10.0, spread_points=2.0,
                source="TEST", source_id=f"{day.date()}:{minute}",
            )
        )
    return tuple(rows)


def test_opening_ranges_use_exact_first_thirty_minutes_of_each_session():
    monday = datetime(2026, 7, 13, tzinfo=UTC)
    rows = _day(monday)
    library = build_opening_range_library(
        {"USA100": rows},
        observed_at=monday + timedelta(days=1),
    )
    records = library.records_for_symbol("USA100")

    assert tuple(item.session for item in records) == ("ASIA", "LONDON", "NEW_YORK")
    expected_starts = {
        "ASIA": monday,
        "LONDON": monday + timedelta(hours=7),
        "NEW_YORK": monday + timedelta(hours=12),
    }
    for item in records:
        start = expected_starts[item.session]
        assert item.start_utc == start.isoformat()
        assert item.end_utc == (start + timedelta(minutes=30)).isoformat()
        assert item.duration_minutes == 30
        assert item.candle_count == 30
        assert item.expected_candle_count == 30
        assert item.coverage_ratio == 1.0
        assert item.complete is True
        assert item.open_price == rows[int((start - monday).total_seconds() // 60)].open
        assert item.close_price == rows[int((start - monday).total_seconds() // 60) + 29].close
        assert item.range_value == item.high_price - item.low_price
        assert item.midpoint_price == (item.high_price + item.low_price) / 2.0
        assert len(item.source_digest) == 64


def test_opening_range_history_is_read_only_dashboard_intelligence():
    monday = datetime(2026, 7, 13, tzinfo=UTC)
    rows = _day(monday)
    library = build_opening_range_library(
        {"USA100": rows},
        observed_at=monday + timedelta(days=1),
    )
    snapshot = MarketSnapshot(
        "USA100",
        monday + timedelta(days=1),
        (RawTick("USA100", rows[-1].end, rows[-1].close, rows[-1].close + 0.1),),
        {"M1": rows[-30:]},
        {"M1": "COMPLETED"},
        ("test:opening-range",),
    )
    report = IntelligenceEngine(opening_range_library=library).analyse(snapshot)
    view = intelligence_view(report)

    assert len(report.opening_range_history) == 3
    assert len(view["opening_range_history"]) == 3
    assert "OPENING_RANGE_HISTORY_OBSERVED" in report.evidence
