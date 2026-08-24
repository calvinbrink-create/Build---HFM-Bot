"""News-state tags are evidence for research, not automatic trade decisions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class NewsEvent:
    event_id: str
    symbol: str
    at: datetime
    importance: str


def news_regime(now: datetime, events: tuple[NewsEvent, ...], window: timedelta) -> str:
    if any(abs(now - event.at) <= window for event in events):
        return "NEWS_WINDOW"
    return "ORDINARY"
