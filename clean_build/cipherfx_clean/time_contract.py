"""Explicit timestamp conversion and clock-comparison evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from zoneinfo import ZoneInfo


UTC = timezone.utc
SAST = ZoneInfo("Africa/Johannesburg")


@dataclass(frozen=True)
class TimestampLineage:
    lineage_id: str
    source_value: str
    source_timezone: str
    utc_value: str
    sast_value: str
    source_utc_offset_seconds: int


@dataclass(frozen=True)
class ClockAudit:
    observed_utc: str
    terminal_utc: str
    server_utc: str
    terminal_delta_seconds: float
    server_delta_seconds: float
    status: str


def normalize_timestamp(value: datetime, source_timezone: str) -> tuple[datetime, TimestampLineage]:
    zone = ZoneInfo(source_timezone)
    if value.tzinfo is None or value.utcoffset() is None:
        localized = value.replace(tzinfo=zone)
    else:
        localized = value.astimezone(zone)
    utc = localized.astimezone(UTC)
    sast = utc.astimezone(SAST)
    payload = f"{value.isoformat()}|{source_timezone}|{utc.isoformat()}|{sast.isoformat()}"
    lineage = TimestampLineage(
        sha256(payload.encode("utf-8")).hexdigest(),
        value.isoformat(),
        source_timezone,
        utc.isoformat(),
        sast.isoformat(),
        int(localized.utcoffset().total_seconds()),
    )
    return utc, lineage


def audit_clocks(
    *,
    observed_utc: datetime,
    terminal_utc: datetime,
    server_utc: datetime,
    tolerance_seconds: float,
) -> ClockAudit:
    rows = (observed_utc, terminal_utc, server_utc)
    if any(item.tzinfo is None or item.utcoffset() is None for item in rows):
        raise ValueError("clock audit requires timezone-aware timestamps")
    observed, terminal, server = (item.astimezone(UTC) for item in rows)
    terminal_delta = (terminal - observed).total_seconds()
    server_delta = (server - observed).total_seconds()
    status = "ALIGNED" if max(abs(terminal_delta), abs(server_delta)) <= tolerance_seconds else "MISALIGNED"
    return ClockAudit(
        observed.isoformat(),
        terminal.isoformat(),
        server.isoformat(),
        terminal_delta,
        server_delta,
        status,
    )
