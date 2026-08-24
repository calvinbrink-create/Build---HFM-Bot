"""Injected trading calendars and observed broker-session reconciliation."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from statistics import median
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo

from .contracts import Candle


@dataclass(frozen=True)
class SessionWindow:
    session_id: str
    weekdays: tuple[int, ...]
    open_local: time
    close_local: time


@dataclass(frozen=True)
class CalendarException:
    local_date: date
    status: str
    close_local: time | None = None
    source: str = "EXPLICIT"


@dataclass(frozen=True)
class MarketTimeState:
    status: str
    session_id: str | None
    local_timestamp: str
    reason: str


@dataclass(frozen=True)
class ObservedSessionProfile:
    symbol: str
    timezone: str
    weekday_open_minutes: Mapping[int, tuple[int, ...]]
    typical_bars_per_day: int
    closed_dates: tuple[str, ...]
    shortened_dates: tuple[str, ...]
    source: str
    shortened_close_minutes: Mapping[str, int] = field(default_factory=dict)


class TradingCalendar:
    def __init__(
        self,
        *,
        timezone_name: str,
        sessions: Sequence[SessionWindow],
        exceptions: Sequence[CalendarException] = (),
    ):
        self.timezone_name = timezone_name
        self._zone = ZoneInfo(timezone_name)
        self._sessions = tuple(sessions)
        self._exceptions = {item.local_date: item for item in exceptions}
        if not self._sessions:
            raise ValueError("at least one explicit session is required")

    def state(self, at: datetime) -> MarketTimeState:
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("calendar requires a timezone-aware timestamp")
        local = at.astimezone(self._zone)
        exception = self._exceptions.get(local.date())
        if exception is not None and exception.status == "CLOSED":
            return MarketTimeState("CLOSED", None, local.isoformat(), "EXPLICIT_HOLIDAY")
        for session in self._sessions:
            if local.weekday() not in session.weekdays:
                continue
            if _inside(local.timetz().replace(tzinfo=None), session.open_local, session.close_local):
                if exception is not None and exception.status == "SHORTENED" and exception.close_local is not None:
                    if local.timetz().replace(tzinfo=None) >= exception.close_local:
                        return MarketTimeState("CLOSED", session.session_id, local.isoformat(), "EXPLICIT_SHORTENED_SESSION")
                return MarketTimeState("OPEN", session.session_id, local.isoformat(), "EXPLICIT_SESSION")
        return MarketTimeState("CLOSED", None, local.isoformat(), "OUTSIDE_EXPLICIT_SESSION")

    def expected_open(self, start: datetime, end: datetime, *, step: timedelta) -> bool:
        cursor = start
        while cursor < end:
            if self.state(cursor).status == "OPEN":
                return True
            cursor += step
        return False


class ObservedTradingCalendar:
    """Calendar reconstructed from HFM bars and explicitly labelled inferred."""

    def __init__(self, profile: ObservedSessionProfile):
        self.profile = profile
        self.timezone_name = profile.timezone
        self._zone = ZoneInfo(profile.timezone)
        self._closed = frozenset(profile.closed_dates)
        self._shortened_close = dict(profile.shortened_close_minutes)

    def state(self, at: datetime) -> MarketTimeState:
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("calendar requires a timezone-aware timestamp")
        local = at.astimezone(self._zone)
        if local.date().isoformat() in self._closed:
            return MarketTimeState("CLOSED", None, local.isoformat(), "OBSERVED_CLOSED_DATE")
        minute = local.hour * 60 + local.minute
        shortened_close = self._shortened_close.get(local.date().isoformat())
        if shortened_close is not None and minute >= shortened_close:
            return MarketTimeState(
                "CLOSED",
                "OBSERVED_SESSION",
                local.isoformat(),
                "OBSERVED_SHORTENED_SESSION",
            )
        if minute in self.profile.weekday_open_minutes.get(local.weekday(), ()):
            return MarketTimeState("OPEN", "OBSERVED_SESSION", local.isoformat(), "INFERRED_FROM_HFM_BARS")
        return MarketTimeState("CLOSED", None, local.isoformat(), "OUTSIDE_OBSERVED_SESSION")

    def expected_open(self, start: datetime, end: datetime, *, step: timedelta) -> bool:
        cursor = start
        while cursor < end:
            if self.state(cursor).status == "OPEN":
                return True
            cursor += min(step, timedelta(minutes=1))
        return False


def observed_session_profile(
    symbol: str,
    bars: Sequence[Candle],
    *,
    timezone_name: str = "UTC",
) -> ObservedSessionProfile:
    zone = ZoneInfo(timezone_name)
    by_date: dict[date, list[datetime]] = defaultdict(list)
    weekday_minutes: dict[int, Counter[int]] = defaultdict(Counter)
    for bar in bars:
        local = bar.start.astimezone(zone)
        by_date[local.date()].append(local)
        weekday_minutes[local.weekday()][local.hour * 60 + local.minute] += 1
    counts = tuple(len(rows) for rows in by_date.values())
    typical = int(median(counts)) if counts else 0
    dates_per_weekday = Counter(day.weekday() for day in by_date)
    open_minutes = {
        weekday: tuple(
            sorted(
                minute
                for minute, count in minute_counts.items()
                if count >= max(1, (dates_per_weekday[weekday] + 1) // 2)
            )
        )
        for weekday, minute_counts in weekday_minutes.items()
    }
    if by_date:
        first, last = min(by_date), max(by_date)
        cursor = first
        closed: list[str] = []
        while cursor <= last:
            if cursor.weekday() in open_minutes and cursor not in by_date:
                closed.append(cursor.isoformat())
            cursor += timedelta(days=1)
    else:
        closed = []
    # The first and last dates are ingestion boundaries, not proven complete
    # sessions.  In particular, the last date is commonly the live, still-
    # forming trading day and must never be learned as an early close.
    boundary_dates = {min(by_date), max(by_date)} if by_date else set()
    shortened = tuple(
        day.isoformat()
        for day, rows in sorted(by_date.items())
        if day not in boundary_dates and typical and len(rows) < typical * 0.65
    )
    shortened_close_minutes = {
        day.isoformat(): _observed_close_minute(day, rows, zone)
        for day, rows in sorted(by_date.items())
        if day.isoformat() in shortened
    }
    return ObservedSessionProfile(
        symbol,
        timezone_name,
        open_minutes,
        typical,
        tuple(closed),
        shortened,
        "OBSERVED_HFM_BARS_NOT_BROKER_RULES",
        shortened_close_minutes,
    )


def _observed_close_minute(day: date, rows: Sequence[datetime], zone: ZoneInfo) -> int:
    latest = max(rows)
    # ``rows`` contain bar starts; the observed M1 session closes one minute
    # after its final start. Crossing midnight is represented as minute 1440.
    close = latest + timedelta(minutes=1)
    if close.date() > day:
        return 1440
    return close.hour * 60 + close.minute


def _inside(current: time, start: time, end: time) -> bool:
    if start == end:
        return True
    if start <= end:
        return start <= current < end
    return current >= start or current < end
