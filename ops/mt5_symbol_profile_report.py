#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

APP_DIR = Path("/opt/cipherfx_mt5")
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import mt5_symbol_profiles as profiles

ENV_FILE = Path("/etc/scalpbot/scalpbot-mt5.env")
MARKET_SUFFIX = {
    "metal": "METALS",
    "index_cfd": "INDICES",
    "index_cfd_eu": "INDICES",
    "stock_us": "STOCKS",
    "crypto": "CRYPTO",
    "energy": "ENERGIES",
    "forex": "FOREX",
}


def load_env_file(path: Path = ENV_FILE) -> None:
    try:
        lines = path.read_text(errors="ignore").splitlines()
    except FileNotFoundError:
        return
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, value = text.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def env_float(key: str, default: float) -> float:
    raw = os.getenv(key)
    if raw is None or str(raw).strip() == "":
        return float(default)
    try:
        return float(str(raw).strip())
    except ValueError:
        return float(default)


def env_int(key: str, default: int = 0) -> int:
    try:
        return max(0, int(float(os.getenv(key, str(default)))))
    except ValueError:
        return max(0, int(default))


def env_set(key: str) -> set[str]:
    raw = os.getenv(key, "") or ""
    return {item.strip().upper() for item in raw.replace(";", ",").replace("|", ",").replace(" ", ",").split(",") if item.strip()}


def is_metal(symbol: str) -> bool:
    return symbol.upper().startswith(("XAU", "XAG", "XPT", "XPD"))


def env_float_for_symbol(prefix: str, symbol: str, market: str, default: float) -> float:
    clean = symbol.upper().replace(".", "_")
    if os.getenv(f"{prefix}_{clean}") is not None:
        return env_float(f"{prefix}_{clean}", default)
    if is_metal(symbol) and os.getenv(f"{prefix}_METALS") is not None:
        return env_float(f"{prefix}_METALS", default)
    suffix = MARKET_SUFFIX.get(market)
    if suffix and os.getenv(f"{prefix}_{suffix}") is not None:
        return env_float(f"{prefix}_{suffix}", default)
    if os.getenv(prefix) is not None:
        return env_float(prefix, default)
    return float(default)


def loss_limit(prefix: str, symbol: str, market: str, default: float) -> float:
    clean = symbol.upper().replace(".", "_")
    key = f"{prefix}_{clean}_USD"
    if os.getenv(key) is not None:
        return env_float(key, default)
    if is_metal(symbol):
        return env_float(f"{prefix}_METALS_USD", default)
    suffix = MARKET_SUFFIX.get(market)
    if suffix:
        return env_float(f"{prefix}_{suffix}_USD", env_float(f"{prefix}_USD", default))
    return env_float(f"{prefix}_USD", default)


def scalp_r(prefix: str, symbol: str, market: str, default: float) -> float:
    clean = symbol.upper().replace(".", "_")
    if os.getenv(f"{prefix}_{clean}") is not None:
        return env_float(f"{prefix}_{clean}", default)
    if is_metal(symbol):
        return env_float(f"{prefix}_METALS", default)
    suffix = MARKET_SUFFIX.get(market)
    if suffix and os.getenv(f"{prefix}_{suffix}") is not None:
        return env_float(f"{prefix}_{suffix}", default)
    return env_float(prefix, default)


def min_score(symbol: str, market: str) -> float:
    score = env_float_for_symbol("MT5_MIN_SCORE", symbol, market, env_float("MT5_MIN_SCORE", 82.0))
    if market in {"index_cfd", "index_cfd_eu"} and os.getenv("MT5_INDEX_MIN_SCORE") is not None:
        score = max(score, env_float("MT5_INDEX_MIN_SCORE", score))
    if market in {"metal", "energy", "crypto"}:
        score = max(score, env_float_for_symbol("MT5_ALT_MARKET_MIN_SCORE", symbol, market, score))
    priority = env_float_for_symbol("MT5_PRIORITY_MIN_SCORE", symbol, market, env_float("MT5_PRIORITY_MIN_SCORE", score))
    floor = env_float_for_symbol("MT5_MIN_SCORE", symbol, market, score)
    return max(score, priority, floor)


def spread_atr(symbol: str, market: str) -> float:
    value = env_float_for_symbol("MT5_MAX_SPREAD_ATR_FRAC", symbol, market, env_float("MT5_MAX_SPREAD_ATR_FRAC", 0.24))
    if market in {"index_cfd", "index_cfd_eu"} and os.getenv("MT5_INDEX_MAX_SPREAD_ATR_FRAC") is not None:
        value = env_float("MT5_INDEX_MAX_SPREAD_ATR_FRAC", value)
    if market in {"index_cfd", "index_cfd_eu"}:
        value = env_float("MT5_PRIORITY_INDEX_MAX_SPREAD_ATR_FRAC", value)
    elif market == "metal":
        value = env_float("MT5_PRIORITY_METAL_MAX_SPREAD_ATR_FRAC", value)
    elif market == "forex":
        value = env_float("MT5_PRIORITY_FOREX_MAX_SPREAD_ATR_FRAC", value)
    else:
        value = env_float("MT5_PRIORITY_MAX_SPREAD_ATR_FRAC", value)
    return value


def spread_pct(symbol: str, market: str) -> float:
    return env_float_for_symbol("MT5_MAX_SPREAD_PCT", symbol, market, env_float("MT5_MAX_SPREAD_PCT", 0.0008))


def baseline(symbol: str, market: str) -> dict[str, Any]:
    return {
        "min_score": min_score(symbol, market),
        "risk_pct": env_float_for_symbol("MT5_RISK_PCT", symbol, market, 0.35),
        "max_lot": (env_float(f"MT5_MAX_LOT_{symbol}", 0.0) if os.getenv(f"MT5_MAX_LOT_{symbol}") is not None else env_float_for_symbol("MT5_MAX_LOT", symbol, market, env_float("MT5_MAX_LOT_DEFAULT", 0.10))),
        "max_position_pct": env_float_for_symbol("MT5_MAX_POSITION_PCT", symbol, market, env_float("MT5_MAX_POSITION_PCT", 1.0)),
        "max_trade_risk_usd": loss_limit("MT5_MAX_TRADE_RISK", symbol, market, 50.0),
        "max_open_position_loss_usd": loss_limit("MT5_MAX_OPEN_POSITION_LOSS", symbol, market, 100.0),
        "max_symbol_floating_loss_usd": loss_limit("MT5_MAX_SYMBOL_FLOATING_LOSS", symbol, market, 150.0),
        "max_stop_distance": env_float_for_symbol("MT5_MAX_STOP_DISTANCE", symbol, market, 0.0),
        "max_spread_atr_frac": spread_atr(symbol, market),
        "max_spread_pct": spread_pct(symbol, market),
        "scalp_tp_r": scalp_r("MT5_SCALP_TP_R", symbol, market, 1.0),
        "scalp_close_r": scalp_r("MT5_SCALP_CLOSE_R", symbol, market, 0.85),
        "breakeven_trigger_r": env_float("MT5_BREAKEVEN_TRIGGER_R", 0.50),
        "trail_start_r": env_float("MT5_TRAIL_START_R", 0.80),
        "max_daily_trades": env_int(f"MT5_MAX_DAILY_TRADES_PER_SYMBOL_{symbol}", env_int("MT5_PRIORITY_MAX_DAILY_TRADES_PER_SYMBOL", env_int("MT5_MAX_DAILY_TRADES_PER_SYMBOL", 0))),
        "max_daily_losses": env_int("MT5_PRIORITY_MAX_SYMBOL_LOSSES_PER_DAY", env_int("MT5_MAX_SYMBOL_LOSSES_PER_DAY", 1)),
        "cooldown_minutes": env_int("MT5_PRIORITY_SYMBOL_COOLDOWN_MIN", env_int("MT5_SYMBOL_COOLDOWN_MIN", 60)),
        "max_duration_minutes": env_int("MT5_MAX_HOLD_MINUTES", 30),
        "tighten_after_minutes": 0,
        "tighten_if_profit_below_usd": 0.0,
        "blocked_strategies": env_set("MT5_BLOCK_STRATEGIES") | env_set(f"MT5_BLOCK_STRATEGIES_{symbol}"),
        "allowed_strategies": env_set(f"MT5_ALLOW_STRATEGIES_{symbol}"),
    }

CAP_FIELDS = ["risk_pct", "max_lot", "max_position_pct", "max_trade_risk_usd", "max_open_position_loss_usd", "max_symbol_floating_loss_usd", "max_spread_atr_frac", "max_spread_pct", "scalp_tp_r", "scalp_close_r", "breakeven_trigger_r", "trail_start_r", "tighten_if_profit_below_usd"]
FLOOR_FIELDS = ["min_score", "cooldown_minutes"]
INT_CAP_FIELDS = ["max_daily_trades", "max_daily_losses", "max_duration_minutes", "tighten_after_minutes"]


def risk_increases(symbol: str, profile: dict[str, Any], base: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for field in CAP_FIELDS:
        value = profiles.numeric(profile, field)
        if value is None:
            continue
        base_value = float(base.get(field) or 0.0)
        if base_value > 0 and value > base_value + 1e-9:
            problems.append(f"{field} {value} > env {base_value}")
    stop_value = profiles.numeric(profile, "max_stop_distance")
    stop_base = float(base.get("max_stop_distance") or 0.0)
    if stop_value is not None and stop_base > 0 and stop_value > stop_base + 1e-9:
        problems.append(f"max_stop_distance {stop_value} > env {stop_base}")
    for field in FLOOR_FIELDS:
        value = profiles.numeric(profile, field)
        if value is None:
            continue
        base_value = float(base.get(field) or 0.0)
        if value + 1e-9 < base_value:
            problems.append(f"{field} {value} < env {base_value}")
    for field in INT_CAP_FIELDS:
        value = profiles.integer(profile, field)
        if value is None:
            continue
        base_value = int(base.get(field) or 0)
        if base_value > 0 and (value == 0 or value > base_value):
            problems.append(f"{field} {value} weakens env {base_value}")
    env_blocked = set(base.get("blocked_strategies") or set())
    profile_blocked = profiles.symbol_set(profile, "blocked_strategies")
    if not env_blocked.issubset(profile_blocked):
        problems.append(f"blocked_strategies missing env blocks {sorted(env_blocked - profile_blocked)}")
    env_allowed = set(base.get("allowed_strategies") or set())
    profile_allowed = profiles.symbol_set(profile, "allowed_strategies")
    if env_allowed and (not profile_allowed or not profile_allowed.issubset(env_allowed)):
        problems.append("allowed_strategies expands env allowlist")
    return problems


def main() -> int:
    load_env_file()
    rows = profiles.load_profiles()
    missing = [sym for sym in profiles.PRIORITY_SYMBOLS if sym not in rows]
    problems: dict[str, list[str]] = {}
    print("MT5 Symbol Profile Report")
    print(f"profile_path={profiles.profile_path()}")
    print(f"profile_count={len(rows)}")
    print("symbol enabled market allowed blocked min_score max_lot risk_pct max_trade_risk max_stop spread_atr spread_pct news_exposures daily_trades daily_losses cooldown max_duration tighten_after fallback_fields")
    for symbol in profiles.PRIORITY_SYMBOLS:
        row = rows.get(symbol) or {}
        market = str(row.get("market") or "")
        base = baseline(symbol, market or "forex")
        p = risk_increases(symbol, row, base) if row else ["missing profile"]
        if p:
            problems[symbol] = p
        fallback = profiles.missing_or_fallback_fields(row)
        print(
            f"{symbol} {row.get('enabled')} {market} "
            f"{','.join(sorted(profiles.symbol_set(row, 'allowed_strategies'))) or '-'} "
            f"{','.join(sorted(profiles.symbol_set(row, 'blocked_strategies'))) or '-'} "
            f"{row.get('min_score')} {row.get('max_lot')} {row.get('risk_pct')} "
            f"{row.get('max_trade_risk_usd')} {row.get('max_stop_distance')} "
            f"{row.get('max_spread_atr_frac')} {row.get('max_spread_pct')} "
            f"{','.join(row.get('news_exposures') or [])} "
            f"{row.get('max_daily_trades')} {row.get('max_daily_losses')} {row.get('cooldown_minutes')} "
            f"{row.get('max_duration_minutes')} {row.get('tighten_after_minutes')} "
            f"{','.join(fallback) or '-'}"
        )
    print(f"all_priority_symbols_profiled={not missing}")
    print(f"missing_profiles={','.join(missing) if missing else '-'}")
    print(f"risk_increase_found={'yes' if problems else 'no'}")
    if problems:
        for symbol, items in problems.items():
            print(f"RISK_INCREASE {symbol}: {'; '.join(items)}")
        return 2
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
