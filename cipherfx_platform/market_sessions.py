from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Any

SAST = timezone(timedelta(hours=2), name="SAST")
FOREX_OPEN = time(23, 0)
FOREX_SATURDAY_CLOSE = time(2, 1)

US_INDEXES = {"NAS100", "US30", "SPX500", "US100CASH", "US30CASH", "US500CASH"}
ASIA_INDEXES = {"JP225", "JP225CASH"}
EUROPE_INDEXES = {"GER40", "FRA40", "EU50", "UK100", "GER40CASH", "FRA40CASH", "EU50CASH", "UK100CASH"}


def _as_sast(now: datetime | None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(SAST)


def market_window(symbol: str, asset_class: str, now: datetime | None = None) -> dict[str, Any]:
    """Return the authoritative SAST trade window for operational checks."""
    local = _as_sast(now)
    weekday = local.weekday()
    current = local.time().replace(second=0, microsecond=0)
    canonical = str(symbol or "").upper().replace("_", "")
    asset = str(asset_class or "").lower()

    if asset in {"forex", "metal"}:
        if weekday == 6:
            open_now = current >= FOREX_OPEN
        elif weekday == 5:
            open_now = current < FOREX_SATURDAY_CLOSE
        else:
            open_now = True
        return {
            "open": open_now,
            "market": "FOREX" if asset == "forex" else "METALS",
            "window_sast": "Sunday 23:00 to Saturday 02:00",
            "reason": "active_weekday_window" if open_now else "weekend_closed",
            "sast": local.isoformat(),
        }

    if asset == "index":
        # These symbols are traded as CFDs, not the underlying cash equity
        # index - brokers offer them on close to a forex-style near-24/5
        # schedule, not the narrow cash-session hours (e.g. NYSE 09:30-16:00)
        # that used to be hardcoded here. Verified 2026-07-20 against this
        # account's own trade history: US30/GER40/UK100/JP225 all have real,
        # successfully-filled historical trades spread across nearly every
        # hour of the SAST clock, well outside the old narrow windows -
        # meaning the old check was blocking hours the broker actually
        # allows and this account had already traded in. Kept as a distinct
        # branch from forex/metals (not merged into it) so a genuine
        # difference can still be reintroduced per-region if ever needed.
        if canonical in US_INDEXES:
            market = "US"
        elif canonical in ASIA_INDEXES:
            market = "ASIA"
        elif canonical in EUROPE_INDEXES:
            market = "UK_EUROPE"
        else:
            return {
                "open": False,
                "market": "INDEX",
                "window_sast": "unclassified index",
                "reason": "unclassified_index",
                "sast": local.isoformat(),
            }
        if weekday == 6:
            open_now = current >= FOREX_OPEN
        elif weekday == 5:
            open_now = current < FOREX_SATURDAY_CLOSE
        else:
            open_now = True
        return {
            "open": open_now,
            "market": market,
            "window_sast": "Sunday 23:00 to Saturday 02:00 SAST",
            "reason": "active_weekday_window" if open_now else "weekend_closed",
            "sast": local.isoformat(),
        }

    return {
        "open": False,
        "market": "UNKNOWN",
        "window_sast": "not configured",
        "reason": "unclassified_asset",
        "sast": local.isoformat(),
    }
