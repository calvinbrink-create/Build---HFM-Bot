from datetime import datetime, timedelta, timezone

from cipherfx_clean.contracts import Candle, MarketSnapshot, RawTick
from cipherfx_clean.dashboard import intelligence_view
from cipherfx_clean.intelligence.engine import IntelligenceEngine
from cipherfx_clean.intelligence.london_sweeps import build_london_sweep_library
from cipherfx_clean.intelligence.session_profiles import build_session_profile_library


UTC = timezone.utc


def _day(day: datetime, scenario: str) -> tuple[Candle, ...]:
    rows = []
    for minute in range(24 * 60):
        start = day + timedelta(minutes=minute)
        open_price = close = 100.0
        high, low = 100.5, 99.5
        if 420 <= minute < 930 and scenario == "HIGH_RECLAIM":
            if minute == 420:
                high, low, close = 101.5, 99.8, 100.0
        elif 420 <= minute < 930 and scenario == "LOW_HOLD":
            high, low, close = 99.0, 98.5, 98.8
            open_price = 98.8
        rows.append(
            Candle(
                "XAUUSD", "M1", start, start + timedelta(minutes=1),
                open_price, high, low, close,
                tick_count=10, tick_volume=10.0, spread_points=2.0,
                source="TEST", source_id=f"{day.date()}:{minute}",
            )
        )
    return tuple(rows)


def test_london_sweep_statistics_keep_every_eligible_day_in_denominator():
    monday = datetime(2026, 7, 13, tzinfo=UTC)
    rows = (
        _day(monday, "HIGH_RECLAIM")
        + _day(monday + timedelta(days=1), "NO_BREACH")
        + _day(monday + timedelta(days=2), "LOW_HOLD")
    )
    library = build_london_sweep_library(
        {"XAUUSD": rows},
        observed_at=monday + timedelta(days=3),
    )
    stats = library.statistics["XAUUSD"]
    outcomes = library.outcomes_for_symbol("XAUUSD")

    assert stats.eligible_session_count == 3
    assert stats.no_breach_session_count == 1
    assert stats.any_breach_session_count == 2
    assert stats.high_breach_count == 1
    assert stats.low_breach_count == 1
    assert stats.breach_event_count == 2
    assert stats.confirmed_sweep_count == 1
    assert stats.held_outside_count == 1
    assert stats.reclaim_rate == 0.5
    assert stats.held_outside_rate == 0.5
    assert outcomes[0].high_reclaimed is True
    assert outcomes[2].low_reclaimed is False
    assert outcomes[2].low_held_outside_at_close is True


def test_london_sweep_library_is_reported_as_evidence_not_a_gate():
    monday = datetime(2026, 7, 13, tzinfo=UTC)
    rows = _day(monday, "HIGH_RECLAIM")
    sweeps = build_london_sweep_library({"XAUUSD": rows}, observed_at=monday + timedelta(days=1))
    profiles = build_session_profile_library({"XAUUSD": rows}, observed_at=monday + timedelta(days=1))
    snapshot = MarketSnapshot(
        "XAUUSD",
        monday + timedelta(days=1),
        (RawTick("XAUUSD", rows[-1].end, 100.0, 100.1),),
        {"M1": rows[-30:]},
        {"M1": "COMPLETED"},
        ("test:london-sweep",),
    )
    report = IntelligenceEngine(session_profiles=profiles, london_sweeps=sweeps).analyse(snapshot)
    view = intelligence_view(report)

    assert report.london_sweep_statistics.eligible_session_count == 1
    assert len(report.london_sweep_outcomes) == 1
    assert view["london_sweep_statistics"]["confirmed_sweep_count"] == 1
    assert "LONDON_ASIA_SWEEP_STATISTICS_OBSERVED" in report.evidence
