from datetime import date, datetime, time, timedelta, timezone

from cipherfx_clean.calendar import (
    CalendarException,
    ObservedTradingCalendar,
    SessionWindow,
    TradingCalendar,
    observed_session_profile,
)
from cipherfx_clean.contracts import Candle, MarketSnapshot, RawTick
from cipherfx_clean.data_quality import DataQualityPolicy, validate_dataset, validate_research_window
from cipherfx_clean.hfm_data import SourceDataset
from cipherfx_clean.scheduler import CompletionScheduler
from cipherfx_clean.time_contract import audit_clocks, normalize_timestamp


UTC = timezone.utc


def test_timestamp_lineage_proves_dst_and_sast_conversion():
    winter, winter_lineage = normalize_timestamp(datetime(2026, 1, 15, 9, 0), "Europe/London")
    summer, summer_lineage = normalize_timestamp(datetime(2026, 7, 15, 9, 0), "Europe/London")

    assert winter.hour == 9
    assert summer.hour == 8
    assert winter_lineage.sast_value.endswith("+02:00")
    assert summer_lineage.sast_value.endswith("+02:00")
    assert winter_lineage.source_utc_offset_seconds == 0
    assert summer_lineage.source_utc_offset_seconds == 3600
    assert winter_lineage.lineage_id != summer_lineage.lineage_id


def test_terminal_server_clock_audit_does_not_guess_offsets():
    now = datetime(2026, 8, 21, 10, tzinfo=UTC)
    aligned = audit_clocks(observed_utc=now, terminal_utc=now + timedelta(seconds=1), server_utc=now - timedelta(seconds=1), tolerance_seconds=2)
    bad = audit_clocks(observed_utc=now, terminal_utc=now + timedelta(hours=1), server_utc=now, tolerance_seconds=2)
    assert aligned.status == "ALIGNED"
    assert bad.status == "MISALIGNED"


def test_calendar_represents_holiday_short_session_and_dst():
    calendar = TradingCalendar(
        timezone_name="Europe/London",
        sessions=(SessionWindow("LONDON", (0, 1, 2, 3, 4), time(8), time(16, 30)),),
        exceptions=(
            CalendarException(date(2026, 12, 25), "CLOSED"),
            CalendarException(date(2026, 12, 24), "SHORTENED", time(12)),
        ),
    )
    assert calendar.state(datetime(2026, 7, 1, 7, 30, tzinfo=UTC)).status == "OPEN"
    assert calendar.state(datetime(2026, 12, 25, 10, tzinfo=UTC)).reason == "EXPLICIT_HOLIDAY"
    assert calendar.state(datetime(2026, 12, 24, 13, tzinfo=UTC)).reason == "EXPLICIT_SHORTENED_SESSION"


def _m1_rows(observed):
    start = observed - timedelta(minutes=30)
    return tuple(
        Candle("UK100", "M1", start + timedelta(minutes=i), start + timedelta(minutes=i + 1), 100, 101, 99, 100.5, source="HFM", source_id=f"m1:{i}")
        for i in range(30)
    )


def test_dataset_quality_distinguishes_market_closed_from_stale_and_detects_open_gap():
    friday = datetime(2026, 8, 21, 21, 0, tzinfo=UTC)
    rows = _m1_rows(friday)
    dataset = SourceDataset("d1", "UK100", rows[0].start, rows[-1].end, {"M1": rows}, (), {"M1": "hash"}, (), {}, {})
    weekday = TradingCalendar(timezone_name="UTC", sessions=(SessionWindow("WEEKDAY", (0, 1, 2, 3, 4), time(8), time(21)),))
    policy = DataQualityPolicy(("M1",), {"M1": 20}, {"M1": timedelta(minutes=2)}, timedelta(seconds=10))

    weekend = validate_dataset(dataset, observed_at=friday + timedelta(days=2), policy=policy, calendar=weekday)
    assert weekend.status == "PASS"
    assert weekend.frames["M1"].status == "MARKET_CLOSED"

    gapped = SourceDataset("d2", "UK100", rows[0].start, rows[-1].end, {"M1": rows[:10] + rows[12:]}, (), {"M1": "hash2"}, (), {}, {})
    open_report = validate_dataset(gapped, observed_at=friday, policy=policy, calendar=weekday)
    assert open_report.status == "PASS"
    assert open_report.frames["M1"].unexplained_gaps == 1
    assert open_report.frames["M1"].status == "VALID_WITH_GAPS"
    window = validate_research_window(
        gapped.bars["M1"], timeframe="M1", calendar=weekday, minimum_bars=20
    )
    assert window.status == "EXCLUDE"
    assert "OBSERVED_BAR_GAP" in window.reasons


def test_observed_calendar_profile_labels_inference_instead_of_broker_fact():
    observed = datetime(2026, 8, 21, 21, 0, tzinfo=UTC)
    profile = observed_session_profile("UK100", _m1_rows(observed))
    assert profile.typical_bars_per_day == 30
    assert profile.source == "OBSERVED_HFM_BARS_NOT_BROKER_RULES"
    inferred = ObservedTradingCalendar(profile)
    assert inferred.state(observed - timedelta(minutes=1)).reason == "INFERRED_FROM_HFM_BARS"
    assert inferred.state(observed + timedelta(days=1)).status == "CLOSED"


def test_observed_calendar_enforces_detected_shortened_session_boundary():
    rows = []
    for day, count in ((3, 10), (10, 10), (17, 4), (24, 10)):
        start = datetime(2026, 8, day, 8, 0, tzinfo=UTC)
        rows.extend(
            Candle(
                "UK100",
                "M1",
                start + timedelta(minutes=index),
                start + timedelta(minutes=index + 1),
                100,
                101,
                99,
                100.5,
                source="HFM",
                source_id=f"{day}:{index}",
            )
            for index in range(count)
        )
    profile = observed_session_profile("UK100", tuple(rows))
    inferred = ObservedTradingCalendar(profile)

    assert "2026-08-17" in profile.shortened_dates
    assert profile.shortened_close_minutes["2026-08-17"] == 8 * 60 + 4
    assert inferred.state(datetime(2026, 8, 17, 8, 3, tzinfo=UTC)).status == "OPEN"
    after_close = inferred.state(datetime(2026, 8, 17, 8, 5, tzinfo=UTC))
    assert after_close.status == "CLOSED"
    assert after_close.reason == "OBSERVED_SHORTENED_SESSION"


def test_observed_calendar_never_learns_live_boundary_day_as_shortened():
    rows = []
    for day, count in ((3, 10), (10, 10), (17, 4)):
        start = datetime(2026, 8, day, 8, 0, tzinfo=UTC)
        rows.extend(
            Candle(
                "UK100", "M1", start + timedelta(minutes=index),
                start + timedelta(minutes=index + 1), 100, 101, 99, 100.5,
                source="HFM", source_id=f"{day}:{index}",
            )
            for index in range(count)
        )

    profile = observed_session_profile("UK100", tuple(rows))

    assert "2026-08-17" not in profile.shortened_dates
    assert "2026-08-17" not in profile.shortened_close_minutes


def test_observed_calendar_does_not_promote_one_off_minutes_to_regular_hours():
    rows = []
    for day in (3, 10, 17):
        start = datetime(2026, 8, day, 8, 0, tzinfo=UTC)
        count = 11 if day == 3 else 10
        rows.extend(
            Candle(
                "UK100", "M1", start + timedelta(minutes=index),
                start + timedelta(minutes=index + 1), 100, 101, 99, 100.5,
                source="HFM", source_id=f"{day}:{index}",
            )
            for index in range(count)
        )
    profile = observed_session_profile("UK100", tuple(rows))
    inferred = ObservedTradingCalendar(profile)

    assert 8 * 60 + 10 not in profile.weekday_open_minutes[0]
    assert inferred.state(datetime(2026, 8, 24, 8, 10, tzinfo=UTC)).status == "CLOSED"


def test_completion_scheduler_emits_each_completed_frame_once():
    observed = datetime(2026, 8, 21, 21, 0, tzinfo=UTC)
    rows = _m1_rows(observed)
    snapshot = MarketSnapshot("UK100", observed, (), {"M1": rows}, {"M1": "COMPLETED"}, ("d1",))
    scheduler = CompletionScheduler()

    assert len(scheduler.ready(snapshot)) == 1
    assert scheduler.ready(snapshot) == ()
    next_bar = Candle("UK100", "M1", observed, observed + timedelta(minutes=1), 100, 101, 99, 100.5)
    advanced = MarketSnapshot("UK100", observed + timedelta(minutes=1), (), {"M1": rows + (next_bar,)}, {"M1": "COMPLETED"}, ("d2",))
    assert scheduler.ready(advanced)[0].completed_at == observed + timedelta(minutes=1)
