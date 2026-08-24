from datetime import datetime, timedelta, timezone

from cipherfx_clean.contracts import Candle, MarketSnapshot, RawTick
from cipherfx_clean.dashboard import intelligence_view
from cipherfx_clean.intelligence.engine import IntelligenceEngine
from cipherfx_clean.intelligence.false_opening_range_breakouts import (
    build_false_opening_range_breakout_library,
)
from cipherfx_clean.intelligence.opening_range_breakouts import build_opening_range_breakout_library
from cipherfx_clean.intelligence.opening_ranges import build_opening_range_library


UTC = timezone.utc


def _day(day: datetime) -> tuple[Candle, ...]:
    rows = []
    for minute in range(24 * 60):
        start = day + timedelta(minutes=minute)
        price = 101.0 if 455 <= minute < 460 else 100.0
        rows.append(
            Candle(
                "USA100", "M1", start, start + timedelta(minutes=1),
                price, price + 0.1, price - 0.1, price,
                tick_count=10, tick_volume=10.0, spread_points=2.0,
                source="TEST", source_id=f"{day.date()}:{minute}",
            )
        )
    return tuple(rows)


def _libraries(rows, observed_at):
    ranges = build_opening_range_library({"USA100": rows}, observed_at=observed_at)
    breakouts = build_opening_range_breakout_library(
        {"USA100": rows}, observed_at=observed_at, opening_ranges=ranges,
    )
    failures = build_false_opening_range_breakout_library(
        {"USA100": rows}, observed_at=observed_at, breakouts=breakouts,
    )
    return ranges, breakouts, failures


def test_false_orb_requires_a_later_close_back_across_the_broken_boundary():
    monday = datetime(2026, 7, 13, tzinfo=UTC)
    rows = _day(monday)
    _, breakouts, failures = _libraries(rows, monday + timedelta(days=1))
    outcomes = failures.outcomes_for_symbol("USA100")
    london = outcomes[0]
    stats = failures.statistics_for_symbol("USA100")["LONDON"]

    assert sum(item.breakout_present for item in breakouts.outcomes_for_symbol("USA100")) == 1
    assert len(outcomes) == 1
    assert london.session == "LONDON"
    assert london.breakout_direction == "UP"
    assert london.reclaimed is True
    assert london.breakout_at_utc == (monday + timedelta(hours=7, minutes=36)).isoformat()
    assert london.reclaimed_at_utc == (monday + timedelta(hours=7, minutes=41)).isoformat()
    assert london.minutes_to_reclaim == 5
    assert london.reclaim_close <= london.broken_level
    assert london.maximum_extension_before_reclaim > 0
    assert stats.eligible_breakout_count == 1
    assert stats.false_breakout_count == 1
    assert stats.unreclaimed_breakout_count == 0
    assert stats.false_breakout_rate == 1.0


def test_false_orb_library_is_dashboard_evidence_not_a_gate():
    monday = datetime(2026, 7, 13, tzinfo=UTC)
    rows = _day(monday)
    ranges, breakouts, failures = _libraries(rows, monday + timedelta(days=1))
    snapshot = MarketSnapshot(
        "USA100",
        monday + timedelta(days=1),
        (RawTick("USA100", rows[-1].end, rows[-1].close, rows[-1].close + 0.1),),
        {"M1": rows[-30:]},
        {"M1": "COMPLETED"},
        ("test:false-opening-range-breakout",),
    )
    report = IntelligenceEngine(
        opening_range_library=ranges,
        opening_range_breakouts=breakouts,
        false_opening_range_breakouts=failures,
    ).analyse(snapshot)
    view = intelligence_view(report)

    assert report.false_opening_range_breakout_statistics["LONDON"].false_breakout_count == 1
    assert len(report.false_opening_range_breakout_outcomes) == 1
    assert view["false_opening_range_breakout_statistics"]["LONDON"]["false_breakout_rate"] == 1.0
    assert len(view["false_opening_range_breakout_outcomes"]) == 1
    assert "FALSE_OPENING_RANGE_BREAKOUT_STATISTICS_OBSERVED" in report.evidence
