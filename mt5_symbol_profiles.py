#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

DEFAULT_PROFILE_PATH = Path("/opt/cipherfx_mt5/config/symbol_profiles.json")
PROFILE_ENV = "MT5_SYMBOL_PROFILES_FILE"
PROFILE_FIELDS = [
    "enabled",
    "market",
    "allowed_strategies",
    "blocked_strategies",
    "min_score",
    "risk_pct",
    "max_lot",
    "max_position_pct",
    "max_trade_risk_usd",
    "max_open_position_loss_usd",
    "max_symbol_floating_loss_usd",
    "max_stop_distance",
    "max_spread_pips",
    "max_spread_points",
    "max_spread_atr_ratio",
    "max_spread_atr_frac",
    "max_spread_pct",
    "scalp_tp_r",
    "scalp_close_r",
    "breakeven_trigger_r",
    "trail_start_r",
    "news_exposures",
    "max_daily_trades",
    "max_daily_losses",
    "cooldown_minutes",
    "max_duration_minutes",
    "tighten_after_minutes",
    "tighten_if_profit_below_usd",
    "strategy_type",
    "sessions",
    "risk_tier",
    "volatility_filter",
    "entry_model",
    "exit_model",
]
PRIORITY_SYMBOLS = [
    "NAS100", "SPX500", "US30", "GER40", "XAUUSD", "EURUSD", "GBPUSD", "USDJPY",
    "AUDUSD", "USDCHF", "USDCAD", "NZDUSD", "EURGBP", "EURJPY", "GBPJPY",
]


def profile_path() -> Path:
    return Path(os.getenv(PROFILE_ENV, str(DEFAULT_PROFILE_PATH))).expanduser()


def canonical_symbol(symbol: str) -> str:
    return str(symbol or "").strip().upper()


def load_profiles(path: str | Path | None = None) -> dict[str, dict[str, Any]]:
    source = Path(path).expanduser() if path else profile_path()
    try:
        payload = json.loads(source.read_text())
    except FileNotFoundError:
        return {}
    if isinstance(payload, dict) and isinstance(payload.get("symbols"), dict):
        rows = payload["symbols"]
    elif isinstance(payload, dict):
        rows = payload
    else:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for key, value in rows.items():
        symbol = canonical_symbol(str(key))
        if symbol and isinstance(value, dict):
            out[symbol] = dict(value)
    return out


def get_profile(symbol: str, profiles: dict[str, dict[str, Any]] | None = None) -> dict[str, Any] | None:
    rows = profiles if profiles is not None else load_profiles()
    return rows.get(canonical_symbol(symbol))


def field_is_configured(profile: dict[str, Any] | None, field: str) -> bool:
    return bool(profile is not None and field in profile and profile.get(field) is not None)


def configured_fields(profile: dict[str, Any] | None) -> list[str]:
    if not profile:
        return []
    return [field for field in PROFILE_FIELDS if field in profile and profile.get(field) is not None]


def missing_or_fallback_fields(profile: dict[str, Any] | None) -> list[str]:
    if not profile:
        return list(PROFILE_FIELDS)
    return [field for field in PROFILE_FIELDS if field not in profile or profile.get(field) is None]


def numeric(profile: dict[str, Any] | None, field: str) -> float | None:
    if not field_is_configured(profile, field):
        return None
    try:
        value = float(profile[field])
    except (TypeError, ValueError):
        return None
    return value


def integer(profile: dict[str, Any] | None, field: str) -> int | None:
    value = numeric(profile, field)
    if value is None:
        return None
    return max(0, int(value))


def symbol_set(profile: dict[str, Any] | None, field: str) -> set[str]:
    if not field_is_configured(profile, field):
        return set()
    raw = profile.get(field)
    if isinstance(raw, str):
        values = raw.replace(";", ",").replace("|", ",").split(",")
    elif isinstance(raw, (list, tuple, set)):
        values = list(raw)
    else:
        values = []
    return {str(item or "").strip().upper() for item in values if str(item or "").strip()}
