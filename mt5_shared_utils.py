#!/usr/bin/env python3
"""Shared MT5 utilities.

This module contains no signal generation, broker connection, or order path.
It replaces utility imports that historically pulled the retired IBKR bot into
the live MT5 process.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


TRADING_TZ = ZoneInfo("Africa/Johannesburg")

CFG = {
    "capital_cap_usd": 5000.0,
    "risk_pct": 0.35,
    "usd_to_zar": 18.0,
    "gbp_usd": 1.27,
    "rr_ratio": 1.5,
    "rr_ratio_forex": 1.6,
    "rr_ratio_metals": 1.8,
    "rr_ratio_energies": 1.8,
    "rr_ratio_us": 2.0,
    "rr_ratio_uk": 2.0,
    "crypto_rr": 2.0,
    "atr_stop_mult": 2.0,
    "index_atr_stop_mult": 1.5,
    "stop_floor_pct": 0.002,
    "stop_floor_pct_us": 0.003,
    "stop_floor_pct_uk": 0.003,
    "forex_stop_floor_pct": 0.0005,
    "metal_stop_floor_pct": 0.0015,
    "energy_stop_floor_pct": 0.003,
    "crypto_stop_floor_pct": 0.005,
    "trade_windows_uk_sast": ((10, 0, 19, 30),),
    "trade_windows_us_sast": ((15, 20, 23, 0),),
    "allow_forex_crypto_anytime": True,
    "mean_rev_min_score_forex": 70.0,
    "bbs_min_score_forex": 70.0,
}

ACTIVE_PROBABILITY_STRATEGIES: set[str] = set()


def _minutes(dt: datetime) -> int:
    return dt.hour * 60 + dt.minute


def _within(now_min: int, start: int, end: int) -> bool:
    if start <= end:
        return start <= now_min <= end
    return now_min >= start or now_min <= end


def _window_active(now: datetime, windows) -> bool:
    minute = _minutes(now)
    return any(_within(minute, sh * 60 + sm, eh * 60 + em) for sh, sm, eh, em in windows)


def _global_trading_enabled(now_dt: datetime | None = None) -> bool:
    now = now_dt or datetime.now(TRADING_TZ)
    weekday, minute = now.weekday(), _minutes(now)
    if weekday == 5 and minute >= 121:
        return False
    if weekday == 6 and minute < 1380:
        return False
    return True


def in_trade_window(market: str, now_dt: datetime | None = None) -> bool:
    now = now_dt or datetime.now(TRADING_TZ)
    if market == "crypto" and CFG["allow_forex_crypto_anytime"]:
        return True
    if not _global_trading_enabled(now):
        return False
    weekday, minute = now.weekday(), _minutes(now)
    if market in {"forex", "metal", "energy"}:
        if weekday in {0, 1, 2, 3, 4}:
            return True
        return (weekday == 5 and minute <= 120) or (weekday == 6 and minute >= 1380)
    if weekday not in {0, 1, 2, 3, 4}:
        return False
    if market in {"stock_uk", "index_cfd_eu"}:
        return _window_active(now, CFG["trade_windows_uk_sast"])
    if market in {"stock_us", "future", "index_cfd"}:
        return _window_active(now, CFG["trade_windows_us_sast"])
    return False


def _market_floor_pct(market: str, is_crypto: bool = False) -> float:
    if is_crypto:
        return CFG["crypto_stop_floor_pct"]
    return {
        "forex": CFG["forex_stop_floor_pct"],
        "metal": CFG["metal_stop_floor_pct"],
        "energy": CFG["energy_stop_floor_pct"],
        "stock_uk": CFG["stop_floor_pct_uk"],
        "stock_us": CFG["stop_floor_pct_us"],
        "index_cfd": CFG["stop_floor_pct"],
        "index_cfd_eu": CFG["stop_floor_pct"],
    }.get(market, CFG["stop_floor_pct"])


def _compute_stop_distance(
    price: float,
    atr: float,
    market: str = "stock_us",
    vwap_stop_dist: float | None = None,
    is_crypto: bool = False,
) -> float:
    floor = max(float(price), 0.0) * _market_floor_pct(market, is_crypto)
    if vwap_stop_dist is not None and float(vwap_stop_dist) > 0:
        return max(float(vwap_stop_dist), floor)
    mult = CFG["index_atr_stop_mult"] if market in {"index_cfd", "index_cfd_eu"} else CFG["atr_stop_mult"]
    return max(max(float(atr), 0.0) * mult, floor)


def _rr_for_market(
    market: str,
    sym: str | None = None,
    sig_mode: str | None = None,
    is_crypto: bool = False,
) -> float:
    if is_crypto:
        return float(CFG["crypto_rr"])
    return float({
        "forex": CFG["rr_ratio_forex"],
        "metal": CFG["rr_ratio_metals"],
        "energy": CFG["rr_ratio_energies"],
        "stock_uk": CFG["rr_ratio_uk"],
        "stock_us": CFG["rr_ratio_us"],
        "index_cfd": 1.8,
        "index_cfd_eu": 1.8,
    }.get(market, CFG["rr_ratio"]))


def _gross_pnl_usd(entry: float, exit_px: float, qty: float, direction: str, market: str) -> float:
    gross = (float(exit_px) - float(entry)) * float(qty)
    if str(direction).upper() == "SELL":
        gross = -gross
    if market == "stock_uk":
        gross = gross / 100.0 * float(CFG["gbp_usd"])
    return gross


def execution_ok(price: float, atr: float, market: str = "stock_us"):
    """Geometry validation only; low volatility is setup quality, not a final veto."""
    if float(price or 0.0) <= 0:
        return False, "bad price"
    if float(atr or 0.0) <= 0:
        return False, "bad ATR"
    return True, "geometry valid"


def _score_details_from_signal(sig: dict, market: str, htf_trend: str = "FLAT", now_dt=None) -> dict:
    score = float(sig.get("score") or 0.0)
    explicit = sig.get("score_breakdown") if isinstance(sig.get("score_breakdown"), dict) else {}
    h4 = sig.get("h4_score")
    h1 = sig.get("h1_score", sig.get("bias_1h_score"))
    m15 = sig.get("m15_score", sig.get("setup_15m_score"))
    m5 = sig.get("trigger_5m_score")
    m1 = sig.get("execution_1m_score")
    breakdown = {
        "h4": float(h4) if h4 is not None else None,
        "bias_1h": float(h1) if h1 is not None else None,
        "setup_15m": float(m15) if m15 is not None else None,
        "trigger_5m": float(m5) if m5 is not None else score,
        "execution_1m": float(m1) if m1 is not None else 0.0,
        "components": explicit,
        "strategy": str(sig.get("strategy") or sig.get("mode") or ""),
        "score_stage": sig.get("score_stage"),
        "scan_trigger": "COMPLETED_M5",
        "scan_cycle_at": sig.get("generated_at"),
        "bar_times": {
            "H4_open": sig.get("h4_time"), "H4_close": sig.get("h4_closed_at"),
            "H1_open": sig.get("h1_time"), "H1_close": sig.get("h1_closed_at"),
            "M15_open": sig.get("m15_time"), "M15_close": sig.get("m15_closed_at"),
            "M5_open": sig.get("m5_time"), "M5_close": sig.get("m5_candle_closed_at"),
            "M1_close": sig.get("baseline_m1_closed_at"),
        },
    }
    return {
        "score_band": "STRONG" if score >= 80 else "TRADEABLE" if score >= 70 else "WATCH" if score >= 60 else "WEAK",
        "score_breakdown": breakdown,
        "h4_score": breakdown["h4"],
        "bias_1h_score": breakdown["bias_1h"],
        "setup_15m_score": breakdown["setup_15m"],
        "trigger_5m_score": breakdown["trigger_5m"],
        "execution_1m_score": breakdown["execution_1m"],
        "timeframe_setup": "H4+H1+M15+M5 -> M1",
        "comparison_bucket": "replacement_engine",
        "forward_phase": "live",
        "session_name": _session_name(market, now_dt),
    }


def _session_name(market: str, now_dt: datetime | None = None) -> str:
    now = now_dt or datetime.now(TRADING_TZ)
    minute = _minutes(now)
    if market in {"stock_us", "index_cfd"} and 920 <= minute <= 1380:
        return "New York"
    if market in {"stock_uk", "index_cfd_eu"} and 600 <= minute <= 1170:
        return "London"
    if 480 <= minute < 1020:
        return "London"
    if 1380 <= minute or minute < 480:
        return "Asia"
    return "overlap"


def get_usdzar() -> float:
    try:
        return float(os.getenv("USDZAR_RATE", str(CFG["usd_to_zar"])))
    except Exception:
        return float(CFG["usd_to_zar"])


def utc_broker_day_bounds(offset_seconds: int, now_utc: datetime | None = None) -> tuple[datetime, datetime]:
    now = now_utc or datetime.utcnow()
    broker_now = now + timedelta(seconds=int(offset_seconds))
    broker_start = broker_now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = broker_start - timedelta(seconds=int(offset_seconds))
    return start_utc, start_utc + timedelta(days=1)
