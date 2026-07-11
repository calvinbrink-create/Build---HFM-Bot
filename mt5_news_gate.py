from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_EVENTS_FILE = "/opt/cipherfx_mt5/state/news_events.json"
_TRUE_VALUES = {"1", "true", "yes", "on"}

SYMBOL_EXPOSURES: dict[str, set[str]] = {
    "EURUSD": {"EUR", "USD"},
    "GBPUSD": {"GBP", "USD"},
    "USDJPY": {"USD", "JPY"},
    "AUDUSD": {"AUD", "USD"},
    "USDCHF": {"USD", "CHF"},
    "USDCAD": {"USD", "CAD"},
    "NZDUSD": {"NZD", "USD"},
    "EURGBP": {"EUR", "GBP"},
    "EURJPY": {"EUR", "JPY"},
    "GBPJPY": {"GBP", "JPY"},
    "XAUUSD": {"USD", "GOLD", "RATES", "FOMC", "CPI", "NFP"},
    "NAS100": {"USD", "US_INDEX", "FOMC", "CPI", "NFP", "PPI", "FED", "TECH"},
    "SPX500": {"USD", "US_INDEX", "FOMC", "CPI", "NFP", "PPI", "FED"},
    "US30": {"USD", "US_INDEX", "FOMC", "CPI", "NFP", "PPI", "FED"},
    "GER40": {"EUR", "GER", "ECB", "EUROZONE", "DAX"},
}


def _truthy(name: str, default: str = "0") -> bool:
    return str(os.getenv(name, default) or "").strip().lower() in _TRUE_VALUES


def _int_env(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default)) or default))
    except Exception:
        return int(default)


def _parse_time(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def _event_file() -> Path:
    return Path(os.getenv("MT5_NEWS_EVENTS_FILE", DEFAULT_EVENTS_FILE))


def _load_events() -> tuple[list[dict], str, dict]:
    path = _event_file()
    details = {"events_file": str(path)}
    if not path.exists():
        return [], "missing events file", details
    try:
        max_age = _int_env("MT5_NEWS_EVENTS_MAX_AGE_MINUTES", 1440)
        age_minutes = max(0.0, (time.time() - path.stat().st_mtime) / 60.0)
        details["events_age_minutes"] = round(age_minutes, 2)
        if max_age > 0 and age_minutes > max_age:
            return [], f"events file stale ({age_minutes:.1f}m > {max_age}m)", details
        payload = json.loads(path.read_text() or "[]")
        if not isinstance(payload, list):
            return [], "events file must contain a JSON array", details
        return [item for item in payload if isinstance(item, dict)], "OK", details
    except Exception as exc:
        return [], f"invalid events file: {str(exc)[:160]}", details


def _windows(severity: str) -> tuple[int, int]:
    sev = str(severity or "").strip().lower()
    if sev == "medium":
        return _int_env("MT5_NEWS_PRE_EVENT_MINUTES_MEDIUM", 20), _int_env("MT5_NEWS_POST_EVENT_MINUTES_MEDIUM", 10)
    if sev == "low":
        return _int_env("MT5_NEWS_PRE_EVENT_MINUTES_LOW", 0), _int_env("MT5_NEWS_POST_EVENT_MINUTES_LOW", 0)
    return _int_env("MT5_NEWS_PRE_EVENT_MINUTES_HIGH", 45), _int_env("MT5_NEWS_POST_EVENT_MINUTES_HIGH", 30)


def _matches(symbol: str, event: dict) -> tuple[bool, str]:
    canonical = str(symbol or "").strip().upper()
    exposures = SYMBOL_EXPOSURES.get(canonical, {canonical})
    event_symbols = {str(item or "").strip().upper() for item in (event.get("symbols") or [])}
    if canonical in event_symbols:
        return True, "symbol"
    event_currency = str(event.get("currency") or "").strip().upper()
    if event_currency and event_currency in exposures:
        return True, "currency"
    event_tags = {str(item or "").strip().upper() for item in (event.get("tags") or [])}
    overlap = exposures & event_tags
    if overlap:
        return True, "tag"
    return False, ""


def check_entry_allowed(symbol: str, now_utc: datetime | None = None) -> tuple[bool, str, dict]:
    enabled = _truthy("MT5_NEWS_ENTRY_GATE_ENABLE", "1")
    mode = str(os.getenv("MT5_NEWS_ENTRY_GATE_MODE", "audit") or "audit").strip().lower()
    if mode not in {"audit", "block"}:
        mode = "audit"
    if not enabled:
        return True, "news gate disabled", {"mode": mode, "enabled": False}

    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    events, load_reason, load_details = _load_events()
    details: dict[str, Any] = {"mode": mode, "enabled": True, **load_details}
    if load_reason != "OK":
        details["load_reason"] = load_reason
        if mode == "block" and _truthy("MT5_NEWS_FAIL_CLOSED", "1"):
            return False, load_reason, details
        return True, load_reason, details

    canonical = str(symbol or "").strip().upper()
    for event in events:
        match, match_type = _matches(canonical, event)
        if not match:
            continue
        event_time = _parse_time(event.get("time_utc"))
        if event_time is None:
            continue
        pre_min, post_min = _windows(str(event.get("severity") or "high"))
        delta_minutes = (now - event_time).total_seconds() / 60.0
        if -float(pre_min) <= delta_minutes <= float(post_min):
            event_details = {
                **details,
                "match_type": match_type,
                "event_id": event.get("event_id", ""),
                "event_title": event.get("title", ""),
                "event_time_utc": event_time.isoformat(),
                "severity": event.get("severity", "high"),
                "minutes_from_event": round(delta_minutes, 2),
                "pre_minutes": pre_min,
                "post_minutes": post_min,
            }
            reason = f"news window {canonical}: {event.get('title') or event.get('event_id') or 'scheduled event'}"
            if mode == "block":
                return False, reason, event_details
            return True, f"audit would block: {reason}", event_details

    details["checked_events"] = len(events)
    return True, "no matching scheduled news window", details
