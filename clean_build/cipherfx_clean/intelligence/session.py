"""Timezone-aware research sessions over one canonical UTC timeline."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone
from typing import Sequence
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class SessionDefinition:
    session_id: str
    timezone_name: str
    open_local: time
    close_local: time
    weekdays: tuple[int, ...] = (0, 1, 2, 3, 4)


@dataclass(frozen=True)
class SessionInterval:
    session_id: str
    start_utc: datetime
    end_utc: datetime
    timezone_name: str


@dataclass(frozen=True)
class SessionObservation:
    canonical_utc: str
    primary_session: str
    active_sessions: tuple[str, ...]
    intervals: tuple[SessionInterval, ...]
    source: str = "IANA_EXCHANGE_CLOCKS_FROM_CANONICAL_UTC"


@dataclass(frozen=True)
class BrokerSessionObservation:
    broker_wall_epoch: int
    utc_epoch: int
    broker_utc_offset_seconds: int
    offset_reconciled: bool
    session: SessionObservation
    source: str = "BROKER_WALL_EPOCH_RECONCILED_TO_EXPLICIT_UTC"


RESEARCH_SESSIONS = (
    SessionDefinition("ASIA", "Asia/Tokyo", time(9, 0), time(16, 0)),
    SessionDefinition("LONDON", "Europe/London", time(8, 0), time(16, 30)),
    SessionDefinition("NEW_YORK", "America/New_York", time(8, 0), time(17, 0)),
)


def session_intervals(
    timestamp: datetime,
    definitions: Sequence[SessionDefinition] = RESEARCH_SESSIONS,
) -> tuple[SessionInterval, ...]:
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("session timestamp must be timezone-aware")
    output = []
    for definition in definitions:
        zone = ZoneInfo(definition.timezone_name)
        local = timestamp.astimezone(zone)
        start = datetime.combine(local.date(), definition.open_local, zone)
        end = datetime.combine(local.date(), definition.close_local, zone)
        if end <= start:
            from datetime import timedelta

            end += timedelta(days=1)
        output.append(
            SessionInterval(
                definition.session_id,
                start.astimezone(ZoneInfo("UTC")),
                end.astimezone(ZoneInfo("UTC")),
                definition.timezone_name,
            )
        )
    return tuple(output)


def active_sessions(
    timestamp: datetime,
    definitions: Sequence[SessionDefinition] = RESEARCH_SESSIONS,
) -> tuple[str, ...]:
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("session timestamp must be timezone-aware")
    intervals = session_intervals(timestamp, definitions)
    return tuple(
        interval.session_id
        for definition, interval in zip(definitions, intervals)
        if timestamp.astimezone(ZoneInfo(definition.timezone_name)).weekday() in definition.weekdays
        and interval.start_utc <= timestamp < interval.end_utc
    )


def session_label(timestamp: datetime) -> str:
    active = active_sessions(timestamp)
    if "LONDON" in active and "NEW_YORK" in active:
        return "LONDON_NY_OVERLAP"
    if "LONDON" in active:
        return "LONDON"
    if "NEW_YORK" in active:
        return "NEW_YORK"
    if "ASIA" in active:
        return "ASIA"
    return "QUIET"


def session_observation(timestamp: datetime) -> SessionObservation:
    """Classify sessions only after converting an aware timestamp to UTC."""

    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("session timestamp must be timezone-aware")
    canonical = timestamp.astimezone(timezone.utc)
    return SessionObservation(
        canonical_utc=canonical.isoformat(),
        primary_session=session_label(canonical),
        active_sessions=active_sessions(canonical),
        intervals=session_intervals(canonical),
    )


def broker_session_observation(
    *,
    broker_wall_epoch: int,
    utc_epoch: int,
    broker_utc_offset_seconds: int,
) -> BrokerSessionObservation:
    """Reject unreconciled broker time and classify the explicit UTC instant."""

    observed_offset = int(broker_wall_epoch) - int(utc_epoch)
    expected_offset = int(broker_utc_offset_seconds)
    if observed_offset != expected_offset:
        raise ValueError(
            "broker timestamp does not reconcile to UTC: "
            f"observed_offset={observed_offset} exported_offset={expected_offset}"
        )
    canonical = datetime.fromtimestamp(int(utc_epoch), timezone.utc)
    return BrokerSessionObservation(
        broker_wall_epoch=int(broker_wall_epoch),
        utc_epoch=int(utc_epoch),
        broker_utc_offset_seconds=expected_offset,
        offset_reconciled=True,
        session=session_observation(canonical),
    )
