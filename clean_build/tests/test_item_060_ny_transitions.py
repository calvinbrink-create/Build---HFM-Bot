from datetime import datetime, timedelta, timezone

from cipherfx_clean.contracts import Candle, MarketSnapshot, RawTick
from cipherfx_clean.dashboard import intelligence_view
from cipherfx_clean.intelligence.engine import IntelligenceEngine
from cipherfx_clean.intelligence.ny_transitions import build_ny_transition_library
from cipherfx_clean.intelligence.session_profiles import build_session_profile_library


UTC = timezone.utc


def _price(minute: int, scenario: str) -> float:
    if minute < 420:
        return 100.0
    london_sign = -1.0 if scenario == "CONTINUE_DOWN" else 1.0
    if minute < 720:
        return 100.0 + london_sign * 10.0 * (minute - 420) / 300.0
    london_close = 100.0 + london_sign * 10.0
    ny_sign = -1.0 if scenario in {"CONTINUE_DOWN", "REVERSE_DOWN"} else 1.0
    if minute < 1260:
        return london_close + ny_sign * 10.0 * (minute - 720) / 540.0
    return london_close + ny_sign * 10.0


def _day(day: datetime, scenario: str) -> tuple[Candle, ...]:
    rows = []
    for minute in range(24 * 60):
        start = day + timedelta(minutes=minute)
        opening = _price(minute, scenario)
        closing = _price(minute + 1, scenario)
        rows.append(
            Candle(
                "USA100", "M1", start, start + timedelta(minutes=1),
                opening, max(opening, closing) + 0.02, min(opening, closing) - 0.02, closing,
                tick_count=10, tick_volume=10.0, spread_points=2.0,
                source="TEST", source_id=f"{day.date()}:{minute}",
            )
        )
    return tuple(rows)


def test_ny_continuation_uses_only_london_information_before_ny_open():
    monday = datetime(2026, 7, 13, tzinfo=UTC)
    rows = (
        _day(monday, "CONTINUE_UP")
        + _day(monday + timedelta(days=1), "CONTINUE_DOWN")
        + _day(monday + timedelta(days=2), "REVERSE_DOWN")
    )
    library = build_ny_transition_library(
        {"USA100": rows},
        observed_at=monday + timedelta(days=3),
    )
    stats = library.statistics["USA100"]
    outcomes = library.outcomes_for_symbol("USA100")

    assert stats.eligible_session_count == 3
    assert stats.continuation_count == 2
    assert stats.reversal_count == 1
    assert stats.neutral_count == 0
    assert stats.continuation_rate == 2 / 3
    assert stats.reversal_rate == 1 / 3
    assert outcomes[0].london_reference_end_utc == outcomes[0].new_york_start_utc
    assert outcomes[0].london_direction == "UP"
    assert outcomes[0].new_york_direction == "UP"
    assert outcomes[1].london_direction == "DOWN"
    assert outcomes[1].new_york_direction == "DOWN"
    assert outcomes[2].continuation is False
    assert outcomes[2].reversal is True


def test_ny_transition_statistics_are_read_only_intelligence_context():
    monday = datetime(2026, 7, 13, tzinfo=UTC)
    rows = _day(monday, "CONTINUE_UP")
    transitions = build_ny_transition_library({"USA100": rows}, observed_at=monday + timedelta(days=1))
    profiles = build_session_profile_library({"USA100": rows}, observed_at=monday + timedelta(days=1))
    snapshot = MarketSnapshot(
        "USA100",
        monday + timedelta(days=1),
        (RawTick("USA100", rows[-1].end, rows[-1].close, rows[-1].close + 0.1),),
        {"M1": rows[-30:]},
        {"M1": "COMPLETED"},
        ("test:ny-transition",),
    )
    report = IntelligenceEngine(session_profiles=profiles, ny_transitions=transitions).analyse(snapshot)
    view = intelligence_view(report)

    assert report.ny_transition_statistics.continuation_count == 1
    assert len(report.ny_transition_outcomes) == 1
    assert view["ny_transition_statistics"]["continuation_rate"] == 1.0
    assert "NY_TRANSITION_STATISTICS_OBSERVED" in report.evidence
