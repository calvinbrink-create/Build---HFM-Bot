from datetime import datetime, timezone

import pytest

from cipherfx_clean.intelligence.session import (
    broker_session_observation,
    session_observation,
)


UTC = timezone.utc


def test_broker_wall_time_is_reconciled_before_session_classification():
    canonical = datetime(2026, 7, 15, 13, 30, tzinfo=UTC)
    utc_epoch = int(canonical.timestamp())
    observed = broker_session_observation(
        broker_wall_epoch=utc_epoch + 3 * 3600,
        utc_epoch=utc_epoch,
        broker_utc_offset_seconds=3 * 3600,
    )

    assert observed.offset_reconciled is True
    assert observed.session.canonical_utc == canonical.isoformat()
    assert observed.session.primary_session == "LONDON_NY_OVERLAP"
    assert set(observed.session.active_sessions) == {"LONDON", "NEW_YORK"}


def test_broker_wall_time_mismatch_is_rejected_instead_of_guessed():
    utc_epoch = int(datetime(2026, 1, 15, 12, tzinfo=UTC).timestamp())
    with pytest.raises(ValueError, match="does not reconcile"):
        broker_session_observation(
            broker_wall_epoch=utc_epoch + 3 * 3600,
            utc_epoch=utc_epoch,
            broker_utc_offset_seconds=2 * 3600,
        )


def test_exchange_sessions_follow_dst_and_tokyo_remains_stable():
    winter = session_observation(datetime(2026, 1, 15, 12, tzinfo=UTC))
    summer = session_observation(datetime(2026, 7, 15, 12, tzinfo=UTC))
    winter_intervals = {item.session_id: item for item in winter.intervals}
    summer_intervals = {item.session_id: item for item in summer.intervals}

    assert winter_intervals["ASIA"].start_utc.hour == 0
    assert summer_intervals["ASIA"].start_utc.hour == 0
    assert winter_intervals["LONDON"].start_utc.hour == 8
    assert summer_intervals["LONDON"].start_utc.hour == 7
    assert winter_intervals["NEW_YORK"].start_utc.hour == 13
    assert summer_intervals["NEW_YORK"].start_utc.hour == 12


def test_weekend_is_quiet_and_naive_time_is_rejected():
    weekend = session_observation(datetime(2026, 7, 18, 13, 30, tzinfo=UTC))
    assert weekend.primary_session == "QUIET"
    assert weekend.active_sessions == ()

    with pytest.raises(ValueError, match="timezone-aware"):
        session_observation(datetime(2026, 7, 15, 13, 30))
