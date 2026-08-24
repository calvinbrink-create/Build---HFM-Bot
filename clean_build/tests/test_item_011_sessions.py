from datetime import datetime, timezone

import pytest

from cipherfx_clean.intelligence.session import active_sessions, session_intervals, session_label


UTC = timezone.utc


def test_london_session_boundaries_follow_dst_instead_of_fixed_utc_hours():
    winter = {item.session_id: item for item in session_intervals(datetime(2026, 1, 15, 12, tzinfo=UTC))}
    summer = {item.session_id: item for item in session_intervals(datetime(2026, 7, 15, 12, tzinfo=UTC))}

    assert winter["LONDON"].start_utc.hour == 8
    assert summer["LONDON"].start_utc.hour == 7
    assert winter["NEW_YORK"].start_utc.hour == 13
    assert summer["NEW_YORK"].start_utc.hour == 12


def test_overlap_and_weekend_labels_are_derived_from_local_exchange_clocks():
    overlap = datetime(2026, 7, 15, 13, 30, tzinfo=UTC)
    tokyo_open = datetime(2026, 7, 20, 0, 30, tzinfo=UTC)

    assert set(active_sessions(overlap)) == {"LONDON", "NEW_YORK"}
    assert session_label(overlap) == "LONDON_NY_OVERLAP"
    assert active_sessions(tokyo_open) == ("ASIA",)
    assert session_label(tokyo_open) == "ASIA"


def test_session_timestamps_must_be_timezone_aware():
    with pytest.raises(ValueError, match="timezone-aware"):
        session_label(datetime(2026, 7, 15, 13, 30))
