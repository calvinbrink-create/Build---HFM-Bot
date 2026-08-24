from datetime import datetime, timedelta, timezone

import pytest

from cipherfx_clean.contracts import Candle, MarketSnapshot, RawTick
from cipherfx_clean.dashboard import intelligence_view
from cipherfx_clean.intelligence.engine import IntelligenceEngine
from cipherfx_clean.intelligence.session_profiles import (
    PROFILE_SESSIONS,
    build_session_profile_library,
)


UTC = timezone.utc


def _full_day(symbol: str, day: datetime, drift: float) -> tuple[Candle, ...]:
    rows = []
    price = 100.0
    for minute in range(24 * 60):
        start = day + timedelta(minutes=minute)
        close = price + drift
        rows.append(
            Candle(
                symbol,
                "M1",
                start,
                start + timedelta(minutes=1),
                price,
                max(price, close) + 0.05,
                min(price, close) - 0.05,
                close,
                tick_count=10,
                tick_volume=10.0 + minute % 5,
                spread_points=2.0,
                source="TEST",
                source_id=f"{symbol}:{start.isoformat()}",
            )
        )
        price = close
    return tuple(rows)


def test_session_profile_library_isolated_by_instrument_and_session():
    monday = datetime(2026, 7, 13, tzinfo=UTC)
    tuesday = monday + timedelta(days=1)
    data = {
        "XAUUSD": _full_day("XAUUSD", monday, 0.01) + _full_day("XAUUSD", tuesday, -0.005),
        "UK100": _full_day("UK100", monday, -0.01) + _full_day("UK100", tuesday, -0.005),
    }
    library = build_session_profile_library(
        data,
        observed_at=tuesday + timedelta(days=1),
    )

    assert set(library.profiles) == {
        f"{symbol}|{session}"
        for symbol in data
        for session in PROFILE_SESSIONS
    }
    assert all(profile.sample_size == 2 for profile in library.profiles.values())
    assert library.profiles["XAUUSD|LONDON"].positive_rate == 0.5
    assert library.profiles["UK100|LONDON"].positive_rate == 0.0
    assert library.profiles["XAUUSD|LONDON"].mean_return != library.profiles["UK100|LONDON"].mean_return
    assert library.profiles["XAUUSD|LONDON_NY_OVERLAP"].sample_size == 2


def test_incomplete_sessions_are_counted_but_excluded_from_statistics():
    monday = datetime(2026, 7, 13, tzinfo=UTC)
    full = _full_day("XAUUSD", monday, 0.01)
    sparse = tuple(row for index, row in enumerate(full) if index % 3 == 0)
    library = build_session_profile_library(
        {"XAUUSD": sparse},
        observed_at=monday + timedelta(days=1),
    )

    assert all(profile.sample_size == 0 for profile in library.profiles.values())
    assert all(profile.excluded_incomplete_sessions == 1 for profile in library.profiles.values())
    assert all(profile.status == "NO_COMPLETE_HISTORY" for profile in library.profiles.values())


def test_session_profiles_are_read_only_intelligence_and_dashboard_context():
    monday = datetime(2026, 7, 13, tzinfo=UTC)
    rows = _full_day("XAUUSD", monday, 0.01)
    library = build_session_profile_library(
        {"XAUUSD": rows},
        observed_at=monday + timedelta(days=1),
    )
    snapshot = MarketSnapshot(
        "XAUUSD",
        monday + timedelta(days=1),
        (RawTick("XAUUSD", rows[-1].end, rows[-1].close, rows[-1].close + 0.1),),
        {"M1": rows[-30:]},
        {"M1": "COMPLETED"},
        ("test:session-profile",),
    )
    report = IntelligenceEngine(session_profiles=library).analyse(snapshot)
    view = intelligence_view(report)

    assert set(report.session_profiles) == set(PROFILE_SESSIONS)
    assert view["session_profiles"]["LONDON"]["sample_size"] == 1
    assert "SESSION_PROFILE_LIBRARY_OBSERVED" in report.evidence


def test_profile_inputs_reject_wrong_frames_and_naive_observation_time():
    monday = datetime(2026, 7, 13, tzinfo=UTC)
    rows = list(_full_day("XAUUSD", monday, 0.01))
    rows[0] = Candle(
        "XAUUSD", "M5", rows[0].start, rows[0].end, rows[0].open,
        rows[0].high, rows[0].low, rows[0].close,
    )
    with pytest.raises(ValueError, match="M1 candles only"):
        build_session_profile_library({"XAUUSD": rows}, observed_at=monday + timedelta(days=1))
    with pytest.raises(ValueError, match="timezone-aware"):
        build_session_profile_library({}, observed_at=datetime(2026, 7, 13))
