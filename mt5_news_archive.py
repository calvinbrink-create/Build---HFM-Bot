#!/usr/bin/env python3
from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


DEFAULT_ARCHIVE_FILE = Path("/opt/cipherfx_mt5/data/mt5_news_calendar_archive.csv")
DEFAULT_BLACKOUT_MINUTES = 45

_CACHE_PATH: Path | None = None
_CACHE_MTIME: float | None = None
_CACHE_EVENTS: list["NewsEvent"] = []


@dataclass(frozen=True)
class NewsEvent:
    timestamp_utc: datetime
    event: str
    currency: str
    impact: str
    kind: str
    source: str
    source_url: str
    headline: str = ""
    sentiment: str = ""


def archive_path() -> Path:
    return Path(os.getenv("MT5_NEWS_ARCHIVE_FILE", str(DEFAULT_ARCHIVE_FILE)))


def _parse_timestamp(value: str) -> datetime:
    text = (value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _normalize_set(values: set[str] | list[str] | tuple[str, ...] | None) -> set[str] | None:
    if values is None:
        return None
    normalized = {str(item).strip().lower() for item in values if str(item).strip()}
    return normalized or None


def load_events(path: str | Path | None = None) -> list[NewsEvent]:
    global _CACHE_EVENTS, _CACHE_MTIME, _CACHE_PATH
    archive = Path(path) if path is not None else archive_path()
    try:
        mtime = archive.stat().st_mtime
    except FileNotFoundError:
        _CACHE_PATH = archive
        _CACHE_MTIME = None
        _CACHE_EVENTS = []
        return []

    if _CACHE_PATH == archive and _CACHE_MTIME == mtime:
        return list(_CACHE_EVENTS)

    events: list[NewsEvent] = []
    with archive.open(newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            try:
                events.append(
                    NewsEvent(
                        timestamp_utc=_parse_timestamp(row.get("timestamp_utc", "")),
                        event=(row.get("event") or "").strip(),
                        currency=(row.get("currency") or "").strip().upper(),
                        impact=(row.get("impact") or "").strip().lower(),
                        kind=(row.get("kind") or "").strip().lower(),
                        source=(row.get("source") or "").strip(),
                        source_url=(row.get("source_url") or "").strip(),
                        headline=(row.get("headline") or "").strip(),
                        sentiment=(row.get("sentiment") or "").strip(),
                    )
                )
            except Exception:
                continue
    events.sort(key=lambda event: event.timestamp_utc)
    _CACHE_PATH = archive
    _CACHE_MTIME = mtime
    _CACHE_EVENTS = events
    return list(events)


def events_in_window(
    at_utc: datetime | None = None,
    before_minutes: int | float | None = None,
    after_minutes: int | float | None = None,
    currencies: set[str] | list[str] | tuple[str, ...] | None = ("USD",),
    impacts: set[str] | list[str] | tuple[str, ...] | None = ("high",),
    kinds: set[str] | list[str] | tuple[str, ...] | None = None,
) -> list[NewsEvent]:
    anchor = at_utc or datetime.now(timezone.utc)
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    anchor = anchor.astimezone(timezone.utc)
    before = float(before_minutes if before_minutes is not None else DEFAULT_BLACKOUT_MINUTES)
    after = float(after_minutes if after_minutes is not None else DEFAULT_BLACKOUT_MINUTES)
    start = anchor - timedelta(minutes=before)
    end = anchor + timedelta(minutes=after)
    currency_filter = _normalize_set(currencies)
    impact_filter = _normalize_set(impacts)
    kind_filter = _normalize_set(kinds)

    matches: list[NewsEvent] = []
    for event in load_events():
        if event.timestamp_utc < start or event.timestamp_utc > end:
            continue
        if currency_filter and event.currency.lower() not in currency_filter:
            continue
        if impact_filter and event.impact.lower() not in impact_filter:
            continue
        if kind_filter and event.kind.lower() not in kind_filter:
            continue
        matches.append(event)
    return matches


def blackout_state(
    at_utc: datetime | None = None,
    before_minutes: int | float | None = None,
    after_minutes: int | float | None = None,
) -> dict:
    matches = events_in_window(at_utc, before_minutes, after_minutes)
    if not matches:
        return {"blackout": False, "reason": "", "events": []}
    labels = []
    for event in matches:
        labels.append(f"{event.kind}:{event.timestamp_utc.isoformat().replace('+00:00', 'Z')}")
    return {
        "blackout": True,
        "reason": "; ".join(labels),
        "events": matches,
    }


def is_event_blackout(
    at_utc: datetime | None = None,
    before_minutes: int | float | None = None,
    after_minutes: int | float | None = None,
) -> bool:
    return bool(blackout_state(at_utc, before_minutes, after_minutes).get("blackout"))
