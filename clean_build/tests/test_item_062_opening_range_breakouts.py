from datetime import datetime, timedelta, timezone

from cipherfx_clean.contracts import Candle, MarketSnapshot, RawTick
from cipherfx_clean.dashboard import intelligence_view
from cipherfx_clean.intelligence.engine import IntelligenceEngine
from cipherfx_clean.intelligence.opening_range_breakouts import build_opening_range_breakout_library
from cipherfx_clean.intelligence.opening_ranges import build_opening_range_library


UTC = timezone.utc


def _day(day: datetime) -> tuple[Candle, ...]:
    rows = []
    for minute in range(24 * 60):
        start = day + timedelta(minutes=minute)
        price = 101.0 if minute >= 455 else 100.0
        rows.append(
            Candle(
                "USA100", "M1", start, start + timedelta(minutes=1),
                price, price + 0.1, price - 0.1, price,
                tick_count=10, tick_volume=10.0, spread_points=2.0,
                source="TEST", source_id=f"{day.date()}:{minute}",
            )
        )
    return tuple(rows)


def test_opening_range_breakout_uses_first_close_outside_frozen_range():
    monday = datetime(2026, 7, 13, tzinfo=UTC)
    rows = _day(monday)
    ranges = build_opening_range_library({"USA100": rows}, observed_at=monday + timedelta(days=1))
    library = build_opening_range_breakout_library(
        {"USA100": rows},
        observed_at=monday + timedelta(days=1),
        opening_ranges=ranges,
    )
    outcomes = library.outcomes_for_symbol("USA100")
    london = next(item for item in outcomes if item.session == "LONDON")
    asia = next(item for item in outcomes if item.session == "ASIA")
    stats = library.statistics_for_symbol("USA100")

    assert len(outcomes) == 3
    assert asia.breakout_present is False
    assert asia.breakout_direction == "NONE"
    assert london.breakout_present is True
    assert london.breakout_direction == "UP"
    assert london.opening_range_end_utc == (monday + timedelta(hours=7, minutes=30)).isoformat()
    assert london.breakout_at_utc == (monday + timedelta(hours=7, minutes=36)).isoformat()
    assert london.minutes_after_opening_range == 6
    assert london.close_distance > 0
    assert london.maximum_favourable_excursion >= 0
    assert london.maximum_adverse_excursion >= 0
    assert stats["LONDON"].eligible_opening_range_count == 1
    assert stats["LONDON"].breakout_count == 1
    assert stats["LONDON"].breakout_rate == 1.0
    assert stats["ASIA"].no_breakout_count == 1


def test_opening_range_breakouts_are_dashboard_evidence_not_an_execution_gate():
    monday = datetime(2026, 7, 13, tzinfo=UTC)
    rows = _day(monday)
    ranges = build_opening_range_library({"USA100": rows}, observed_at=monday + timedelta(days=1))
    breakouts = build_opening_range_breakout_library(
        {"USA100": rows},
        observed_at=monday + timedelta(days=1),
        opening_ranges=ranges,
    )
    snapshot = MarketSnapshot(
        "USA100",
        monday + timedelta(days=1),
        (RawTick("USA100", rows[-1].end, rows[-1].close, rows[-1].close + 0.1),),
        {"M1": rows[-30:]},
        {"M1": "COMPLETED"},
        ("test:opening-range-breakout",),
    )
    report = IntelligenceEngine(
        opening_range_library=ranges,
        opening_range_breakouts=breakouts,
    ).analyse(snapshot)
    view = intelligence_view(report)

    assert report.opening_range_breakout_statistics["LONDON"].breakout_count == 1
    assert len(report.opening_range_breakout_outcomes) == 3
    assert view["opening_range_breakout_statistics"]["LONDON"]["breakout_rate"] == 1.0
    assert len(view["opening_range_breakout_outcomes"]) == 3
    assert "OPENING_RANGE_BREAKOUT_STATISTICS_OBSERVED" in report.evidence
