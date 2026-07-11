#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter
import json
import math
import os
import re
import signal
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from dashboard.backend import state_store as _ss
from mt5_xm_config import MT5RuntimeConfig
from mt5_xm_gateway import MT5Gateway, MT5PositionView, MT5SymbolSpec
from mt5_shared_utils import (
    ACTIVE_PROBABILITY_STRATEGIES,
    CFG,
    _compute_stop_distance,
    _global_trading_enabled,
    _gross_pnl_usd,
    _rr_for_market,
    _score_details_from_signal,
    execution_ok,
    get_usdzar,
    in_trade_window,
)

try:
    import news_filter as _news_filter
except Exception:
    _news_filter = None

try:
    import mt5_safety_guards as _mt5_safety_guards
except Exception:
    _mt5_safety_guards = None

try:
    import mt5_news_gate as _mt5_news_gate
except Exception:
    _mt5_news_gate = None

try:
    import mt5_symbol_profiles as _mt5_symbol_profiles
except Exception:
    _mt5_symbol_profiles = None

try:
    import mt5_strategy_engines as _mt5_strategy_engines
except Exception:
    _mt5_strategy_engines = None

try:
    import strategy_architecture_v1 as _strategy_v1
except Exception:
    _strategy_v1 = None

try:
    import mt5_systematic_engine as _mt5_systematic_engine
except Exception:
    _mt5_systematic_engine = None

GROUP_MARKET = {
    "forex": "forex",
    "metals": "metal",
    "energies": "energy",
    "crypto": "crypto",
    "indices": "index_cfd",
    "stocks": "stock_us",
    "etfs": "stock_us",
}
EU_INDEX_SYMBOLS = {"GER40", "DE40", "DAX40", "FRA40", "EU50", "UK100", "AUS200"}

TRADE_RETCODE_DONE = {10008, 10009, 10010}
SETUP_TIMEFRAME_MINUTES = 15


# --- MULTI ENGINE CLEAN REBUILD V1 ---
FOREX_ENGINE = "FOREX_ENGINE"
INDEX_ENGINE = "INDEX_ENGINE"
METALS_ENGINE = "METALS_ENGINE"

TEST_MODE_GATING = True
ruleset_version = "multi_engine_clean_rebuild_v1"
RULESET_VERSION = ruleset_version
BUILD_ID = f"{RULESET_VERSION}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"

FULL_SCAN_SECONDS = 5
PENDING_MONITOR_SECONDS = 0.25
PENDING_SETUP_TTL_SECONDS = 300
HOUSEKEEPING_SECONDS = 900

MAX_TRADES_PER_DAY = 60
DAILY_LOSS_LIMIT_USD = 1000
MAX_OPEN_TRADES_TOTAL = 30
MAX_OPEN_TRADES_PER_SYMBOL = 10

TIER_A_SYMBOLS = {"XAUUSD", "NAS100", "US100", "US30", "GER40"}
TIER_B_SYMBOLS = {"XAGUSD", "SPX500", "UK100", "FRA40", "EURJPY", "USDJPY", "GBPUSD"}
TIER_C_SYMBOLS = {"EURUSD", "AUDUSD", "NZDUSD", "USDCAD"}
MAX_LOSSES_PER_SYMBOL_PER_DAY = 2
MAX_TOTAL_CONSECUTIVE_LOSSES = 5

BASE_RISK_PER_TRADE_USD = 20
MAX_RISK_PER_TRADE_USD = 50
REDUCED_RISK_AFTER_LOSS_USD = 10

ALLOW_DIRECT_5M_ENTRY = False
REQUIRE_1M_CONFIRMATION = True

RAW_SCAN = "RAW_SCAN"
NO_SETUP = "NO_SETUP"
CANDIDATE = "CANDIDATE"
BLOCKED = "BLOCKED"
PENDING = "PENDING"
PENDING_SOFT = "PENDING_SOFT"
CONFIRMED = "CONFIRMED"
EXECUTED = "EXECUTED"
CANCELLED = "CANCELLED"

SYMBOL_CONFIG = {
    "EURUSD": {"asset_class": "forex", "engine": FOREX_ENGINE, "strategy": "mean_reversion_pullback", "min_score": 85, "mr_adx_soft": 28, "mr_adx_hard": 34, "trend_adx_max": 42, "max_spread_pips_test": 2.0, "max_spread_atr_ratio": 0.50, "atr_stop_mult": 1.2, "tp_r": 1.5, "timeout_minutes": 5},
    "GBPUSD": {"asset_class": "forex", "engine": FOREX_ENGINE, "strategy": "liquidity_sweep_continuation", "min_score": 86, "mr_adx_soft": 28, "mr_adx_hard": 35, "trend_adx_max": 44, "max_spread_pips_test": 2.5, "max_spread_atr_ratio": 0.55, "atr_stop_mult": 1.4, "tp_r": 1.6, "timeout_minutes": 5},
    "USDJPY": {"asset_class": "forex", "engine": FOREX_ENGINE, "strategy": "trend_continuation", "min_score": 84, "mr_adx_soft": 26, "mr_adx_hard": 33, "trend_adx_max": 45, "max_spread_pips_test": 2.0, "max_spread_atr_ratio": 0.50, "atr_stop_mult": 1.3, "tp_r": 1.5, "timeout_minutes": 5},
    "AUDUSD": {"asset_class": "forex", "engine": FOREX_ENGINE, "strategy": "mean_reversion_pullback", "min_score": 84, "mr_adx_soft": 28, "mr_adx_hard": 34, "trend_adx_max": 40, "max_spread_pips_test": 2.6, "max_spread_atr_ratio": 0.50, "atr_stop_mult": 1.2, "tp_r": 1.4, "timeout_minutes": 5},
    "NZDUSD": {"asset_class": "forex", "engine": FOREX_ENGINE, "strategy": "mean_reversion_pullback", "min_score": 84, "mr_adx_soft": 30, "mr_adx_hard": 36, "trend_adx_max": 40, "max_spread_pips_test": 2.8, "max_spread_atr_ratio": 0.55, "atr_stop_mult": 1.2, "tp_r": 1.4, "timeout_minutes": 5},
    "USDCAD": {"asset_class": "forex", "engine": FOREX_ENGINE, "strategy": "mean_reversion", "min_score": 85, "mr_adx_soft": 28, "mr_adx_hard": 34, "trend_adx_max": 40, "max_spread_pips_test": 2.7, "max_spread_atr_ratio": 0.55, "atr_stop_mult": 1.3, "tp_r": 1.5, "timeout_minutes": 5},
    "EURJPY": {"asset_class": "forex", "engine": FOREX_ENGINE, "strategy": "trend_continuation", "min_score": 86, "mr_adx_soft": 27, "mr_adx_hard": 35, "trend_adx_max": 45, "max_spread_pips_test": 3.0, "max_spread_atr_ratio": 0.55, "atr_stop_mult": 1.4, "tp_r": 1.6, "timeout_minutes": 5},
    "NAS100": {"asset_class": "index", "engine": INDEX_ENGINE, "strategy": "momentum_breakout_pullback", "min_score": 88, "pending_floor": 68, "adx_ideal_min": 22, "adx_ideal_max": 42, "adx_danger": 50, "max_spread_points": 3.0, "catastrophic_spread_points": 8.0, "max_spread_atr_ratio": 0.18, "soft_allow_buffer": 20, "atr_stop_mult": 1.5, "tp_r": 1.8, "timeout_minutes": 3},
    "US30": {"asset_class": "index", "engine": INDEX_ENGINE, "strategy": "trend_pullback", "min_score": 87, "pending_floor": 67, "adx_ideal_min": 20, "adx_ideal_max": 40, "adx_danger": 48, "max_spread_points": 5.0, "catastrophic_spread_points": 12.0, "max_spread_atr_ratio": 0.18, "soft_allow_buffer": 20, "atr_stop_mult": 1.6, "tp_r": 1.7, "timeout_minutes": 3},
    "SPX500": {"asset_class": "index", "engine": INDEX_ENGINE, "strategy": "trend_continuation", "min_score": 86, "pending_floor": 66, "adx_ideal_min": 20, "adx_ideal_max": 38, "adx_danger": 45, "max_spread_points": 2.0, "catastrophic_spread_points": 5.0, "max_spread_atr_ratio": 0.15, "soft_allow_buffer": 20, "atr_stop_mult": 1.4, "tp_r": 1.6, "timeout_minutes": 3},
    "GER40": {"asset_class": "index", "engine": INDEX_ENGINE, "strategy": "breakout_pullback", "min_score": 88, "pending_floor": 68, "adx_ideal_min": 22, "adx_ideal_max": 42, "adx_danger": 50, "max_spread_points": 3.5, "catastrophic_spread_points": 10.0, "max_spread_atr_ratio": 0.18, "soft_allow_buffer": 20, "atr_stop_mult": 1.6, "tp_r": 1.8, "timeout_minutes": 3},
    "UK100": {"asset_class": "index", "engine": INDEX_ENGINE, "strategy": "mean_reversion_trend", "min_score": 84, "adx_ideal_min": 18, "adx_ideal_max": 35, "adx_danger": 42, "max_spread_points": 2.5, "max_spread_atr_ratio": 0.16, "soft_allow_buffer": 3, "atr_stop_mult": 1.3, "tp_r": 1.5, "timeout_minutes": 3},
    "FRA40": {"asset_class": "index", "engine": INDEX_ENGINE, "strategy": "trend_pullback", "min_score": 85, "adx_ideal_min": 20, "adx_ideal_max": 38, "adx_danger": 45, "max_spread_points": 2.5, "max_spread_atr_ratio": 0.16, "soft_allow_buffer": 3, "atr_stop_mult": 1.4, "tp_r": 1.6, "timeout_minutes": 3},
    "EU50": {"asset_class": "index", "engine": INDEX_ENGINE, "strategy": "trend_continuation", "min_score": 85, "adx_ideal_min": 20, "adx_ideal_max": 38, "adx_danger": 45, "max_spread_points": 2.0, "max_spread_atr_ratio": 0.16, "soft_allow_buffer": 3, "atr_stop_mult": 1.4, "tp_r": 1.6, "timeout_minutes": 3},
    "JP225": {"asset_class": "index", "engine": INDEX_ENGINE, "strategy": "trend_continuation", "min_score": 86, "pending_floor": 66, "adx_ideal_min": 20, "adx_ideal_max": 40, "adx_danger": 48, "max_spread_points": 6.0, "max_spread_points_test": 8.5, "catastrophic_spread_points": 15.0, "max_spread_atr_ratio": 0.18, "soft_allow_buffer": 20, "atr_stop_mult": 1.5, "tp_r": 1.7, "timeout_minutes": 3},
    "XAUUSD": {"asset_class": "metal", "engine": METALS_ENGINE, "strategy": "trend_pullback_breakout_retest", "min_score": 87, "adx_soft": 30, "adx_hard": 38, "trend_adx_max": 48, "max_spread_points": 0.60, "spread_atr_soft": 0.25, "spread_atr_hard": 0.45, "spread_atr_extreme": 0.60, "atr_stop_mult": 1.6, "tp_r": 1.8, "timeout_minutes": 4},
    "XAGUSD": {"asset_class": "metal", "engine": METALS_ENGINE, "strategy": "trend_pullback_breakout_retest", "min_score": 86, "adx_soft": 30, "adx_hard": 38, "trend_adx_max": 48, "max_spread_points": 0.09, "spread_atr_soft": 0.35, "spread_atr_hard": 0.60, "spread_atr_extreme": 0.75, "atr_stop_mult": 1.7, "tp_r": 1.9, "timeout_minutes": 4},
}


INDEX_SYMBOL_ALIASES = {"US100": "NAS100"}


def canonical_symbol(symbol):
    text = str(symbol or "").upper().strip()
    text = re.sub(r"[^A-Z0-9.]", "", text)
    base = text.split(".", 1)[0]
    if base in INDEX_SYMBOL_ALIASES:
        return INDEX_SYMBOL_ALIASES[base]
    for configured in sorted(SYMBOL_CONFIG, key=len, reverse=True):
        if text == configured or text.startswith(configured + ".") or text.startswith(configured):
            return configured
    return base


def get_symbol_config(symbol):
    return SYMBOL_CONFIG.get(canonical_symbol(symbol))


def get_engine_for_symbol(symbol):
    cfg = get_symbol_config(symbol) or {}
    if cfg.get("engine"):
        return str(cfg.get("engine"))
    asset = str(cfg.get("asset_class") or "")
    return FOREX_ENGINE if asset == "forex" else INDEX_ENGINE if asset == "index" else METALS_ENGINE if asset == "metal" else ""


def pending_floor_for_symbol(symbol, min_score: float, engine: str = "") -> float:
    cfg = get_symbol_config(symbol) or {}
    configured = cfg.get("pending_floor")
    if configured is not None:
        try:
            return max(0.0, float(configured))
        except Exception:
            pass
    engine_name = str(engine or cfg.get("engine") or "").upper()
    asset_class = str(cfg.get("asset_class") or "").lower()
    buffer = 20.0 if engine_name == INDEX_ENGINE or asset_class == "index" else 15.0
    return max(0.0, float(min_score or 0.0) - buffer)


def _bars(df):
    if df is None or len(df) == 0:
        return pd.DataFrame()
    out = df.copy()
    if len(out) > 3:
        out = out.iloc[:-1].copy()
    return out


def _col(df, name, default=0.0):
    for candidate in (name, name.title(), name.lower(), name.upper()):
        if candidate in df:
            return pd.to_numeric(df[candidate], errors="coerce").astype(float)
    return pd.Series([float(default)] * len(df), index=df.index, dtype="float64")


def _last(series, default=0.0):
    try:
        clean = pd.to_numeric(series, errors="coerce").dropna()
        if len(clean):
            return float(clean.iloc[-1])
    except Exception:
        pass
    return float(default)


def _ema(close, span):
    return close.ewm(span=span, adjust=False).mean()


def _rsi(close, period=14):
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, float("nan"))
    return (100 - (100 / (1 + rs))).fillna(50.0)


def _atr_series(df, period=14):
    high = _col(df, "High")
    low = _col(df, "Low")
    close = _col(df, "Close")
    prev_close = close.shift(1)
    tr = pd.concat([(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean().bfill().fillna(tr.mean() if len(tr) else 0.0)


def _adx_series(df, period=14):
    high = _col(df, "High")
    low = _col(df, "Low")
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
    atr = _atr_series(df, period).replace(0, float("nan"))
    plus_di = 100 * plus_dm.rolling(period).mean() / atr
    minus_di = 100 * minus_dm.rolling(period).mean() / atr
    dx = ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, float("nan"))) * 100
    return dx.rolling(period).mean().fillna(0.0)


def _vwap(df):
    high = _col(df, "High")
    low = _col(df, "Low")
    close = _col(df, "Close")
    volume = _col(df, "Volume")
    if float(volume.abs().sum() or 0.0) <= 0:
        volume = _col(df, "Tick_volume", 1.0)
    typical = (high + low + close) / 3.0
    denom = volume.cumsum().replace(0, float("nan"))
    return ((typical * volume).cumsum() / denom).ffill().fillna(close)


def _pack(df):
    bars = _bars(df)
    if len(bars) < 25:
        bars = df.copy() if df is not None else pd.DataFrame()
    if len(bars) == 0:
        return {"ready": False, "close": 0.0, "atr": 0.0, "adx": 0.0, "rsi": 50.0, "bias": ""}
    open_ = _col(bars, "Open")
    high = _col(bars, "High")
    low = _col(bars, "Low")
    close = _col(bars, "Close")
    ema8 = _ema(close, 8)
    ema20 = _ema(close, 20)
    ema50 = _ema(close, 50) if len(close) >= 50 else _ema(close, max(5, min(20, len(close))))
    atr = _atr_series(bars, 14)
    adx = _adx_series(bars, 14)
    rsi = _rsi(close, 14)
    vwap = _vwap(bars)
    last_close = _last(close)
    atr_last = max(_last(atr), 1e-12)
    ema20_last = _last(ema20)
    ema50_last = _last(ema50)
    bias = "BUY" if ema20_last > ema50_last and last_close >= ema20_last else "SELL" if ema20_last < ema50_last and last_close <= ema20_last else "RANGE"
    recent_high = float(high.tail(12).iloc[:-1].max()) if len(high) > 2 else _last(high)
    recent_low = float(low.tail(12).iloc[:-1].min()) if len(low) > 2 else _last(low)
    return {
        "ready": len(bars) >= 20,
        "open": _last(open_), "high": _last(high), "low": _last(low), "close": last_close,
        "prev_close": _last(close.iloc[:-1], last_close) if len(close) > 1 else last_close,
        "ema8": _last(ema8), "ema20": ema20_last, "ema50": ema50_last,
        "atr": atr_last, "adx": _last(adx), "rsi": _last(rsi, 50.0), "vwap": _last(vwap, last_close),
        "vwap_z": (last_close - _last(vwap, last_close)) / atr_last, "bias": bias,
        "recent_high": recent_high, "recent_low": recent_low,
        "body": abs(last_close - _last(open_)),
        "upper_wick": max(0.0, _last(high) - max(last_close, _last(open_))),
        "lower_wick": max(0.0, min(last_close, _last(open_)) - _last(low)),
        "range": max(_last(high) - _last(low), 1e-12),
    }


def _directional_context_score(side: str, pack: dict, timeframe: str) -> tuple[float | None, dict]:
    # Derive the HTF score from the completed candle context passed to the gate.
    side = str(side or "").upper()
    if side not in {"BUY", "SELL"} or not pack.get("ready"):
        return None, {"timeframe": timeframe, "status": "missing"}
    close = float(pack.get("close") or 0.0)
    ema20 = float(pack.get("ema20") or 0.0)
    ema50 = float(pack.get("ema50") or 0.0)
    vwap = float(pack.get("vwap") or 0.0)
    rsi = float(pack.get("rsi") or 50.0)
    adx = float(pack.get("adx") or 0.0)
    if min(close, ema20, ema50, vwap) <= 0.0:
        return None, {"timeframe": timeframe, "status": "invalid_context_values"}

    buy = side == "BUY"
    components = {
        "bias": 35.0 if pack.get("bias") == side else 0.0,
        "close_vs_ema20": 20.0 if ((close >= ema20) if buy else (close <= ema20)) else 0.0,
        "ema20_vs_ema50": 20.0 if ((ema20 >= ema50) if buy else (ema20 <= ema50)) else 0.0,
        "vwap_location": 15.0 if ((close >= vwap) if buy else (close <= vwap)) else 0.0,
        "rsi_direction": 5.0 if ((rsi >= 50.0) if buy else (rsi <= 50.0)) else 0.0,
        "adx_present": 5.0 if adx > 0.0 else 0.0,
    }
    return round(sum(components.values()), 2), {
        "timeframe": timeframe,
        "status": "calculated",
        "components": components,
        "bias": pack.get("bias"),
        "close": close,
        "ema20": ema20,
        "ema50": ema50,
        "vwap": vwap,
        "rsi": rsi,
        "adx": adx,
    }



def _empty_trigger(reason, strategy=""):
    return {"direction": None, "strategy_before": strategy or "NO_SETUP", "strategy_after": strategy or "", "regime": "NO_SETUP", "trigger_name": "NO_SETUP", "reason": reason, "metrics": {}}


def _trigger_result(direction, strategy, regime, trigger_name, reason, metrics):
    return {"direction": direction if direction in {"BUY", "SELL"} else None, "strategy_before": strategy, "strategy_after": strategy, "regime": regime, "trigger_name": trigger_name, "reason": reason, "metrics": dict(metrics or {})}


def _trend_side(h1p, m15p):
    if h1p.get("bias") in {"BUY", "SELL"} and h1p.get("bias") == m15p.get("bias"):
        return h1p.get("bias")
    if h1p.get("bias") in {"BUY", "SELL"}:
        return h1p.get("bias")
    return m15p.get("bias") if m15p.get("bias") in {"BUY", "SELL"} else None


def _bollinger_position(df):
    bars = _bars(df)
    if len(bars) < 20:
        return None
    close = _col(bars, "Close")
    window = close.tail(20)
    mid = float(window.mean())
    std = float(window.std(ddof=0) or 0.0)
    upper = mid + (2.0 * std)
    lower = mid - (2.0 * std)
    if upper <= lower:
        return None
    return (float(close.iloc[-1]) - lower) / (upper - lower)


def _liquidity_sweep_side(p):
    if p["low"] < p["recent_low"] and p["close"] > p["open"] and p["lower_wick"] >= p["body"]:
        return "BUY"
    if p["high"] > p["recent_high"] and p["close"] < p["open"] and p["upper_wick"] >= p["body"]:
        return "SELL"
    return None


def _breakout_side(p):
    if p["close"] > p["recent_high"] and p["close"] > p["ema20"]:
        return "BUY"
    if p["close"] < p["recent_low"] and p["close"] < p["ema20"]:
        return "SELL"
    return None


def _pullback_side(p, trend):
    if trend == "BUY" and p["close"] >= p["ema20"] and p["low"] <= max(p["ema20"], p["vwap"]):
        return "BUY"
    if trend == "SELL" and p["close"] <= p["ema20"] and p["high"] >= min(p["ema20"], p["vwap"]):
        return "SELL"
    return None


def _mean_reversion_side(p):
    if p["vwap_z"] <= -0.80 and p["rsi"] <= 48:
        return "BUY"
    if p["vwap_z"] >= 0.80 and p["rsi"] >= 52:
        return "SELL"
    return None


def detect_forex_m5_trigger(symbol, m5, m15, h1):
    cfg = get_symbol_config(symbol) or {}
    strategy = str(cfg.get("strategy") or "forex_m5")
    p, p15, p1h = _pack(m5), _pack(m15), _pack(h1)
    if not (p.get("ready") and p15.get("ready") and p1h.get("ready")):
        return _empty_trigger("insufficient H1/M15/M5 data", strategy)
    trend = _trend_side(p1h, p15)
    sweep = _liquidity_sweep_side(p)
    pullback = _pullback_side(p, trend)
    breakout = _breakout_side(p)
    mean_rev = _mean_reversion_side(p)
    if "liquidity_sweep" in strategy:
        side = sweep or pullback
        trigger_name = "liquidity_sweep_continuation" if sweep else "trend_pullback"
    elif "trend" in strategy:
        side = pullback or breakout
        trigger_name = "trend_pullback" if pullback else "breakout_retest"
    elif "mean_reversion" in strategy:
        side = mean_rev or pullback
        trigger_name = "mean_reversion_pullback" if mean_rev else "trend_pullback"
    else:
        side = breakout or pullback or mean_rev
        trigger_name = "breakout_retest"
    metrics = {**p, "h1_bias": p1h.get("bias"), "m15_bias": p15.get("bias"), "trend_side": trend}
    if side not in {"BUY", "SELL"}:
        return _empty_trigger("no forex M5 trigger", strategy)
    return _trigger_result(side, strategy, "TREND" if trend in {"BUY", "SELL"} else "RANGE", trigger_name, f"{trigger_name} selected by {FOREX_ENGINE}", metrics)


def detect_index_m5_trigger(symbol, m5, m15, h1):
    cfg = get_symbol_config(symbol) or {}
    strategy = str(cfg.get("strategy") or "index_m5")
    p, p15, p1h = _pack(m5), _pack(m15), _pack(h1)
    if not (p.get("ready") and p15.get("ready") and p1h.get("ready")):
        return _empty_trigger("insufficient H1/M15/M5 data", strategy)
    trend = _trend_side(p1h, p15)
    breakout = _breakout_side(p)
    pullback = _pullback_side(p, trend)
    mean_rev = _mean_reversion_side(p) or _liquidity_sweep_side(p)
    if "momentum_breakout" in strategy or "breakout" in strategy:
        side = breakout or pullback
        trigger_name = "momentum_breakout_pullback" if breakout else "trend_pullback"
    elif "mean_reversion" in strategy or p["adx"] < 18:
        side = mean_rev
        trigger_name = "range_rejection"
    else:
        side = pullback or breakout
        trigger_name = "trend_pullback" if pullback else "breakout_retest"
    metrics = {**p, "h1_bias": p1h.get("bias"), "m15_bias": p15.get("bias"), "trend_side": trend}
    if side not in {"BUY", "SELL"}:
        return _empty_trigger("no index M5 trigger", strategy)
    return _trigger_result(side, strategy, "MOMENTUM" if p["adx"] >= 22 else "RANGE", trigger_name, f"{trigger_name} selected by {INDEX_ENGINE}", metrics)


def detect_metals_m5_trigger(symbol, m5, m15, h1):
    cfg = get_symbol_config(symbol) or {}
    strategy = str(cfg.get("strategy") or "metals_m5")
    p, p15, p1h = _pack(m5), _pack(m15), _pack(h1)
    if not (p.get("ready") and p15.get("ready") and p1h.get("ready")):
        return _empty_trigger("insufficient H1/M15/M5 data", strategy)
    trend = _trend_side(p1h, p15)
    sweep = _liquidity_sweep_side(p)
    breakout = _breakout_side(p)
    pullback = _pullback_side(p, trend)
    side = pullback or breakout or sweep
    trigger_name = "trend_pullback" if pullback else "breakout_retest" if breakout else "liquidity_sweep_reversal" if sweep else ""
    metrics = {**p, "h1_bias": p1h.get("bias"), "m15_bias": p15.get("bias"), "trend_side": trend}
    if side not in {"BUY", "SELL"}:
        return _empty_trigger("no metals M5 trigger", strategy)
    return _trigger_result(side, strategy, "METALS_TREND" if trend in {"BUY", "SELL"} else "METALS_RANGE", trigger_name, f"{trigger_name} selected by {METALS_ENGINE}", metrics)


def _aligned_points(side, bias, points):
    return float(points) if side == bias else float(points) * 0.45 if bias in {"BUY", "SELL"} else float(points) * 0.55


def _trigger_points(trigger_name, points):
    return float(points) if str(trigger_name or "") not in {"", "NO_SETUP"} else 0.0


def _legacy_probability_score(symbol, market, m5):
    # The legacy probability engine is retired from the live MT5 build.
    return 0.0, {}


def calculate_forex_score(symbol, side, trigger, m5, m15, h1):
    cfg = get_symbol_config(symbol) or {}
    p, p15, p1h = _pack(m5), _pack(m15), _pack(h1)
    strategy = str(cfg.get("strategy") or (trigger or {}).get("strategy_after") or "")
    breakdown = {
        "1H bias aligned": _aligned_points(side, p1h.get("bias"), 15),
        "15M structure/VWAP/liquidity": _aligned_points(side, p15.get("bias"), 10) + (5 if abs(p15.get("vwap_z", 0.0)) <= 1.8 else 2),
        "5M trigger quality": _trigger_points((trigger or {}).get("trigger_name"), 20),
        "VWAP/EMA location": 10 if (side == "BUY" and p["close"] >= min(p["ema20"], p["vwap"])) or (side == "SELL" and p["close"] <= max(p["ema20"], p["vwap"])) else 5,
        "RSI acceptable": 5 if 25 <= p["rsi"] <= 75 else 2,
    }
    legacy_score, _legacy_sig = _legacy_probability_score(symbol, "forex", m5)
    if legacy_score > 0:
        breakdown["legacy_probability_context"] = min(5.0, legacy_score / 20.0)
    score_before = min(100.0, sum(float(v) for v in breakdown.values()))
    penalties = {}
    score_after = score_before
    if "mean_reversion" in strategy and abs(p.get("vwap_z", 0.0)) < 1.20:
        penalties["mean_reversion_zscore_soft"] = round(min(4.0, (1.20 - abs(p.get("vwap_z", 0.0))) * 3.0), 3)
        score_after -= penalties["mean_reversion_zscore_soft"]
    removed_credits = 29.0
    return {"score_before": round(score_before, 3), "score_after": round(max(0.0, score_after), 3), "breakdown": breakdown, "penalties": penalties, "min_score": max(0.0, float(cfg.get("min_score") or 85) - removed_credits), "removed_non_evidence_credits": removed_credits, "score_basis": "measured_components_only"}


def calculate_index_score(symbol, side, trigger, m5, m15, h1):
    cfg = get_symbol_config(symbol) or {}
    p, p15, p1h = _pack(m5), _pack(m15), _pack(h1)
    breakdown = {
        "1H bias aligned": _aligned_points(side, p1h.get("bias"), 20),
        "15M VWAP/structure": _aligned_points(side, p15.get("bias"), 12) + (3 if abs(p15.get("vwap_z", 0.0)) <= 2.2 else 1),
        "5M breakout/pullback": _trigger_points((trigger or {}).get("trigger_name"), 20),
        "ADX/momentum": 10 if cfg.get("adx_ideal_min", 20) <= p["adx"] <= cfg.get("adx_ideal_max", 42) else 5,
        "ATR expansion": 5 if p["atr"] > 0 and p["range"] >= p["atr"] * 0.35 else 3,
    }
    score_before = min(100.0, sum(float(v) for v in breakdown.values()))
    removed_credits = 24.0
    return {"score_before": round(score_before, 3), "score_after": round(score_before, 3), "breakdown": breakdown, "penalties": {}, "min_score": max(0.0, float(cfg.get("min_score") or 86) - removed_credits), "removed_non_evidence_credits": removed_credits, "score_basis": "measured_components_only"}


def calculate_metals_score(symbol, side, trigger, m5, m15, h1):
    cfg = get_symbol_config(symbol) or {}
    p, p15, p1h = _pack(m5), _pack(m15), _pack(h1)
    breakdown = {
        "1H bias aligned": _aligned_points(side, p1h.get("bias"), 18),
        "15M structure/VWAP": _aligned_points(side, p15.get("bias"), 12) + (3 if abs(p15.get("vwap_z", 0.0)) <= 2.5 else 1),
        "5M trigger quality": _trigger_points((trigger or {}).get("trigger_name"), 20),
        "ADX/momentum": 10 if p["adx"] <= cfg.get("trend_adx_max", 48) else 5,
    }
    score_before = min(100.0, sum(float(v) for v in breakdown.values()))
    removed_credits = 31.0
    return {"score_before": round(score_before, 3), "score_after": round(score_before, 3), "breakdown": breakdown, "penalties": {}, "min_score": max(0.0, float(cfg.get("min_score") or 87) - removed_credits), "removed_non_evidence_credits": removed_credits, "score_basis": "measured_components_only"}


def apply_forex_adx_logic(symbol, strategy, adx, score, h1_bias_aligned, m15_structure_aligned):
    cfg = get_symbol_config(symbol) or {}
    adjusted = float(score)
    penalties = {}
    soft = float(cfg.get("mr_adx_soft", 28))
    hard = float(cfg.get("mr_adx_hard", 34))
    trend_max = float(cfg.get("trend_adx_max", 42))
    if "mean_reversion" in str(strategy or "") and adx > soft:
        penalties["mean_reversion_adx_soft"] = round(min(8.0, max(0.0, adx - soft) * 0.8), 3)
        adjusted -= penalties["mean_reversion_adx_soft"]
    if adx > trend_max and not (h1_bias_aligned and m15_structure_aligned):
        penalties["trend_adx_extension"] = round(min(10.0, max(0.0, adx - trend_max) * 0.8), 3)
        adjusted -= penalties["trend_adx_extension"]
    return True, max(0.0, adjusted), "forex ADX penalties applied" if penalties else "OK", {"decision": "soft_penalty" if penalties else "allow", "penalties": penalties, "adx": adx, "mr_adx_soft": soft, "mr_adx_hard": hard, "trend_adx_max": trend_max}


def mean_reversion_zscore_gate(symbol, zscore, zscore_min, score):
    z = abs(float(zscore or 0.0))
    needed = float(zscore_min or 1.2)
    penalty = min(5.0, (needed - z) * 3.0) if z < needed else 0.0
    return True, max(0.0, float(score) - penalty), "mean reversion zscore soft penalty" if penalty else "OK", {"decision": "soft_penalty" if penalty else "allow", "zscore": z, "zscore_min": needed, "penalty": round(penalty, 3)}


def apply_index_regime_logic(symbol, regime, adx, strategy, score, score_before):
    cfg = get_symbol_config(symbol) or {}
    ideal_min = float(cfg.get("adx_ideal_min", 20))
    ideal_max = float(cfg.get("adx_ideal_max", 40))
    danger = float(cfg.get("adx_danger", 48))
    penalty = 0.0
    require_stronger_1m = False
    warnings = []
    adx_value = float(adx or 0.0)
    if adx_value < ideal_min:
        penalty = max(3.0, min(5.0, (ideal_min - adx_value) * 0.6))
        require_stronger_1m = True
        warnings.append("weak ADX")
    elif adx_value > ideal_max:
        penalty = min(5.0, (adx_value - ideal_max) * 0.45)
        warnings.append("extended ADX")
    if adx_value >= danger:
        penalty = min(5.0, max(penalty, 4.0))
        warnings.append("ADX danger")
    if str(regime or "").upper() in {"RANGE", "CHOP", "CHOPPY"}:
        require_stronger_1m = True
        warnings.append("range/chop warning")
    adjusted = max(0.0, float(score) - penalty)
    min_score = float(cfg.get("min_score") or 86)
    pending_floor = pending_floor_for_symbol(symbol, min_score, INDEX_ENGINE)
    buffer = float(cfg.get("soft_allow_buffer") or max(0.0, min_score - pending_floor))
    soft_allow = adjusted >= pending_floor or (float(score_before) >= min_score and adjusted >= min_score - buffer)
    reason = "; ".join(warnings) if warnings else "OK"
    return True, adjusted, reason, {"decision": "scan_warning" if warnings or penalty else "allow", "penalty": round(penalty, 3), "penalty_cap": 5, "soft_allow_used": bool(soft_allow and adjusted < min_score), "require_stronger_1m_confirmation": bool(require_stronger_1m or (soft_allow and adjusted < min_score)), "regime": regime, "adx": adx_value, "pending_floor": pending_floor}


def apply_metals_adx_logic(symbol, strategy, adx, score, h1_bias_aligned, m15_structure_aligned):
    cfg = get_symbol_config(symbol) or {}
    soft = float(cfg.get("adx_soft", 30))
    hard = float(cfg.get("adx_hard", 38))
    trend_max = float(cfg.get("trend_adx_max", 48))
    penalty = 0.0
    if adx > soft and not (h1_bias_aligned and m15_structure_aligned):
        penalty += min(6.0, (adx - soft) * 0.55)
    if adx > hard and "pullback" in str(strategy or ""):
        penalty += min(4.0, (adx - hard) * 0.45)
    if adx > trend_max:
        penalty += min(6.0, (adx - trend_max) * 0.7)
    return True, max(0.0, float(score) - penalty), "metals ADX tiered penalty" if penalty else "OK", {"decision": "soft_penalty" if penalty else "allow", "penalty": round(penalty, 3), "adx": adx, "adx_soft": soft, "adx_hard": hard, "trend_adx_max": trend_max}


def _pip_size(symbol):
    return 0.01 if canonical_symbol(symbol).endswith("JPY") else 0.0001


def forex_spread_gate(symbol, bid, ask, atr_price, score):
    cfg = get_symbol_config(symbol) or {}
    pip = _pip_size(symbol)
    spread = max(0.0, float(ask) - float(bid))
    spread_pips = spread / pip if pip > 0 else 0.0
    atr_pips = float(atr_price or 0.0) / pip if pip > 0 else 0.0
    max_pips = float(cfg.get("max_spread_pips_test", 2.5))
    max_ratio = float(cfg.get("max_spread_atr_ratio", 0.50))
    ratio = spread / float(atr_price) if float(atr_price or 0.0) > 0 else 0.0
    penalty = 0.0
    reasons = []
    if spread_pips > max_pips:
        penalty += min(6.0, (spread_pips - max_pips) * 1.5)
        reasons.append("spread pips above test cap")
    if atr_pips >= 4.0 and ratio > max_ratio:
        penalty += min(6.0, (ratio - max_ratio) * 10.0)
        reasons.append("spread/ATR above cap")
    hard = spread_pips > max_pips * 1.8 and atr_pips >= 4.0
    adjusted = max(0.0, float(score) - penalty)
    return not hard, adjusted, "; ".join(reasons) or "OK", {"decision": "block" if hard else "soft_penalty" if penalty else "allow", "spread": spread, "spread_pips": spread_pips, "atr_pips": atr_pips, "spread_to_atr": ratio, "max_spread_pips": max_pips, "max_spread_atr_ratio": max_ratio, "penalty": round(penalty, 3)}


def index_spread_gate(symbol, bid, ask, atr_price, score):
    cfg = get_symbol_config(symbol) or {}
    canonical = canonical_symbol(symbol)
    spread = max(0.0, float(ask) - float(bid))
    max_points = float(cfg.get("max_spread_points_test") or cfg.get("max_spread_points") or 3.0)
    catastrophic_defaults = {"NAS100": 8.0, "US30": 12.0, "SPX500": 5.0, "GER40": 10.0, "JP225": 15.0}
    catastrophic = float(cfg.get("catastrophic_spread_points") or catastrophic_defaults.get(canonical, max_points * 2.8))
    ratio = spread / float(atr_price) if float(atr_price or 0.0) > 0 else 0.0
    max_ratio = float(cfg.get("max_spread_atr_ratio", 0.18))
    penalty = 0.0
    if spread > max_points:
        penalty += min(4.0, (spread - max_points) * 0.75)
    if ratio > max_ratio:
        penalty += min(4.0, (ratio - max_ratio) * 12.0)
    hard = spread > catastrophic
    adjusted = max(0.0, float(score) - penalty)
    reason = f"catastrophic index spread ({spread:.2f} > {catastrophic:.2f})" if hard else "index spread/ATR scan warning" if penalty else "OK"
    return not hard, adjusted, reason, {"decision": "block" if hard else "soft_penalty" if penalty else "allow", "spread": spread, "spread_points": spread, "spread_to_atr": ratio, "max_spread_points": max_points, "catastrophic_spread_points": catastrophic, "max_spread_atr_ratio": max_ratio, "penalty": round(penalty, 3)}


def metals_spread_gate(symbol, bid, ask, atr_price, score):
    cfg = get_symbol_config(symbol) or {}
    spread = max(0.0, float(ask) - float(bid))
    ratio = spread / float(atr_price) if float(atr_price or 0.0) > 0 else 0.0
    soft = float(cfg.get("spread_atr_soft", 0.25))
    hard_ratio = float(cfg.get("spread_atr_hard", 0.45))
    extreme = float(cfg.get("spread_atr_extreme", 0.60))
    max_points = float(cfg.get("max_spread_points", spread or 0.0))
    penalty = 0.0
    if spread > max_points:
        penalty += min(5.0, (spread - max_points) / max(max_points, 1e-9) * 3.0)
    if ratio > soft:
        penalty += min(8.0, (ratio - soft) * 12.0)
    hard = ratio > extreme or (spread > max_points * 2.0 and ratio > hard_ratio)
    adjusted = max(0.0, float(score) - penalty)
    return not hard, adjusted, "metals spread tier penalty" if penalty else "OK", {"decision": "block" if hard else "soft_penalty" if penalty else "allow", "spread": spread, "spread_points": spread, "spread_to_atr": ratio, "spread_atr_soft": soft, "spread_atr_hard": hard_ratio, "spread_atr_extreme": extreme, "max_spread_points": max_points, "penalty": round(penalty, 3)}


def _completed_candle_identity(frame, label: str, timeframe_seconds: int):
    bars = _bars(frame)
    if bars is None or len(bars) == 0:
        return "", ""
    opened_at = _latest_bar_dt(bars)
    if opened_at is not None:
        closed_at = opened_at + timedelta(seconds=int(timeframe_seconds))
        closed_text = closed_at.isoformat()
        return closed_text, f"{label.upper()}:{closed_text}"
    row = bars.iloc[-1]
    values = []
    for name in ("Open", "High", "Low", "Close"):
        try:
            values.append(f"{float(row.get(name, 0.0) or 0.0):.12g}")
        except Exception:
            values.append("0")
    return "", f"{label.upper()}:OHLC:" + ":".join(values)


def _completed_m1_candle_id(df1):
    return _completed_candle_identity(df1, "M1", 60)


def valid_1m_confirmation(df1, direction, engine):
    bars = _bars(df1)
    p = _pack(df1)
    side = str(direction or "").upper()
    if side not in {"BUY", "SELL"} or not p.get("ready") or len(bars) < 3:
        return False, "waiting for 1M confirmation", 0.0
    close = float(p["close"])
    open_ = float(p["open"])
    high = float(p["high"])
    low = float(p["low"])
    body = abs(close - open_)
    candle_range = max(abs(high - low), 1e-12)
    body_frac = body / candle_range
    close_location = (close - low) / candle_range
    min_body_frac = 0.25
    if side == "BUY":
        side_ok = close > open_ and close >= p["prev_close"]
        location_ok = close_location >= 0.60
        ema_ok = close > p["ema8"]
        momentum_ok = p["rsi"] >= 45
    else:
        side_ok = close < open_ and close <= p["prev_close"]
        location_ok = close_location <= 0.40
        ema_ok = close < p["ema8"]
        momentum_ok = p["rsi"] <= 55
    # EMA alignment is diagnostic context, not a replacement for directional candle confirmation.
    strict_ok = body > 0.0 and body_frac >= min_body_frac and side_ok and location_ok
    score = 70.0 + (10.0 if (close > p["ema20"] if side == "BUY" else close < p["ema20"]) else 0.0)
    score += 10.0 if momentum_ok else 0.0
    score += 10.0 if (close_location >= 0.70 if side == "BUY" else close_location <= 0.30) else 0.0
    score += 10.0 if body_frac >= 0.35 else 0.0
    score = min(100.0, score)
    if strict_ok:
        return True, f"1M confirmation aligned for {engine}; body_frac={body_frac:.2f} close_location={close_location:.2f}", score
    return False, (
        f"waiting for 1M confirmation for {side}: real_candle={close != open_} "
        f"body_frac={body_frac:.2f}/{min_body_frac:.2f} close_location={close_location:.2f} "
        f"ema_aligned={ema_ok}"
    ), score


def tick_execution_check(symbol, engine, bid, ask, signal_price, atr_price, score):
    engine_key = str(engine or "").upper()
    if engine_key == FOREX_ENGINE or engine_key.startswith("FOREX_"):
        ok, adjusted, reason, meta = forex_spread_gate(symbol, bid, ask, atr_price, score)
    elif engine_key == INDEX_ENGINE or engine_key.startswith("INDEX_"):
        ok, adjusted, reason, meta = index_spread_gate(symbol, bid, ask, atr_price, score)
    elif engine_key == METALS_ENGINE or engine_key.startswith(("METAL_", "METALS_")):
        ok, adjusted, reason, meta = metals_spread_gate(symbol, bid, ask, atr_price, score)
    else:
        return False, f"unknown engine at tick execution: {engine or '<missing>'}", {"engine": engine_key}
    mid = (float(bid) + float(ask)) / 2.0 if float(bid) > 0 and float(ask) > 0 else 0.0
    slippage_atr = abs(mid - float(signal_price or mid)) / float(atr_price) if mid > 0 and float(atr_price or 0.0) > 0 else 0.0
    meta.update({"mid": mid, "adjusted_score": adjusted, "slippage_atr": slippage_atr})
    if slippage_atr > 0.35:
        return False, f"slippage too high before execution ({slippage_atr:.2f}x ATR)", meta
    return bool(ok), reason if ok else reason, meta


def _env_float(name: str, default: float, lo: float | None = None, hi: float | None = None) -> float:
    try:
        value = float(os.getenv(name, str(default)) or default)
    except Exception:
        value = float(default)
    if lo is not None:
        value = max(lo, value)
    if hi is not None:
        value = min(hi, value)
    return value


def _env_int(name: str, default: int, lo: int | None = None, hi: int | None = None) -> int:
    try:
        value = int(float(os.getenv(name, str(default)) or default))
    except Exception:
        value = int(default)
    if lo is not None:
        value = max(lo, value)
    if hi is not None:
        value = min(hi, value)
    return value


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _env_symbol_set(name: str, default: str = "") -> set[str]:
    raw = os.getenv(name, default) or ""
    normalized = raw.replace(";", ",").replace("|", ",").replace(" ", ",")
    return {item.strip().upper() for item in normalized.split(",") if item.strip()}


def _parse_dt(value) -> datetime | None:
    if value in (None, ""):
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).replace(tzinfo=None)
    except Exception:
        return None


def _latest_bar_dt(df) -> datetime | None:
    if df is None or len(df) == 0:
        return None
    try:
        row = df.iloc[-1]
    except Exception:
        row = None
    for name in ("time", "Time", "datetime", "Datetime", "date", "Date"):
        try:
            if row is not None and name in df.columns:
                value = row.get(name)
                if isinstance(value, pd.Timestamp):
                    return value.to_pydatetime().replace(tzinfo=None)
                dt = _parse_dt(value)
                if dt is not None:
                    return dt
                number = float(value)
                if number > 0:
                    if number > 10_000_000_000:
                        number = number / 1000.0
                    return datetime.utcfromtimestamp(number)
        except Exception:
            pass
    try:
        idx = df.index[-1]
        if isinstance(idx, pd.Timestamp):
            return idx.to_pydatetime().replace(tzinfo=None)
        dt = _parse_dt(idx)
        if dt is not None:
            return dt
    except Exception:
        pass
    return None


class PortfolioRiskManager:
    def __init__(self, bot):
        self.bot = bot
        self.engine_open_limits = {
            FOREX_ENGINE: _env_int("MT5_ENGINE_MAX_OPEN_FOREX", MAX_OPEN_TRADES_TOTAL, lo=0),
            INDEX_ENGINE: _env_int("MT5_ENGINE_MAX_OPEN_INDEX", MAX_OPEN_TRADES_TOTAL, lo=0),
            METALS_ENGINE: _env_int("MT5_ENGINE_MAX_OPEN_METALS", MAX_OPEN_TRADES_TOTAL, lo=0),
        }

    def _engine_exposure_count(self, engine: str) -> int:
        count = 0
        try:
            positions = self.bot.gateway.positions()
        except Exception:
            positions = []
        for pos in positions:
            canonical = canonical_symbol(self.bot._canonical_from_resolved(pos.symbol)).upper()
            if get_engine_for_symbol(canonical) == engine:
                count += 1
        for setup in self.bot.pending_setups.values():
            if isinstance(setup, dict) and str(setup.get("engine") or "") == engine:
                count += 1
        return count

    def check(self, symbol: str, market: str, sig: dict) -> tuple[bool, str]:
        canonical = canonical_symbol(self.bot._canonical_from_resolved(symbol)).upper()
        engine = str(sig.get("engine") or get_engine_for_symbol(canonical))
        try:
            realized_today = float(self.bot._broker_day_realized_pnl())
        except Exception:
            realized_today = 0.0
        daily_limit = float(self.bot._daily_loss_limit_used())
        if realized_today <= -abs(daily_limit):
            return False, f"daily loss cap reached ({realized_today:.2f} <= -{daily_limit:.2f})"
        if not self.bot._daily_risk_allows_entry():
            return False, "existing daily risk control blocked entry"
        max_trades = self.bot._effective_max_daily_trades()
        if self.bot._daily_trade_count() >= max_trades:
            _ss.set_status("daily_trade_limit_observed", f"{self.bot._daily_trade_count()}/{max_trades}")
        if not self.bot._daily_trade_count_allows_entry():
            return False, "existing max daily trade control blocked entry"
        max_open = self.bot._effective_max_open_trades()
        if self.bot._open_position_count() >= max_open:
            return False, f"max open trades total reached ({max_open})"
        if MAX_OPEN_TRADES_PER_SYMBOL <= 1 and self.bot._symbol_has_open_position(canonical) and not self.bot._pyramiding_enabled():
            return False, f"max open trades per symbol reached ({canonical})"
        confirmed_pending = bool(sig.get("one_min_confirmation")) and str(sig.get("final_status") or "").upper() == "CONFIRMED"
        symbol_ok, symbol_reason = self.bot._symbol_risk_allows_entry(canonical, confirmed_pending=confirmed_pending)
        if not symbol_ok:
            return False, symbol_reason
        rows = self.bot._today_closed_trades()
        loss_rows = [row for row in rows if self.bot._canonical_from_resolved(str(row.get("sym") or "")).upper() == canonical and self.bot._counts_as_loss_for_limits(row, canonical)]
        if len(loss_rows) >= MAX_LOSSES_PER_SYMBOL_PER_DAY:
            self.bot._audit_decision({
                "event": "PORTFOLIO_QUALITY_OBSERVE_ONLY",
                "decision": "observe_only",
                "scope": "symbol",
                "symbol": canonical,
                "engine": engine,
                "reason": f"symbol loss count observed ({len(loss_rows)}/{MAX_LOSSES_PER_SYMBOL_PER_DAY})",
                "confirmed_pending": confirmed_pending,
            })
        _streak_ok, _streak_reason = self.bot._daily_loss_streak_allows_entry()
        _ss.set_status("loss_streak_status", _streak_reason)
        engine_limit = int(self.engine_open_limits.get(engine, MAX_OPEN_TRADES_TOTAL) or 0)
        if engine_limit > 0 and self._engine_exposure_count(engine) >= engine_limit:
            return False, f"engine exposure limit reached ({engine}: {engine_limit})"
        if self.bot.systematic is not None:
            ok, reason = self.bot._systematic_portfolio_allows_entry(canonical, market, sig)
            if not ok:
                return False, reason
        return True, "OK"


class CostModel:
    def __init__(self, bot):
        self.bot = bot

    def check(self, symbol: str, market: str, sig: dict, volume: float, sl: float, tp: float, risk_meta: dict | None = None, stage: str = "pre_order") -> tuple[bool, str, float, dict]:
        price = float(sig.get("price") or 0.0)
        stage_name = str(stage or "pre_order").strip().lower()
        spread_snapshot = dict(sig.get("_mt5_spread_signal") or {})
        spread_price = float(spread_snapshot.get("spread") or sig.get("spread_signal") or 0.0)
        if spread_price <= 0.0 and stage_name != "scan":
            try:
                _resolved, _tick = self.bot.gateway.symbol_tick(symbol)
                _bid = float(getattr(_tick, "bid", 0.0) or 0.0)
                _ask = float(getattr(_tick, "ask", 0.0) or 0.0)
                if _bid > 0.0 and _ask >= _bid:
                    spread_price = _ask - _bid
                    sig["_mt5_spread_signal"] = {"bid": _bid, "ask": _ask, "spread": spread_price, "source": "live_pre_order_tick"}
            except Exception:
                pass
        spec = (risk_meta or {}).get("spec") or {}
        contract_size = float(spec.get("contract_size") or 1.0) if isinstance(spec, dict) else 1.0
        commission_per_lot = _env_float("MT5_COMMISSION_PER_LOT_USD", 0.0, lo=0.0)
        volume = max(float(volume or 0.0), 0.0)
        target_distance = abs(float(tp or 0.0) - price)
        broker_profit_per_lot = float((risk_meta or {}).get("profit_per_lot") or 0.0)
        target_value = broker_profit_per_lot * volume
        calculation_source = "broker_geometry"
        if target_value <= 0:
            target_value = target_distance * contract_size * volume
            calculation_source = "contract_fallback"
        spread_ratio = abs(spread_price) / target_distance if target_distance > 0 else 999.0
        spread_cost = target_value * spread_ratio if target_value > 0 else 0.0
        slippage_estimate = target_value * spread_ratio * 0.5 if target_value > 0 else 0.0
        commission_estimate = commission_per_lot * volume
        total_cost = spread_cost + slippage_estimate + commission_estimate
        cost_to_target = total_cost / target_value if target_value > 0 else 999.0
        strict_threshold = _env_float("MT5_COST_TO_TARGET_BLOCK", 0.30, lo=0.0, hi=2.0)
        extreme_threshold = _env_float("MT5_COST_TO_TARGET_EXTREME_BLOCK", 0.75, lo=max(strict_threshold, 0.30), hi=2.0)
        scan_extreme_threshold = _env_float("MT5_SCAN_COST_TO_TARGET_BLOCK", strict_threshold, lo=0.0, hi=2.0)
        soft_threshold = _env_float("MT5_COST_TO_TARGET_SOFT", 0.18, lo=0.0, hi=2.0)
        engine_name = str(sig.get("engine") or get_engine_for_symbol(symbol) or "").upper()
        if stage_name == "scan" and engine_name == FOREX_ENGINE:
            scan_extreme_threshold = strict_threshold
        score_before = float(sig.get("score") or 0.0)
        adjusted = score_before
        decision = "allow"
        reason = "OK"
        cost_stage = "pre_order_ok"
        if stage_name == "scan":
            if cost_to_target > scan_extreme_threshold:
                if cost_to_target >= extreme_threshold:
                    decision = "block"
                    cost_stage = "scan_extreme_block"
                    if engine_name == FOREX_ENGINE:
                        reason = f"extreme_cost_to_target ({cost_to_target:.2%} >= {extreme_threshold:.2%})"
                    else:
                        reason = f"extreme scan cost_to_target ({cost_to_target:.2%} >= {extreme_threshold:.2%})"
                else:
                    decision = "quality_filter"
                    cost_stage = "scan_quality_filter"
                    reason = f"cost-to-target quality filter ({cost_to_target:.2%} > {scan_extreme_threshold:.2%} and < {extreme_threshold:.2%})"
            else:
                cost_stage = "scan_warning"
                if cost_to_target >= soft_threshold:
                    decision = "scan_warning"
                    reason = f"scan cost_to_target warning ({cost_to_target:.2%} <= {scan_extreme_threshold:.2%})"
                else:
                    reason = f"scan cost_to_target allowed ({cost_to_target:.2%} <= {scan_extreme_threshold:.2%})"
        else:
            if cost_to_target >= strict_threshold:
                if cost_to_target >= extreme_threshold:
                    decision = "block"
                    cost_stage = "pre_order_block"
                    reason = f"extreme cost_to_target ({cost_to_target:.2%} >= {extreme_threshold:.2%})"
                else:
                    decision = "quality_filter"
                    cost_stage = "pre_order_quality_filter"
                    reason = f"cost-to-target quality filter ({cost_to_target:.2%} >= {strict_threshold:.2%} and < {extreme_threshold:.2%})"
            elif cost_to_target >= soft_threshold:
                decision = "soft_penalty"
                cost_stage = "pre_order_warning"
                penalty = min(6.0, (cost_to_target - soft_threshold) * 100.0)
                adjusted = max(0.0, score_before - penalty)
                reason = f"borderline cost soft penalty ({score_before:.1f}->{adjusted:.1f})"
        details = {
            "stage": stage_name,
            "cost_to_target_stage": cost_stage,
            "calculation_source": calculation_source,
            "spread_cost": round(spread_cost, 6),
            "slippage_estimate": round(slippage_estimate, 6),
            "commission_estimate": round(commission_estimate, 6),
            "total_cost": round(total_cost, 6),
            "target_value": round(target_value, 6),
            "cost_to_target": round(cost_to_target, 6),
            "strict_threshold": strict_threshold,
            "extreme_threshold": extreme_threshold,
            "scan_extreme_threshold": scan_extreme_threshold,
            "soft_threshold": soft_threshold,
            "decision": decision,
            "score_before": score_before,
            "score_after": adjusted,
            "quality_filter": decision == "quality_filter",
        }
        thresholds = sig.setdefault("_mt5_thresholds", {})
        thresholds["cost_model_scan" if stage_name == "scan" else "cost_model_pre_order"] = details
        thresholds["cost_model"] = details
        sig["cost_to_target_pct"] = cost_to_target
        sig["cost_to_target_stage"] = cost_stage
        sig["_cost_extreme_hard"] = decision == "block"
        if stage_name != "scan":
            sig["score"] = adjusted
            sig["score_after"] = adjusted
        if decision == "block":
            self.bot._audit_decision({
                "event": "COST_TO_TARGET_BLOCKED",
                "decision": "block",
                "symbol": self.bot._canonical_from_resolved(symbol),
                "market": market,
                "direction": sig.get("direction"),
                "stage": stage_name,
                "cost_to_target": cost_to_target,
                "threshold": strict_threshold if stage_name != "scan" else scan_extreme_threshold,
                "reason": reason,
            })
            _ss.set_status("last_cost_to_target_block", f"{symbol}: {reason}")
        return decision != "block", reason, adjusted, details


class AggressiveSizingEngine:
    def __init__(self, bot):
        self.bot = bot

    def symbol_tier(self, symbol: str) -> str:
        canonical = canonical_symbol(symbol).upper()
        if canonical in TIER_A_SYMBOLS:
            return "A"
        if canonical in TIER_B_SYMBOLS:
            return "B"
        return "C"

    def target_profit_usd(self, symbol: str, sig: dict) -> float:
        base = _env_float("MT5_TARGET_PROFIT_PER_TRADE_USD", 100.0, lo=1.0, hi=10000.0)
        tier = self.symbol_tier(symbol)
        score = float((sig or {}).get("score") or 0.0)
        min_score = float((sig or {}).get("min_score") or 0.0)
        if tier == "A":
            return base
        if tier == "B":
            return min(base, max(50.0, base * 0.75))
        if score >= min_score:
            return min(base, max(25.0, base * 0.50))
        return min(base, max(10.0, base * 0.25))

    def min_net_profit_usd(self, symbol: str) -> float:
        tier = self.symbol_tier(symbol)
        if tier == "A":
            return _env_float("MT5_TIER_A_MIN_NET_PROFIT_USD", 15.0, lo=0.0)
        if tier == "B":
            return _env_float("MT5_TIER_B_MIN_NET_PROFIT_USD", 8.0, lo=0.0)
        return _env_float("MT5_TIER_C_MIN_NET_PROFIT_USD", 5.0, lo=0.0)

    def _profit_per_lot_at_target(self, spec: MT5SymbolSpec, entry: float, target: float, direction: str) -> float:
        if float(entry or 0.0) <= 0 or float(target or 0.0) <= 0:
            return 0.0
        try:
            projected = self.bot.gateway.order_calc_profit(spec.symbol, direction, 1.0, entry, target)
        except Exception as exc:
            self.bot._audit_decision({
                "event": "BROKER_CALC_UNAVAILABLE",
                "decision": "block",
                "symbol": spec.symbol,
                "calculation": "OrderCalcProfit",
                "reason": str(exc)[:180],
            })
            return 0.0
        return max(0.0, float(projected))

    def _margin_lot_cap(self, spec: MT5SymbolSpec, price: float, direction: str, margin_per_lot: float | None = None) -> tuple[float, dict]:
        try:
            acct = self.bot.gateway.account_info()
            if margin_per_lot is None:
                margin_per_lot = self.bot.gateway.order_calc_margin(spec.symbol, direction, 1.0, price)
        except Exception as exc:
            return 0.0, {"margin_error": str(exc)[:160], "source": "OrderCalcMargin"}
        free_margin = float(getattr(acct, "free_margin", 0.0) or 0.0)
        if free_margin <= 0 or margin_per_lot <= 0:
            return 0.0, {"free_margin": free_margin, "margin_per_lot": margin_per_lot, "source": "OrderCalcMargin"}
        max_fraction = _env_float("MT5_AGGRESSIVE_MAX_MARGIN_FRACTION", 0.35, lo=0.01, hi=0.95)
        reserve_fraction = _env_float("MT5_MARGIN_RESERVE_FRACTION", 0.20, lo=0.0, hi=0.95)
        usable_margin = max(0.0, free_margin * max_fraction - free_margin * reserve_fraction)
        cap = usable_margin / margin_per_lot
        return max(0.0, cap), {"free_margin": free_margin, "margin_per_lot": margin_per_lot, "usable_margin": usable_margin, "max_margin_fraction": max_fraction, "reserve_fraction": reserve_fraction, "source": "OrderCalcMargin"}

    def plan(self, symbol: str, market: str, sig: dict, spec: MT5SymbolSpec, price: float, sl: float, tp: float, strategy_equity: float, base_volume: float, base_meta: dict) -> tuple[float, dict]:
        canonical = canonical_symbol(symbol).upper()
        if not self.bot._aggressive_scalp_effective():
            return base_volume, {"enabled": False, "reason": "aggressive scalp mode inactive"}
        target_profit = self.target_profit_usd(canonical, sig)
        direction = str((sig or {}).get("direction") or "").upper()
        profit_per_lot = float(base_meta.get("profit_per_lot") or self._profit_per_lot_at_target(spec, price, tp, direction))
        if profit_per_lot <= 0:
            return 0.0, {"enabled": True, "reason": "broker target profit unavailable", "target_profit_usd": target_profit}
        required_lot = target_profit / profit_per_lot
        loss_per_lot = float(base_meta.get("loss_per_lot") or self.bot._loss_per_lot(spec, price, abs(price - sl), direction))
        max_trade_risk = self.bot._max_trade_risk_limit_for_symbol(canonical, market)
        max_by_risk = max_trade_risk / loss_per_lot if loss_per_lot > 0 and max_trade_risk > 0 else float(spec.volume_max or required_lot)
        max_lot = self.bot._max_lot_for_symbol(canonical, market)
        if max_lot <= 0:
            max_lot = float(spec.volume_max or required_lot)
        margin_cap, margin_meta = self._margin_lot_cap(spec, price, direction, float(base_meta.get("margin_per_lot") or 0.0) or None)
        volume_cap = min(float(spec.volume_max or required_lot), max_lot, max_by_risk, margin_cap)
        base_margin_cap_volume = float(base_meta.get("volume_by_cap") or 0.0)
        if base_margin_cap_volume > 0:
            volume_cap = min(volume_cap, base_margin_cap_volume)
        pyramid_cap = self.bot._pyramid_addon_volume_cap(canonical, str((sig or {}).get("direction") or ""))
        if pyramid_cap is not None:
            volume_cap = min(volume_cap, pyramid_cap)
        raw_target = max(float(base_volume or 0.0), required_lot)
        selected_raw = min(raw_target, max(0.0, volume_cap))
        volume = self.bot.gateway.normalize_volume(spec, selected_raw)
        expected_gross = profit_per_lot * volume
        shortfall = max(0.0, target_profit - expected_gross)
        details = {
            "enabled": True,
            "tier": self.symbol_tier(canonical),
            "target_profit_usd": round(target_profit, 2),
            "profit_per_lot_at_target": round(profit_per_lot, 4),
            "required_lot": round(required_lot, 4),
            "base_volume": float(base_volume or 0.0),
            "selected_volume": volume,
            "volume_cap": round(volume_cap, 4),
            "max_by_risk": round(max_by_risk, 4),
            "max_trade_risk": round(max_trade_risk, 2),
            "margin": margin_meta,
            "pyramid_volume_cap": pyramid_cap,
            "expected_gross_profit": round(expected_gross, 2),
            "target_profit_shortfall": round(shortfall, 2),
        }
        sig["target_profit_shortfall"] = round(shortfall, 2)
        sig["aggressive_target_profit_usd"] = round(target_profit, 2)
        return volume, details

    def check_expected_net_profit(self, symbol: str, market: str, sig: dict, volume: float, price: float, tp: float, risk_meta: dict, cost_details: dict) -> tuple[bool, str, dict]:
        canonical = canonical_symbol(symbol).upper()
        if not self.bot._aggressive_scalp_effective():
            return True, "OK", {"enabled": False}
        spec_data = (risk_meta or {}).get("spec") or {}
        spec = MT5SymbolSpec(**spec_data) if isinstance(spec_data, dict) else self.bot.gateway.symbol_info(canonical)
        profit_per_lot = self._profit_per_lot_at_target(spec, price, tp, str((sig or {}).get("direction") or "").upper())
        expected_gross = profit_per_lot * max(float(volume or 0.0), 0.0)
        total_cost = float((cost_details or {}).get("total_cost") or 0.0)
        expected_net = expected_gross - total_cost
        min_net = self.min_net_profit_usd(canonical)
        target_profit = self.target_profit_usd(canonical, sig)
        shortfall = max(0.0, target_profit - expected_gross)
        details = {
            "enabled": True,
            "tier": self.symbol_tier(canonical),
            "expected_gross_profit": round(expected_gross, 2),
            "estimated_total_cost": round(total_cost, 2),
            "expected_net_profit": round(expected_net, 2),
            "minimum_net_profit": round(min_net, 2),
            "target_profit_usd": round(target_profit, 2),
            "target_profit_shortfall": round(shortfall, 2),
        }
        sig["target_profit_shortfall"] = round(shortfall, 2)
        sig.setdefault("_mt5_thresholds", {})["minimum_net_profit_filter"] = details
        if expected_net >= min_net:
            return True, "expected net profit passes", details
        if self.bot._demo_winrate_test_effective() and expected_net > 0:
            reason = f"demo smaller positive-net test trade allowed (${expected_net:.2f} net < ${min_net:.2f} target)"
            sig["why_trade_allowed_in_demo"] = reason
            return True, reason, details
        reason = f"NOT_QUALIFIED minimum net profit observed ({chr(36)}{expected_net:.2f} < {chr(36)}{min_net:.2f}); final execution advisory"
        sig["minimum_net_profit_quality_observed"] = True
        self.bot._audit_decision({
            "event": "NOT_QUALIFIED",
            "decision": "observe",
            "scope": "setup",
            "symbol": canonical,
            "engine": sig.get("engine"),
            "reason": reason,
            "details": details,
        })
        return True, reason, details


class ExecutionQualityEngine:
    def __init__(self, bot):
        self.bot = bot

    def pre_order_check(self, symbol: str, market: str, sig: dict, volume: float, sl: float, tp: float) -> tuple[bool, str, dict]:
        canonical = canonical_symbol(self.bot._canonical_from_resolved(symbol)).upper()
        direction = str(sig.get("direction") or "").upper()
        price = float(sig.get("price") or 0.0)
        atr = float(sig.get("atr") or 0.0)
        if not self.bot._entry_window_open(market):
            return False, "market closed before order", {"market_open": False}
        if volume <= 0:
            return False, "lot size invalid before order", {"volume": volume}
        viable, viable_reason = execution_ok(price, atr, market=market)
        if not viable:
            reason = f"NOT_QUALIFIED low volatility observed; final execution advisory: {viable_reason}"
            sig["low_vol_stage"] = "pre_order_observe"
            sig["low_vol_quality_observed"] = True
            details = {"low_vol_stage": "pre_order_observe", "price": price, "atr": atr}
            self.bot._audit_decision({
                "event": "NOT_QUALIFIED",
                "decision": "observe",
                "scope": "setup",
                "symbol": canonical,
                "engine": sig.get("engine"),
                "reason": reason,
                "details": details,
            })
            return True, reason, details
        duplicate_ok, duplicate_reason = self.bot._duplicate_order_allows_entry(canonical, direction)
        if not duplicate_ok:
            return False, duplicate_reason, {"duplicate_ok": False}
        try:
            _resolved, tick = self.bot.gateway.symbol_tick(canonical)
            bid = float(getattr(tick, "bid", 0.0) or 0.0)
            ask = float(getattr(tick, "ask", 0.0) or 0.0)
            ok, reason, tick_info = tick_execution_check(canonical, str(sig.get("engine") or ""), bid, ask, price, atr, float(sig.get("score") or 0.0))
        except Exception as exc:
            return False, f"final tick check failed: {str(exc)[:120]}", {"error": str(exc)[:180]}
        tick_info.update({"duplicate_ok": True, "market_open": True, "volume": volume, "sl": sl, "tp": tp})
        return bool(ok), reason if ok else reason, tick_info

    def confirm_order_result(self, symbol: str, sig: dict, result=None, error: str = "") -> tuple[bool, str, dict]:
        if error:
            details = {"error": error[:240], "accepted": False}
            sig.setdefault("_mt5_thresholds", {})["mt5_order_result"] = details
            return False, f"MT5 order result failed: {error[:160]}", details

        def result_value(name: str, default=None):
            if isinstance(result, dict):
                return result.get(name, default)
            return getattr(result, name, default) if result is not None else default

        retcode = result_value("retcode")
        if retcode is None:
            details = {"retcode": None, "retcode_text": "MISSING_RESULT", "ticket": 0, "fill_price": 0.0, "accepted": False}
            sig.setdefault("_mt5_thresholds", {})["mt5_order_result"] = details
            return False, "MT5 order result missing", details
        retcode_int = int(retcode or 0)
        retcode_text = {
            10008: "TRADE_RETCODE_PLACED",
            10009: "TRADE_RETCODE_DONE",
            10010: "TRADE_RETCODE_DONE_PARTIAL",
        }.get(retcode_int, str(result_value("comment", "")) or f"retcode={retcode_int}")
        ticket_raw = result_value("order", 0) or result_value("deal", 0) or result_value("ticket", 0) or 0
        try:
            ticket = int(ticket_raw or 0)
        except Exception:
            ticket = 0
        fill_price = float(result_value("price", 0.0) or sig.get("fill_price") or sig.get("price") or 0.0)
        accepted = retcode_int in TRADE_RETCODE_DONE
        details = {
            "retcode": retcode_int,
            "retcode_text": retcode_text,
            "ticket": ticket,
            "fill_price": fill_price,
            "accepted": accepted,
        }
        sig["retcode"] = retcode_int
        sig["retcode_text"] = retcode_text
        sig["fill_price"] = fill_price
        sig.setdefault("_mt5_thresholds", {})["mt5_order_result"] = details
        return accepted, "MT5 order accepted" if accepted else f"MT5 order rejected retcode={retcode_int}", details


class AnalyticsEngine:
    def __init__(self, bot):
        self.bot = bot

    def record(self, state: str, symbol: str, engine: str, strategy: str, score: float, reason: str, sig: dict | None = None) -> None:
        sig_payload = sig or {}
        thresholds = sig_payload.get("_mt5_thresholds") if isinstance(sig_payload.get("_mt5_thresholds"), dict) else {}
        order_result = thresholds.get("mt5_order_result") if isinstance(thresholds.get("mt5_order_result"), dict) else {}
        payload = {
            "event": "analytics_engine",
            "state": state,
            "status": state,
            "symbol": canonical_symbol(symbol).upper(),
            "engine": engine,
            "strategy": strategy,
            "score": float(score or 0.0),
            "pending_floor": sig_payload.get("pending_floor"),
            "reason": str(reason or ""),
            "exit_source": sig_payload.get("exit_source"),
            "pnl": sig_payload.get("pnl"),
            "cost_to_target": sig_payload.get("cost_to_target_pct"),
            "spread": sig_payload.get("spread_signal") or sig_payload.get("spread_execution"),
            "slippage": sig_payload.get("slippage"),
            "retcode": sig_payload.get("retcode") or order_result.get("retcode"),
            "ruleset_version": RULESET_VERSION,
            "decision_trace_id": sig_payload.get("decision_trace_id"),
            "generated_at": sig_payload.get("generated_at") or datetime.utcnow().isoformat(),
            "source_loop_name": sig_payload.get("source_loop_name"),
        }
        self.bot._audit_decision(payload)
        try:
            _ss.set_status(f"analytics_last_{str(state).lower()}", f"{payload['symbol']} {engine} {strategy} score={float(score or 0.0):.1f} {str(reason or '')[:80]}")
        except Exception:
            pass


class SymbolHealthManager:
    def __init__(self, bot):
        self.bot = bot
        self.flagged = set(self.bot.state.setdefault("symbol_health_flags", []))

    def check(self, symbol: str, sig: dict) -> tuple[bool, str]:
        canonical = canonical_symbol(self.bot._canonical_from_resolved(symbol)).upper()
        rows = [row for row in self.bot._today_closed_trades(limit=500) if self.bot._canonical_from_resolved(str(row.get("sym") or "")).upper() == canonical]
        min_trades = _env_int("MT5_SYMBOL_HEALTH_MIN_TRADES", 8, lo=1)
        if len(rows) < min_trades:
            return True, "OK"
        pnl = 0.0
        for row in rows:
            try:
                pnl += float(row.get("pnl") or row.get("profit") or row.get("realized") or 0.0)
            except Exception:
                pass
        expectancy = pnl / max(len(rows), 1)
        if expectancy < 0:
            reason = f"negative expectancy symbol flagged ({canonical}: {expectancy:.2f} over {len(rows)} trades)"
            if canonical not in self.flagged:
                self.flagged.add(canonical)
                self.bot.state["symbol_health_flags"] = sorted(self.flagged)
                self.bot._save_state()
                self.bot._audit_decision({"event": "symbol_health_flag", "symbol": canonical, "expectancy": expectancy, "trades": len(rows), "reason": reason, "ruleset_version": RULESET_VERSION})
            if TEST_MODE_GATING and _env_bool("MT5_SYMBOL_HEALTH_BLOCK_NEGATIVE", False):
                return False, reason
            return True, reason
        return True, "OK"


class XM_MT5_Bot:
    def __init__(self, runtime: MT5RuntimeConfig):
        if _strategy_v1 is None or not callable(getattr(_strategy_v1, "evaluate", None)):
            raise RuntimeError("replacement strategy engine unavailable; refusing legacy fallback")
        if not _env_bool("MT5_REPLACEMENT_STRATEGY_V1_LIVE", True):
            raise RuntimeError("MT5_REPLACEMENT_STRATEGY_V1_LIVE must remain enabled; legacy fallback is retired")
        self.runtime = runtime
        self._stop_requested = False
        self.gateway = MT5Gateway(runtime)
        self.state_file = Path(runtime.state_file)
        self.state = self._load_state()
        self.symbol_groups = runtime.symbol_groups()
        self.priority_symbols = _env_symbol_set("MT5_PRIORITY_SYMBOLS", "NAS100,US30,XAUUSD,EURUSD")
        self.symbol_profiles = self._load_symbol_profiles()
        self.symbol_profile_path = (
            str(_mt5_symbol_profiles.profile_path()) if _mt5_symbol_profiles is not None else "/opt/cipherfx_mt5/config/symbol_profiles.json"
        )
        self.symbol_markets = {
            sym: (
                "index_cfd_eu"
                if group == "indices" and sym.upper() in EU_INDEX_SYMBOLS
                else GROUP_MARKET.get(group, "forex")
            )
            for group, symbols in self.symbol_groups.items()
            for sym in symbols
        }
        self._active_scan_stats: dict | None = None
        self.scanner_process_started_at = datetime.utcnow().isoformat()
        self.ruleset_version = RULESET_VERSION
        self.systematic = self._init_systematic_engine()
        self.portfolio_risk_manager = PortfolioRiskManager(self)
        self.cost_model = CostModel(self)
        self.aggressive_sizing_engine = AggressiveSizingEngine(self)
        self.execution_quality_engine = ExecutionQualityEngine(self)
        self.account_mode = "unknown"
        self.risk_profile = "unknown"
        self.demo_winrate_test_mode_effective = False
        self.analytics_engine = AnalyticsEngine(self)
        self.symbol_health_manager = SymbolHealthManager(self)
        _ss.init_db()
        # These are last-event dashboard fields, not persistent trading locks.
        _ss.set_status("last_execution_gate", "RESET: awaiting live execution event")
        _ss.set_status("last_strategy_engine_gate", "RESET: awaiting live engine-policy event")

    def _init_systematic_engine(self):
        if _mt5_systematic_engine is None:
            return None
        try:
            return _mt5_systematic_engine.SystematicTradingEngine(
                Path("/opt/cipherfx_mt5/state/systematic_exports"),
                Path("/opt/cipherfx_mt5/state/systematic_disabled.json"),
            )
        except Exception as exc:
            try:
                _ss.set_status("systematic_engine_error", str(exc)[:180])
            except Exception:
                pass
            return None

    def _load_state(self) -> dict:
        try:
            return json.loads(self.state_file.read_text())
        except Exception:
            return {"open_trades": {}, "closed_tickets": []}

    def _save_state(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        staging = self.state_file.with_suffix(self.state_file.suffix + ".tmp")
        staging.write_text(json.dumps(self.state, indent=2, default=str))
        staging.replace(self.state_file)

    @property
    def open_trades(self) -> dict:
        rows = self.state.setdefault("open_trades", {})
        if isinstance(rows, dict):
            return rows
        normalized: dict[str, dict] = {}
        if isinstance(rows, list):
            for idx, meta in enumerate(rows):
                if not isinstance(meta, dict):
                    continue
                key = str(
                    meta.get("ticket")
                    or meta.get("trade_id")
                    or meta.get("id")
                    or f"legacy_{idx}"
                )
                normalized[key] = meta
        self.state["open_trades"] = normalized
        return normalized

    @property
    def symbol_trade_locks(self) -> dict:
        return self.state.setdefault("symbol_trade_locks", {})

    @property
    def last_symbol_closes(self) -> dict:
        return self.state.setdefault("last_symbol_closes", {})

    @property
    def pending_setups(self) -> dict:
        return self.state.setdefault("pending_setups", {})

    @property
    def campaigns(self) -> dict:
        return self.state.setdefault("campaigns", {})

    def _pending_setup_ttl_seconds(self, symbol: str = "", market: str = "", engine: str = "") -> int:
        # Pending setups have fixed, short execution windows by asset class.
        canonical = canonical_symbol(self._canonical_from_resolved(symbol)).upper()
        market_text = str(market or self._market_for_symbol(canonical) or "").lower()
        engine_text = str(engine or get_engine_for_symbol(canonical) or "").upper()
        if engine_text == INDEX_ENGINE or market_text in {"index", "index_cfd", "index_cfd_eu"}:
            return 90
        if engine_text == METALS_ENGINE or market_text in {"metal", "metals"}:
            return 120
        if engine_text == FOREX_ENGINE or market_text == "forex":
            return 120
        return 120

    def _pending_setup_key(self, symbol: str, side: str) -> str:
        canonical = canonical_symbol(self._canonical_from_resolved(str(symbol or "").split(":")[0])).upper()
        direction = str(side or "").upper()
        return f"{canonical}:{direction}"

    def _pending_setup_status(self, setup: dict | None) -> str:
        if not isinstance(setup, dict):
            return ""
        status = str(setup.get("status") or "").strip().lower()
        if status:
            return status
        state = str(setup.get("state") or "").upper()
        if state in {PENDING, PENDING_SOFT}:
            return "waiting_1m"
        if state == CONFIRMED:
            return "confirmed_1m"
        if state == EXECUTED:
            return "entered"
        if state == CANCELLED:
            return "cancelled"
        return "waiting_1m"

    def _pending_setup_is_terminal(self, setup: dict | None) -> bool:
        return self._pending_setup_status(setup) in {"entered", "cancelled", "expired", "order_rejected"}

    def _pending_setup_expires_at(self, setup: dict, now: datetime | None = None) -> datetime:
        now = now or datetime.utcnow()
        created = _parse_dt(setup.get("created_at")) or now
        ttl_default = self._pending_setup_ttl_seconds(
            str(setup.get("symbol") or ""),
            str(setup.get("market") or ""),
            str(setup.get("engine") or ""),
        )
        ttl_seconds = min(int(float(setup.get("ttl_seconds") or ttl_default)), ttl_default)
        expires = _parse_dt(setup.get("expires_at"))
        capped_expiry = created + timedelta(seconds=ttl_seconds)
        return min(expires, capped_expiry) if expires else capped_expiry

    def _pending_setup_age_seconds(self, setup: dict, now: datetime | None = None) -> float:
        now = now or datetime.utcnow()
        created = _parse_dt(setup.get("created_at"))
        if created is None:
            return 0.0
        return max(0.0, (now - created).total_seconds())

    def _pending_setup_seconds_until_expiry(self, setup: dict, now: datetime | None = None) -> float:
        now = now or datetime.utcnow()
        expires = self._pending_setup_expires_at(setup, now)
        return (expires - now).total_seconds()

    def _pending_setup_is_expired(self, setup: dict, now: datetime | None = None) -> bool:
        if not isinstance(setup, dict):
            return True
        if self._pending_setup_is_terminal(setup):
            return False
        now = now or datetime.utcnow()
        return self._pending_setup_seconds_until_expiry(setup, now) <= 0

    def _normalize_pending_setup_key(self, key: str, setup: dict, now: datetime | None = None) -> tuple[str, dict]:
        now = now or datetime.utcnow()
        raw_symbol = str(setup.get("symbol") or str(key or "").split(":")[0] or "").upper()
        canonical = canonical_symbol(self._canonical_from_resolved(raw_symbol)).upper()
        side = str(setup.get("side") or setup.get("direction") or str(key or "").split(":")[-1] or "").upper()
        pending_key = self._pending_setup_key(canonical, side)
        setup["pending_key"] = pending_key
        setup["symbol"] = canonical
        setup["side"] = side
        setup["direction"] = side
        setup.setdefault("setup_id", f"setup_{canonical}_{side}_{str(setup.get('created_at') or now.isoformat()).replace(':', '').replace('.', '')}")
        setup.setdefault("created_at", now.isoformat())
        ttl_default = self._pending_setup_ttl_seconds(canonical, setup.get("market") or "", setup.get("engine") or "")
        existing_ttl = int(float(setup.get("ttl_seconds") or ttl_default))
        ttl_seconds = min(existing_ttl, ttl_default)
        setup["ttl_seconds"] = ttl_seconds
        created_at = _parse_dt(setup.get("created_at")) or now
        capped_expiry = created_at + timedelta(seconds=ttl_seconds)
        existing_expiry = _parse_dt(setup.get("expires_at"))
        setup["expires_at"] = (min(existing_expiry, capped_expiry) if existing_expiry else capped_expiry).isoformat()
        setup.setdefault("status", self._pending_setup_status(setup))
        setup.setdefault("trigger_timeframe", "5M")
        setup.setdefault("waiting_for", "1M_CONFIRMATION" if self._pending_setup_status(setup) == "waiting_1m" else "")
        store = self.pending_setups
        if key != pending_key:
            existing = store.get(pending_key)
            if not isinstance(existing, dict):
                store[pending_key] = setup
            store.pop(key, None)
        return pending_key, store.get(pending_key) if isinstance(store.get(pending_key), dict) else setup

    def _pending_setup_snapshot(self, sig: dict | None, timeframe: str) -> dict:
        if not isinstance(sig, dict):
            return {}
        prefix = str(timeframe or "").lower()
        candidates = {
            "time": sig.get(f"{prefix}_time") or sig.get(f"latest_{prefix}_bar") or sig.get("latest_m5_bar"),
            "open": sig.get(f"{prefix}_open"),
            "high": sig.get(f"{prefix}_high"),
            "low": sig.get(f"{prefix}_low"),
            "close": sig.get(f"{prefix}_close") or sig.get("price"),
            "price": sig.get("price"),
            "atr": sig.get("atr"),
            "adx": sig.get("adx"),
            "spread": sig.get("spread_signal"),
            "direction_source": sig.get("direction_source"),
        }
        return {key: self._audit_json_safe(value) for key, value in candidates.items() if value not in (None, "")}

    def _pending_setup_summary(self, key: str, setup: dict, now: datetime | None = None) -> dict:
        now = now or datetime.utcnow()
        return {
            "pending_key": key,
            "setup_id": str(setup.get("setup_id") or ""),
            "symbol": str(setup.get("symbol") or "").upper(),
            "side": str(setup.get("side") or setup.get("direction") or "").upper(),
            "status": self._pending_setup_status(setup),
            "age_seconds": round(self._pending_setup_age_seconds(setup, now), 1),
            "setup_created_at": str(setup.get("setup_created_at") or setup.get("created_at") or ""),
            "expires_at": self._pending_setup_expires_at(setup, now).isoformat(),
            "seconds_until_expiry": round(self._pending_setup_seconds_until_expiry(setup, now), 1),
        }

    def _log_pending_setup_event(self, event: str, key: str, setup: dict, reason: str = "", sig: dict | None = None) -> None:
        now = datetime.utcnow()
        summary = self._pending_setup_summary(key, setup, now)
        safe_reason = str(reason or "").replace("\n", " ").replace("\r", " ")[:240]
        msg = (
            f"event={event} setup_id={summary['setup_id']} pending_key={summary['pending_key']} "
            f"symbol={summary['symbol']} side={summary['side']} status={summary['status']} "
            f"setup_created_at={summary['setup_created_at']} age_seconds={summary['age_seconds']:.1f} "
            f"seconds_until_expiry={summary['seconds_until_expiry']:.1f} reason={safe_reason}"
        )
        print(f"[MT5_PENDING_SETUP] {msg}", flush=True)
        _ss.set_status("last_pending_setup_event", msg)
        self._audit_decision({"event": event, "ruleset_version": RULESET_VERSION, "reason": safe_reason, **summary})
        if isinstance(sig, dict):
            trace_summary = {f"setup_{key}": value for key, value in summary.items()}
            self._trace_step(sig, event, summary["status"], safe_reason, **trace_summary)

    def _publish_pending_setup_debug(self, now: datetime | None = None) -> None:
        now = now or datetime.utcnow()
        rows = []
        for key, setup in list(self.pending_setups.items()):
            if not isinstance(setup, dict) or self._pending_setup_is_terminal(setup):
                continue
            rows.append(self._pending_setup_summary(str(key), setup, now))
        text = "; ".join(
            f"{row['symbol']} {row['side']} {row['status']} age={row['age_seconds']:.1f}s exp_in={row['seconds_until_expiry']:.1f}s expires_at={row['expires_at']}"
            for row in rows[:20]
        )
        _ss.set_status("pending_setup_active_count", len(rows))
        _ss.set_status("pending_setup_debug", text or "none")
        print(f"[MT5_PENDING_DEBUG] active_count={len(rows)} setups={text or 'none'}", flush=True)

    def cleanup_expired_pending_setups(self, now: datetime | None = None, publish_debug: bool = True) -> int:
        now = now or datetime.utcnow()
        removed = 0
        changed = False
        for key, setup in list(self.pending_setups.items()):
            if not isinstance(setup, dict):
                self.pending_setups.pop(key, None)
                removed += 1
                changed = True
                continue
            pending_key, setup = self._normalize_pending_setup_key(str(key), setup, now)
            changed = changed or pending_key != key
            if self._pending_setup_is_terminal(setup):
                self.pending_setups.pop(pending_key, None)
                removed += 1
                changed = True
                continue
            if self._pending_setup_is_expired(setup, now):
                waiting_for_confirmation = self._pending_setup_status(setup) == "waiting_1m"
                setup["status"] = "expired"
                setup["expired_at"] = now.isoformat()
                setup["waiting_for"] = ""
                self._log_pending_setup_event("NOT_QUALIFIED", pending_key, setup, "NOT_QUALIFIED pending setup TTL expired")
                self.pending_setups.pop(pending_key, None)
                removed += 1
                changed = True
        if changed:
            self._save_state()
        if publish_debug:
            self._publish_pending_setup_debug(now)
        return removed

    def _active_pending_setup_for(self, symbol: str, side: str, now: datetime | None = None) -> tuple[str, dict | None]:
        now = now or datetime.utcnow()
        canonical = canonical_symbol(self._canonical_from_resolved(symbol)).upper()
        pending_key = self._pending_setup_key(canonical, side)
        setup = self.pending_setups.get(pending_key)
        if not isinstance(setup, dict):
            legacy = self.pending_setups.get(canonical)
            if isinstance(legacy, dict) and str(legacy.get("side") or legacy.get("direction") or "").upper() == str(side or "").upper():
                pending_key, setup = self._normalize_pending_setup_key(canonical, legacy, now)
        if not isinstance(setup, dict):
            return pending_key, None
        if self._pending_setup_is_expired(setup, now):
            setup["status"] = "expired"
            setup["expired_at"] = now.isoformat()
            setup["waiting_for"] = ""
            self._log_pending_setup_event("NOT_QUALIFIED", pending_key, setup, "NOT_QUALIFIED pending setup TTL expired before new setup")
            self.pending_setups.pop(pending_key, None)
            self._save_state()
            return pending_key, None
        if self._pending_setup_is_terminal(setup):
            self.pending_setups.pop(pending_key, None)
            self._save_state()
            return pending_key, None
        return pending_key, setup

    def _set_pending_setup_status(
        self,
        key: str,
        setup: dict,
        status: str,
        event: str,
        reason: str = "",
        sig: dict | None = None,
        save: bool = True,
    ) -> None:
        if not isinstance(setup, dict):
            return
        now = datetime.utcnow()
        setup["status"] = status
        setup["status_updated_at"] = now.isoformat()
        setup["waiting_for"] = "" if status != "waiting_1m" else "1M_CONFIRMATION"
        if status == "confirmed_1m":
            setup["confirmed_at"] = now.isoformat()
        elif status == "order_sent":
            setup["order_sent_at"] = now.isoformat()
        elif status == "entered":
            setup["entered_at"] = now.isoformat()
        elif status in {"cancelled", "order_rejected"}:
            setup["cancelled_at"] = now.isoformat()
        self._log_pending_setup_event(event, key, setup, reason, sig)
        if save:
            self._save_state()

    def _remove_pending_setup(
        self,
        key: str,
        setup: dict,
        status: str,
        event: str,
        reason: str = "",
        sig: dict | None = None,
    ) -> None:
        self._set_pending_setup_status(key, setup, status, event, reason, sig, save=False)
        setup_id = str(setup.get("setup_id") or "") if isinstance(setup, dict) else ""
        symbol = str(setup.get("symbol") or "").upper() if isinstance(setup, dict) else ""
        for candidate_key, candidate_setup in list(self.pending_setups.items()):
            if candidate_key == key:
                self.pending_setups.pop(candidate_key, None)
                continue
            if isinstance(candidate_setup, dict) and setup_id and str(candidate_setup.get("setup_id") or "") == setup_id:
                self.pending_setups.pop(candidate_key, None)
                continue
            if candidate_key == symbol and isinstance(candidate_setup, dict) and setup_id and str(candidate_setup.get("setup_id") or "") == setup_id:
                self.pending_setups.pop(candidate_key, None)
        self._save_state()

    def _trade_id_in_open_state(self, trade_id: str) -> bool:
        if not trade_id:
            return False
        return any(str(meta.get("trade_id") or "") == trade_id for meta in self.open_trades.values())

    def _account_mode_from_acct(self, acct) -> str:
        if bool(getattr(self.runtime, "dry_run", False)):
            return "paper"
        text = " ".join(str(getattr(acct, name, "") or "") for name in ("server", "name")).lower()
        if "demo" in text or "paper" in text or "test" in text:
            return "demo"
        return "live"

    def _demo_or_paper_account(self, acct=None) -> bool:
        if acct is None:
            try:
                acct = self.gateway.account_info()
            except Exception:
                return self.account_mode in {"demo", "paper"}
        return self._account_mode_from_acct(acct) in {"demo", "paper"}

    def _demo_winrate_test_effective(self, acct=None) -> bool:
        return _env_bool("MT5_DEMO_WINRATE_TEST_MODE", True) and self._demo_or_paper_account(acct)

    def _aggressive_scalp_effective(self, acct=None) -> bool:
        return _env_bool("MT5_AGGRESSIVE_SCALP_MODE", True) and self._demo_winrate_test_effective(acct)

    def _pyramiding_enabled(self) -> bool:
        return _env_bool("MT5_ALLOW_PYRAMIDING", True)

    def _effective_max_open_trades(self) -> int:
        return _env_int("MT5_MAX_OPEN_TRADES_TOTAL", 30, lo=1, hi=30)

    def _effective_max_daily_trades(self) -> int:
        return _env_int("MT5_MAX_TRADES_PER_DAY", 60, lo=1, hi=60)

    def _daily_loss_limit_used(self, acct=None) -> float:
        return _env_float("MT5_MAX_DAILY_LOSS_USD", 1000.0, lo=0.0)

    def _update_demo_profile_status(self, acct=None) -> None:
        if acct is None:
            try:
                acct = self.gateway.account_info()
            except Exception:
                acct = None
        account_mode = self._account_mode_from_acct(acct) if acct is not None else self.account_mode
        demo_mode = self._demo_winrate_test_effective(acct)
        risk_profile = "demo" if demo_mode and _env_bool("MT5_USE_DEMO_RISK_PROFILE", True) else "live"
        self.account_mode = account_mode
        self.risk_profile = risk_profile
        self.demo_winrate_test_mode_effective = demo_mode
        _ss.set_status("account_mode", account_mode)
        _ss.set_status("risk_profile", risk_profile)
        _ss.set_status("daily_loss_limit_used", self._daily_loss_limit_used(acct))
        _ss.set_status("demo_winrate_test_mode", demo_mode)
        _ss.set_status("aggressive_scalp_mode", self._aggressive_scalp_effective(acct))
        _ss.set_status("allow_pyramiding", self._pyramiding_enabled())
        _ss.set_status("max_open_trades", self._effective_max_open_trades())
        _ss.set_status("max_daily_trades", self._effective_max_daily_trades())

    def _effective_pending_floor(self, symbol: str, min_score: float, engine: str = "") -> float:
        base = pending_floor_for_symbol(symbol, min_score, engine)
        if self._demo_winrate_test_effective():
            return max(0.0, base - 5.0)
        return base

    def _max_trade_risk_limit_for_symbol(self, canonical: str, market: str) -> float:
        default = _env_float("MT5_DEMO_MAX_TRADE_RISK_USD", 200.0, lo=0.0) if self._demo_winrate_test_effective() else _env_float("MT5_MAX_TRADE_RISK_USD", 100.0, lo=0.0)
        return self._loss_limit_for_symbol(canonical, market, "MT5_MAX_TRADE_RISK", default)

    def _strategy_equity(self, acct) -> float:
        equity = float(getattr(acct, "equity", 0.0) or 0.0)
        cap = float(self.runtime.capital_cap_usd or CFG.get("capital_cap_usd", 5000.0) or 5000.0)
        if equity <= 0:
            return cap
        return min(equity, cap) if cap > 0 else equity

    def _account_trading_allowed(self, acct) -> tuple[bool, str]:
        actual = int(getattr(acct, "login", 0) or 0)
        expected = int(self.runtime.login or 0)
        if expected and actual <= 0:
            return False, "waiting for MT5 account snapshot"
        if expected and actual != expected:
            return False, f"MT5 account mismatch: expected {expected}, got {actual}"
        if not bool(getattr(acct, "terminal_connected", True)):
            return False, "MT5 terminal is disconnected"
        if not bool(getattr(acct, "trade_allowed", True)):
            return False, "MT5 terminal algo trading is disabled"
        if not bool(getattr(acct, "account_trade_allowed", True)):
            return False, "MT5 account trading is disabled"
        return True, "OK"

    def _market_for_symbol(self, sym: str) -> str:
        canonical = self._canonical_from_resolved(sym)
        return self.symbol_markets.get(canonical, GROUP_MARKET.get(self.runtime.group_for_symbol(canonical), "forex"))

    def _loss_limit_for_symbol(self, canonical: str, market: str, prefix: str, default: float) -> float:
        symbol_key = f"{prefix}_{canonical.upper()}_USD"
        if os.getenv(symbol_key) is not None:
            value = _env_float(symbol_key, default, lo=0.0)
        elif canonical.upper().startswith(("XAU", "XAG", "XPT", "XPD")):
            value = _env_float(f"{prefix}_METALS_USD", default, lo=0.0)
        elif market in {"index_cfd", "index_cfd_eu"}:
            value = _env_float(f"{prefix}_INDICES_USD", default, lo=0.0)
        elif market == "stock_us":
            value = _env_float(f"{prefix}_STOCKS_USD", _env_float(prefix + "_USD", default, lo=0.0), lo=0.0)
        elif market == "crypto":
            value = _env_float(f"{prefix}_CRYPTO_USD", default, lo=0.0)
        elif market == "energy":
            value = _env_float(f"{prefix}_ENERGIES_USD", default, lo=0.0)
        elif market == "forex":
            value = _env_float(f"{prefix}_FOREX_USD", default, lo=0.0)
        else:
            value = _env_float(prefix + "_USD", default, lo=0.0)
        field = {
            "MT5_MAX_TRADE_RISK": "max_trade_risk_usd",
            "MT5_MAX_OPEN_POSITION_LOSS": "max_open_position_loss_usd",
            "MT5_MAX_SYMBOL_FLOATING_LOSS": "max_symbol_floating_loss_usd",
        }.get(prefix)
        return self._profile_cap_float(canonical, field, value) if field else value

    def _max_lot_for_symbol(self, canonical: str, market: str) -> float:
        symbol_key = f"MT5_MAX_LOT_{canonical.upper()}"
        if os.getenv(symbol_key) is not None:
            value = _env_float(symbol_key, 0.0, lo=0.0)
        elif canonical.upper().startswith(("XAU", "XAG", "XPT", "XPD")):
            value = _env_float("MT5_MAX_LOT_METALS", 0.01, lo=0.0)
        elif market in {"index_cfd", "index_cfd_eu"}:
            value = _env_float("MT5_MAX_LOT_INDICES", 0.10, lo=0.0)
        elif market == "stock_us":
            value = _env_float("MT5_MAX_LOT_STOCKS", _env_float("MT5_MAX_LOT_DEFAULT", 0.10, lo=0.0), lo=0.0)
        elif market == "crypto":
            value = _env_float("MT5_MAX_LOT_CRYPTO", 0.01, lo=0.0)
        elif market == "energy":
            value = _env_float("MT5_MAX_LOT_ENERGIES", _env_float("MT5_MAX_LOT_DEFAULT", 0.10, lo=0.0), lo=0.0)
        elif market == "forex":
            value = _env_float("MT5_MAX_LOT_FOREX", 0.25, lo=0.0)
        else:
            value = _env_float("MT5_MAX_LOT_DEFAULT", 0.10, lo=0.0)
        return self._profile_cap_float(canonical, "max_lot", value)

    def _risk_pct_for_symbol(self, canonical: str, market: str) -> float:
        default = float(CFG.get("risk_pct", 0.35) or 0.35)
        value = self._env_float_for_symbol("MT5_RISK_PCT", canonical, market, default, lo=0.01, hi=10.0)
        return self._profile_cap_float(canonical, "risk_pct", value)

    def _position_cap_pct_for_symbol(self, canonical: str, market: str) -> float:
        default = float(self.runtime.max_position_pct or 1.0)
        value = self._env_float_for_symbol("MT5_MAX_POSITION_PCT", canonical, market, default, lo=0.01, hi=50.0)
        return self._profile_cap_float(canonical, "max_position_pct", value)

    def _scalp_r_for_symbol(self, prefix: str, canonical: str, market: str, default: float) -> float:
        clean_symbol = canonical.upper().replace(".", "_")
        symbol_key = f"{prefix}_{clean_symbol}"
        if os.getenv(symbol_key) is not None:
            value = _env_float(symbol_key, default, lo=0.0, hi=10.0)
        elif canonical.upper().startswith(("XAU", "XAG", "XPT", "XPD")):
            value = _env_float(f"{prefix}_METALS", default, lo=0.0, hi=10.0)
        elif market in {"index_cfd", "index_cfd_eu"}:
            value = _env_float(f"{prefix}_INDICES", default, lo=0.0, hi=10.0)
        elif market == "stock_us":
            value = _env_float(f"{prefix}_STOCKS", default, lo=0.0, hi=10.0)
        elif market == "crypto":
            value = _env_float(f"{prefix}_CRYPTO", default, lo=0.0, hi=10.0)
        elif market == "forex":
            value = _env_float(f"{prefix}_FOREX", default, lo=0.0, hi=10.0)
        else:
            value = _env_float(prefix, default, lo=0.0, hi=10.0)
        field = {
            "MT5_SCALP_TP_R": "scalp_tp_r",
            "MT5_SCALP_CLOSE_R": "scalp_close_r",
        }.get(prefix)
        return self._profile_cap_float(canonical, field, value) if field else value

    def _is_metal_symbol(self, canonical: str) -> bool:
        return canonical.upper().startswith(("XAU", "XAG", "XPT", "XPD"))

    def _env_float_for_symbol(self, prefix: str, canonical: str, market: str, default: float,
                              lo: float | None = None, hi: float | None = None) -> float:
        return self._env_float_for_symbol_with_source(prefix, canonical, market, default, lo=lo, hi=hi)[0]

    def _env_float_for_symbol_with_source(self, prefix: str, canonical: str, market: str, default: float,
                                          lo: float | None = None,
                                          hi: float | None = None) -> tuple[float, str]:
        clean_symbol = canonical.upper().replace(".", "_")
        symbol_key = f"{prefix}_{clean_symbol}"
        if os.getenv(symbol_key) is not None:
            return _env_float(symbol_key, default, lo=lo, hi=hi), symbol_key
        if self._is_metal_symbol(canonical):
            metal_key = f"{prefix}_METALS"
            if os.getenv(metal_key) is not None:
                return _env_float(metal_key, default, lo=lo, hi=hi), metal_key
        if market == "metal":
            key = f"{prefix}_METALS"
        elif market == "energy":
            key = f"{prefix}_ENERGIES"
        elif market in {"index_cfd", "index_cfd_eu"}:
            key = f"{prefix}_INDICES"
        elif market == "stock_us":
            key = f"{prefix}_STOCKS"
        elif market == "crypto":
            key = f"{prefix}_CRYPTO"
        elif market == "forex":
            key = f"{prefix}_FOREX"
        else:
            key = prefix
        if os.getenv(key) is not None:
            return _env_float(key, default, lo=lo, hi=hi), key
        if os.getenv(prefix) is not None:
            return _env_float(prefix, default, lo=lo, hi=hi), prefix
        return float(default), "default"

    def _load_symbol_profiles(self) -> dict:
        if _mt5_symbol_profiles is None:
            return {}
        try:
            return _mt5_symbol_profiles.load_profiles()
        except Exception as exc:
            try:
                _ss.set_status("symbol_profile_error", f"load failed: {str(exc)[:160]}")
            except Exception:
                pass
            return {}

    def _symbol_profile(self, canonical: str) -> dict | None:
        return self.symbol_profiles.get(str(canonical or "").upper())

    def _profile_float(self, canonical: str, field: str) -> float | None:
        if _mt5_symbol_profiles is None:
            return None
        return _mt5_symbol_profiles.numeric(self._symbol_profile(canonical), field)

    def _profile_int(self, canonical: str, field: str) -> int | None:
        if _mt5_symbol_profiles is None:
            return None
        return _mt5_symbol_profiles.integer(self._symbol_profile(canonical), field)

    def _profile_set(self, canonical: str, field: str) -> set[str]:
        if _mt5_symbol_profiles is None:
            return set()
        return _mt5_symbol_profiles.symbol_set(self._symbol_profile(canonical), field)

    def _profile_configured_fields(self, canonical: str) -> list[str]:
        if _mt5_symbol_profiles is None:
            return []
        return _mt5_symbol_profiles.configured_fields(self._symbol_profile(canonical))

    def _profile_cap_float(self, canonical: str, field: str | None, fallback: float) -> float:
        if not field:
            return float(fallback)
        value = self._profile_float(canonical, field)
        if value is None or value <= 0:
            return float(fallback)
        if float(fallback) > 0:
            return min(float(fallback), float(value))
        return float(value)

    def _profile_floor_float(self, canonical: str, field: str | None, fallback: float) -> float:
        if not field:
            return float(fallback)
        value = self._profile_float(canonical, field)
        if value is None:
            return float(fallback)
        return max(float(fallback), float(value))

    def _profile_cap_int(self, canonical: str, field: str, fallback: int) -> int:
        value = self._profile_int(canonical, field)
        if value is None:
            return int(fallback)
        if int(fallback) > 0:
            if value <= 0:
                return int(fallback)
            return min(int(fallback), int(value))
        return int(value)

    def _profile_floor_int(self, canonical: str, field: str, fallback: int) -> int:
        value = self._profile_int(canonical, field)
        if value is None:
            return int(fallback)
        return max(int(fallback), int(value))

    def _symbol_profile_gate_allows_entry(self, sym: str, market: str, sig: dict) -> tuple[bool, str]:
        canonical = self._canonical_from_resolved(sym).upper()
        profile = self._symbol_profile(canonical)
        profile_loaded = profile is not None
        decision = "allow"
        reason = "OK"
        if not profile_loaded:
            reason = "symbol profile not loaded; using env/default fallbacks"
        else:
            enabled = bool(profile.get("enabled", True))
            strategy = str(sig.get("strategy") or sig.get("mode") or "").upper()
            blocked = self._profile_set(canonical, "blocked_strategies")
            allowed = self._profile_set(canonical, "allowed_strategies")
            if not enabled:
                decision = "block"
                reason = f"symbol profile disabled for {canonical}"
            elif strategy and strategy in blocked:
                decision = "block"
                reason = f"strategy blocked by symbol profile for {canonical}: {strategy}"
            elif strategy and allowed and strategy not in allowed:
                decision = "block"
                reason = f"strategy not allowed by symbol profile for {canonical}: {strategy}"
        self._audit_decision({
            "event": "symbol_profile_gate",
            "profile_decision": decision,
            "symbol": canonical,
            "market": market,
            "profile_loaded": bool(profile_loaded),
            "profile_path": self.symbol_profile_path,
            "decision": decision,
            "reason": reason,
            "strategy": str(sig.get("strategy") or ""),
            "asset_class": sig.get("asset_class"),
            "engine": sig.get("engine"),
            "strategy_before": sig.get("strategy_before"),
            "strategy_after": sig.get("strategy_after"),
            "score": float(sig.get("score") or 0.0),
            "score_before": sig.get("score_before"),
            "score_after": sig.get("score_after"),
            "min_score": sig.get("min_score"),
            "pending_floor": sig.get("pending_floor"),
            "cost_to_target": sig.get("cost_to_target_pct"),
            "spread": sig.get("spread_signal") or sig.get("spread_execution"),
            "slippage": sig.get("slippage") if sig.get("slippage") is not None else 0.0,
            "retcode": sig.get("retcode"),
            "exit_source": sig.get("exit_source"),
            "pnl": sig.get("pnl"),
            "adx_reason": sig.get("adx_reason"),
            "one_min_confirmation": sig.get("one_min_confirmation"),
            "applied_profile_fields": self._profile_configured_fields(canonical),
            "fallback_fields": [field for field in ("risk_pct", "max_lot", "max_trade_risk_usd", "max_position_pct", "scalp_tp_r", "timeout_minutes", "min_score", "atr_stop_mult") if field not in self._profile_configured_fields(canonical)],
            "fallback_source": "symbol_profiles.json -> mt5_strategy_engines.SYMBOL_CONFIG -> /etc/scalpbot/scalpbot-mt5.env -> code defaults",
        })
        _ss.set_status("last_symbol_profile_resolution", f"{canonical}: applied={self._profile_configured_fields(canonical)} fallback_source=symbol_profiles.json->SYMBOL_CONFIG->env->defaults")
        if decision != "allow":
            sig["_force_quality_filter"] = True
            _ss.set_status("last_symbol_profile_gate", f"NOT_QUALIFIED {canonical}: {reason}")
            return False, reason
        _ss.set_status("last_symbol_profile_gate", f"ALLOW {canonical}: {reason}")
        return True, reason

    def _env_threshold_snapshot(self, prefixes: list[str], canonical: str, market: str) -> dict:
        clean_symbol = canonical.upper().replace(".", "_")
        market_suffix = {
            "metal": "METALS",
            "energy": "ENERGIES",
            "index_cfd": "INDICES",
            "stock_us": "STOCKS",
            "crypto": "CRYPTO",
            "forex": "FOREX",
        }.get(market)
        keys: list[str] = []
        for prefix in prefixes:
            keys.append(prefix)
            if market_suffix:
                keys.append(f"{prefix}_{market_suffix}")
            if self._is_metal_symbol(canonical):
                keys.append(f"{prefix}_METALS")
            keys.append(f"{prefix}_{clean_symbol}")
        snapshot = {}
        for key in dict.fromkeys(keys):
            raw = os.getenv(key)
            if raw is not None:
                snapshot[key] = raw
        return snapshot

    def _entry_time_block_reason(self, canonical: str, market: str) -> str | None:
        clean_symbol = canonical.upper().replace(".", "_")
        market_key = str(market or "").upper().replace(" ", "_")
        keys = [f"MT5_BLOCK_ENTRY_UTC_WINDOWS_{clean_symbol}"]
        if self._is_metal_symbol(canonical):
            keys.append("MT5_BLOCK_ENTRY_UTC_WINDOWS_METALS")
        keys.extend([f"MT5_BLOCK_ENTRY_UTC_WINDOWS_{market_key}", "MT5_BLOCK_ENTRY_UTC_WINDOWS"])
        now = datetime.utcnow()
        now_min = now.hour * 60 + now.minute
        for key in keys:
            raw = os.getenv(key, "")
            if not raw:
                continue
            for chunk in raw.replace(";", ",").split(","):
                window = chunk.strip()
                if not window or "-" not in window:
                    continue
                start_text, end_text = [part.strip() for part in window.split("-", 1)]
                try:
                    start_h, start_m = [int(part) for part in start_text.split(":", 1)]
                    end_h, end_m = [int(part) for part in end_text.split(":", 1)]
                except Exception:
                    continue
                start = (start_h % 24) * 60 + max(0, min(59, start_m))
                end = (end_h % 24) * 60 + max(0, min(59, end_m))
                blocked = start <= now_min < end if start <= end else (now_min >= start or now_min < end)
                if blocked:
                    return f"entry blocked by {key} {window} UTC"
        return None

    def _cap_stop_distance_for_symbol(self, canonical: str, market: str, stop_d: float, sig: dict | None = None) -> float:
        cap = self._env_float_for_symbol("MT5_MAX_STOP_DISTANCE", canonical, market, 0.0, lo=0.0)
        profile_cap = self._profile_float(canonical, "max_stop_distance")
        if profile_cap is not None and profile_cap > 0:
            cap = min(cap, profile_cap) if cap > 0 else profile_cap
        if cap > 0 and stop_d > cap:
            if sig is not None:
                sig.setdefault("conditions", {})["MT5 max stop distance"] = cap
            return cap
        return stop_d

    def _entry_rr_for_symbol(self, canonical: str, market: str, sig: dict, configured_rr: float | None = None) -> float:
        canonical = canonical_symbol(self._canonical_from_resolved(canonical)).upper()
        cfg = get_symbol_config(canonical) or {}
        configured_value = configured_rr if configured_rr is not None else cfg.get("tp_r")
        try:
            configured_value = float(configured_value or 0.0)
        except Exception:
            configured_value = 0.0
        if configured_value > 0:
            base_rr = configured_value
            base_source = "symbol_config.tp_r"
        else:
            base_rr = _rr_for_market(
                market,
                sym=canonical,
                sig_mode=str(sig.get("mode") or ""),
                is_crypto=(market == "crypto"),
            )
            base_source = "market_default"
        if bool(sig.get("replacement_strategy_v1")):
            asset = str(sig.get("asset_class") or cfg.get("asset_class") or "").lower()
            rr = {"forex": 1.60, "index": 1.80, "metal": 1.80}.get(asset, float(base_rr))
            meta = {
                "configured_rr": configured_value or None,
                "base_rr": round(float(base_rr), 6),
                "effective_rr": round(float(rr), 6),
                "source": "strategy_v1.engine_rr",
                "engine": str(sig.get("engine") or ""),
            }
            sig.setdefault("_mt5_thresholds", {})["rr_resolution"] = meta
            sig["rr_used"] = rr
            sig["rr_source"] = meta["source"]
            return rr
        if not _env_bool("MT5_SCALP_TP_ENABLE", True):
            rr = float(base_rr)
            source = f"{base_source};scalp_tp_disabled"
            meta = {
                "configured_rr": configured_value or None,
                "base_rr": base_rr,
                "profile_rr": None,
                "symbol_override_rr": None,
                "effective_rr": rr,
                "source": source,
            }
            sig.setdefault("_mt5_thresholds", {})["rr_resolution"] = meta
            sig["rr_used"] = rr
            sig["rr_source"] = source
            sig.setdefault("conditions", {})["MT5 scalp TP R"] = rr
            return rr

        profile_rr = self._profile_float(canonical, "scalp_tp_r")
        if profile_rr is not None and profile_rr <= 0:
            profile_rr = None
        clean_symbol = canonical.replace(".", "_")
        symbol_key = f"MT5_SCALP_TP_R_{clean_symbol}"
        symbol_override_rr = None
        if os.getenv(symbol_key) is not None:
            symbol_override_rr = _env_float(symbol_key, float(base_rr), lo=0.0, hi=10.0)
            if symbol_override_rr <= 0:
                symbol_override_rr = None

        rr = float(base_rr)
        sources = [base_source]
        if profile_rr is not None:
            rr = min(rr, float(profile_rr))
            sources.append("symbol_profile.scalp_tp_r")
        if symbol_override_rr is not None:
            rr = min(rr, float(symbol_override_rr))
            sources.append(symbol_key)

        market_key = (
            "MT5_SCALP_TP_R_INDICES" if market in {"index_cfd", "index_cfd_eu"}
            else "MT5_SCALP_TP_R_METALS" if market in {"metal", "metals"}
            else "MT5_SCALP_TP_R_FOREX" if market == "forex"
            else "MT5_SCALP_TP_R"
        )
        group_override = os.getenv(market_key)
        ignored_group_override = bool(group_override is not None and (configured_value > 0 or profile_rr is not None or symbol_override_rr is not None))
        if configured_value <= 0 and profile_rr is None and symbol_override_rr is None:
            legacy_rr = self._scalp_r_for_symbol("MT5_SCALP_TP_R", canonical, market, float(base_rr))
            if legacy_rr > 0:
                rr = min(rr, float(legacy_rr))
                if abs(float(legacy_rr) - float(base_rr)) > 1e-12:
                    sources.append("legacy_market_override")

        source = ";".join(sources)
        meta = {
            "configured_rr": configured_value or None,
            "base_rr": round(float(base_rr), 6),
            "profile_rr": round(float(profile_rr), 6) if profile_rr is not None else None,
            "symbol_override_rr": round(float(symbol_override_rr), 6) if symbol_override_rr is not None else None,
            "group_override_key": market_key if group_override is not None else None,
            "group_override_ignored": ignored_group_override,
            "effective_rr": round(float(rr), 6),
            "source": source,
            "engine": str(sig.get("engine") or get_engine_for_symbol(canonical) or ""),
        }
        sig.setdefault("_mt5_thresholds", {})["rr_resolution"] = meta
        sig["rr_used"] = float(rr)
        sig["rr_source"] = source
        sig.setdefault("conditions", {})["MT5 scalp TP R"] = float(rr)
        return float(rr)

    def _management_rr_for_position(self, canonical: str, market: str, direction: str, meta: dict | None = None) -> float:
        risk_meta = (meta or {}).get("risk_meta") if isinstance(meta, dict) else None
        broker_geometry = risk_meta.get("broker_geometry") if isinstance(risk_meta, dict) else None
        if isinstance(broker_geometry, dict):
            intended_rr = float(broker_geometry.get("intended_rr") or 0.0)
            if intended_rr > 0:
                return intended_rr
        rr_sig = {
            "engine": get_engine_for_symbol(canonical),
            "mode": str((meta or {}).get("sig_mode") or "mt5"),
            "direction": direction,
        }
        trade_audit = (meta or {}).get("trade_audit") if isinstance(meta, dict) else None
        if isinstance(trade_audit, dict):
            rr_sig.update(trade_audit)
            rr_sig["replacement_strategy_v1"] = bool(
                trade_audit.get("replacement_strategy_v1") or trade_audit.get("ruleset_version") == "strategy_architecture_v1"
            )
        return self._entry_rr_for_symbol(canonical, market, rr_sig)

    def _is_priority_symbol(self, sym: str) -> bool:
        return self._canonical_from_resolved(sym).upper() in self.priority_symbols

    def _entry_window_open(self, market: str) -> bool:
        return in_trade_window(market)

    def _record_index_market_window_closed_scan(self, sym: str, group: str, market: str) -> None:
        canonical = canonical_symbol(self._canonical_from_resolved(sym)).upper()
        engine = get_engine_for_symbol(canonical)
        if engine != INDEX_ENGINE:
            return
        reason = "market_window_closed"
        trace_stub = {
            "decision_trace_id": self._new_decision_trace_id(canonical),
            "_decision_trace": [],
            "generated_at": datetime.utcnow().isoformat(),
            "source_loop_name": "full_scan",
            "ruleset_version": RULESET_VERSION,
            "build_id": BUILD_ID,
            "scanner_process_started_at": self.scanner_process_started_at,
            "state": NO_SETUP,
            "final_status": NO_SETUP,
            "direction_source": f"{engine}_SESSION_OBSERVABILITY",
            "evaluate_probability_master_gate": False,
            "row_source": "current",
            "engine": engine,
            "asset_class": "index",
            "conditions": {"group": group, "market": market, "session_open": False, "reason": reason},
        }
        _ss.upsert_signal(canonical, market, reason, None, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, NO_SETUP, reason, False, trace_stub)
        self._record_scan_gate(canonical, market, reason, False, False, NO_SETUP)
        self.analytics_engine.record(NO_SETUP, canonical, engine, "", 0.0, reason, trace_stub)
        print(f"[MT5_ENGINE_SCAN] ruleset_version={RULESET_VERSION} symbol={canonical} engine={engine} state={NO_SETUP} reason={reason} direction_source={trace_stub.get('direction_source')} evaluate_probability_master_gate=false pending_created=0", flush=True)

    def _two_engine_enabled(self) -> bool:
        return _env_bool("MT5_TWO_ENGINE_ENABLE", True)

    def _setup_timeframe(self) -> str:
        if self._two_engine_enabled():
            return str(os.getenv("MT5_SETUP_TIMEFRAME", "M5") or "M5").strip().upper()
        return "M15"

    def _new_decision_trace_id(self, sym: str) -> str:
        canonical = self._canonical_from_resolved(sym).upper()
        return f"dt_{canonical}_{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}"

    def _trace_step(self, sig: dict | None, step: str, status: str = "PASS", reason: str = "", **fields) -> None:
        if sig is None:
            return
        trace = sig.setdefault("_decision_trace", [])
        entry = {
            "order": len(trace) + 1,
            "ts": datetime.utcnow().isoformat(),
            "step": str(step),
            "status": str(status),
        }
        if reason:
            entry["reason"] = str(reason)
        for key, value in fields.items():
            if value is not None:
                entry[key] = self._audit_json_safe(value)
        trace.append(entry)
        sig["last_trace_step"] = str(step)

    def _infer_block_step(self, gate: str) -> str:
        text = str(gate or "").lower()
        if "strategy engine" in text or "score below min_score" in text:
            return "SCORE_CALCULATED"
        if "adx" in text:
            return "ADX_LOGIC_APPLIED"
        if "spread" in text or "tick" in text:
            return "SPREAD_GATE_APPLIED"
        if "cost" in text:
            return "COST_GATE_APPLIED"
        if "portfolio" in text or "daily loss" in text or "max open" in text or "loss limit" in text:
            return "PORTFOLIO_RISK_GATE_APPLIED"
        if "pending setup created" in text or "1m confirmation" in text:
            return "ONE_MIN_CONFIRMATION_WAITING"
        if "order rejected" in text or "execution" in text or "duplicate" in text or "fresh pending" in text:
            return "ORDER_EXECUTION_CHECK"
        if "quality" in text or "strategy blocked" in text or "mean reversion" in text:
            return "QUALITY_GATE_APPLIED"
        if "news" in text:
            return "NEWS_GATE_APPLIED"
        if "symbol" in text:
            return "SYMBOL_RISK_GATE_APPLIED"
        return "CONFIG_LOADED"

    def _mark_blocked(self, sig: dict | None, step: str, reason: str) -> None:
        if sig is None:
            return
        if self._is_quality_filter_reason(reason, sig):
            self._mark_not_qualified(sig, reason, step)
            return
        pending = str(reason or "").lower().startswith("pending setup created")
        status = "PENDING_SOFT" if pending and bool(sig.get("index_soft_allow")) else ("PENDING" if pending else "BLOCKED")
        sig["final_status"] = status
        sig["block_reason"] = str(reason or "")
        if pending:
            sig["blocked_at_step"] = ""
            self._trace_step(sig, "PENDING_SETUP_CREATED", "PENDING", reason)
            self._trace_step(sig, "ONE_MIN_CONFIRMATION_WAITING", "PENDING", reason)
        else:
            sig["blocked_at_step"] = str(step)
            self._trace_step(sig, step, "BLOCKED", reason)

    def _is_quality_filter_reason(self, reason: str, sig: dict | None = None) -> bool:
        text = str(reason or "").lower()
        if isinstance(sig, dict) and bool(sig.get("_force_quality_filter")):
            return True
        if "cost-to-target" in text and isinstance(sig, dict) and bool(sig.get("_cost_extreme_hard")):
            return False
        quality_tokens = (
            "htf", "h4 ", "h1 ", "m15", "countertrend", "mean reversion",
            "1m confirmation", "confirmation", "stale", "exhaust",
            "adx danger", "score below", "strategy not", "strategy blocked",
            "profile_gate", "profile ", "engine policy", "engine context",
            "cost-to-target", "daily trade count", "symbol daily trade",
            "symbol daily loss", "cooldown", "trade lock", "fresh setup",
            "pyramid", "signal timestamp", "stale signal", "rr mismatch",
            "missing strategy", "symbol disabled", "symbol health", "no pending setup",
            "order_sent setup locked",
        )
        return any(token in text for token in quality_tokens)

    def _mark_not_qualified(self, sig: dict | None, reason: str, step: str = "QUALITY_FILTER") -> None:
        if not isinstance(sig, dict):
            return
        safe_reason = str(reason or "quality filter rejected candidate")
        sig["not_qualified"] = True
        sig["final_status"] = "NOT_QUALIFIED"
        sig["state"] = "NOT_QUALIFIED"
        sig["block_reason"] = safe_reason
        sig["blocked_at_step"] = ""
        sig["quality_filter_step"] = str(step)
        self._trace_step(sig, "NOT_QUALIFIED", "NOT_QUALIFIED", safe_reason, quality_filter_step=step)
        self._audit_decision({
            "event": "NOT_QUALIFIED",
            "decision": "not_qualified",
            "scope": "setup" if isinstance(sig.get("pending_setup"), dict) else "candidate",
            "symbol": sig.get("symbol"),
            "side": sig.get("direction"),
            "setup_id": sig.get("setup_id"),
            "reason": safe_reason,
            "quality_filter_step": step,
        })

    def _attach_engine_route_contract(self, sym: str, market: str, sig: dict) -> None:
        if not self._two_engine_enabled() or _mt5_strategy_engines is None:
            return
        canonical = self._canonical_from_resolved(sym).upper()
        route = _mt5_strategy_engines.route_symbol(canonical, market)
        if not route.allowed:
            return
        contract = _mt5_strategy_engines.engine_contract_for(route.asset_class)
        if not contract:
            return
        cfg = _mt5_strategy_engines.get_symbol_config(canonical) or {}
        has_direction = bool(sig.get("direction"))
        sig.setdefault("asset_class", route.asset_class)
        sig.setdefault("engine", route.engine)
        sig.setdefault("strategy_before", _mt5_strategy_engines.current_strategy_name(sig) if has_direction else "NO_SETUP")
        sig.setdefault("strategy_after", str(cfg.get("strategy") or sig.get("strategy_before") or "") if has_direction else "")
        sig.setdefault("score_function", contract.get("score_function"))
        sig.setdefault("score_function_source", contract.get("score_function_source"))
        sig.setdefault("adx_function", contract.get("adx_function"))
        sig.setdefault("spread_function", contract.get("spread_function"))
        _mt5_strategy_engines.assert_engine_isolation(
            canonical,
            str(sig.get("asset_class") or ""),
            str(sig.get("engine") or ""),
            str(sig.get("score_function") or ""),
            str(sig.get("adx_function") or ""),
            str(sig.get("spread_function") or ""),
        )
        sig.setdefault("_mt5_thresholds", {})["strategy_engine_route"] = {
            "source": "mt5_strategy_engines.route_symbol",
            "symbol": canonical,
            "asset_class": sig.get("asset_class"),
            "engine": sig.get("engine"),
            "score_function": sig.get("score_function"),
            "score_function_source": sig.get("score_function_source"),
            "adx_function": sig.get("adx_function"),
            "spread_function": sig.get("spread_function"),
            "strategy_after": sig.get("strategy_after"),
            "strategy_status": "pending_engine_score" if has_direction else "no_setup",
        }

    def _assert_signal_engine_contract(self, sym: str, sig: dict) -> None:
        if isinstance(sig, dict) and (sig.get("ruleset_version") == RULESET_VERSION or sig.get("score_function_source") == "mt5_bot.py"):
            return
        if not self._two_engine_enabled() or _mt5_strategy_engines is None or not isinstance(sig, dict):
            return
        engine = str(sig.get("engine") or "")
        asset_class = str(sig.get("asset_class") or "")
        if engine not in {
            _mt5_strategy_engines.FOREX_ENGINE,
            _mt5_strategy_engines.INDEX_ENGINE,
            _mt5_strategy_engines.METALS_ENGINE,
        }:
            return
        _mt5_strategy_engines.assert_engine_isolation(
            self._canonical_from_resolved(sym).upper(),
            asset_class,
            engine,
            str(sig.get("score_function") or ""),
            str(sig.get("adx_function") or ""),
            str(sig.get("spread_function") or ""),
        )

    def _shared_htf_setup_permission(self, canonical: str, market: str, cfg: dict, sig: dict, details: dict, h4, h1, m15, m5) -> tuple[bool, str, dict]:
        side = str(sig.get("direction") or "").upper()
        trigger = (sig.get("_mt5_thresholds") or {}).get("engine_trigger") or {}
        trigger_metrics = trigger.get("metrics") if isinstance(trigger.get("metrics"), dict) else {}
        p4, p1, p15, p5 = _pack(h4), _pack(h1), _pack(m15), _pack(m5)
        strategy = str(sig.get("strategy_after") or sig.get("strategy") or cfg.get("strategy") or "")
        configured_setup_type = str(sig.get("setup_type") or cfg.get("setup_type") or "").strip().upper()
        strategy_is_mean_reversion = "mean_reversion" in strategy.lower()
        setup_type = configured_setup_type or ("MEAN_REVERSION" if strategy_is_mean_reversion else "DIRECTIONAL")
        setup_type_source = "explicit_config" if configured_setup_type else ("strategy_name" if strategy_is_mean_reversion else "default_directional")
        valid_sides = {"BUY", "SELL"}

        def alignment_score(pack, requested_side):
            if not pack.get("ready"):
                return 0.0
            bias = str(pack.get("bias") or "").upper()
            if bias == requested_side:
                return 100.0
            if bias in valid_sides:
                return 0.0
            return 50.0

        h4_bias = str(p4.get("bias") or "").upper()
        h1_bias = str(p1.get("bias") or "").upper()
        m15_bias = str(p15.get("bias") or "").upper()
        h4_score = alignment_score(p4, side)
        h1_score_value, h1_score_meta = _directional_context_score(side, p1, "H1")
        m15_score_value, m15_score_meta = _directional_context_score(side, p15, "M15")
        h1_score = float(h1_score_value or 0.0)
        m15_score = float(m15_score_value or 0.0)
        trigger_5m_score = float(details.get("trigger_5m_score") or trigger_metrics.get("trigger_5m_score") or 0.0)
        execution_1m_score = float(details.get("execution_1m_score") or trigger_metrics.get("execution_1m_score") or 0.0)
        trigger_side = str(trigger.get("direction") or side).upper()
        regime = str(sig.get("regime") or trigger.get("regime") or "").upper()
        opposing_biases = [bias for bias in (h4_bias, h1_bias, m15_bias) if bias in valid_sides and bias != side]
        countertrend = bool(opposing_biases)
        bb_position = _bollinger_position(m5)
        sig.update({
            "setup_type": setup_type,
            "setup_type_source": setup_type_source,
            "h4_bias": h4_bias, "h4_score": h4_score,
            "h1_bias": h1_bias, "h1_score": h1_score,
            "m15_bias": m15_bias, "m15_score": m15_score,
            "setup_15m_score": m15_score, "trigger_5m_score": trigger_5m_score,
            "execution_1m_score": execution_1m_score,
            "h4_open": p4.get("open"), "h4_high": p4.get("high"), "h4_low": p4.get("low"), "h4_close": p4.get("close"),
            "h1_open": p1.get("open"), "h1_high": p1.get("high"), "h1_low": p1.get("low"), "h1_close": p1.get("close"),
            "m15_open": p15.get("open"), "m15_high": p15.get("high"), "m15_low": p15.get("low"), "m15_close": p15.get("close"),
            "bb_position_m5": bb_position,
            "h1_score_source": "mt5_bot._directional_context_score",
            "m15_score_source": "mt5_bot._directional_context_score",
            "h1_score_components": h1_score_meta,
            "m15_score_components": m15_score_meta,
        })
        setup_id = str(sig.get("setup_id") or f"candidate_{canonical}_{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}")
        sig["setup_id"] = setup_id
        meta = {
            "symbol": canonical, "engine": str(sig.get("engine") or get_engine_for_symbol(canonical)),
            "setup_id": setup_id, "setup_type": setup_type, "setup_type_source": setup_type_source, "side": side,
            "h4_bias": h4_bias, "h4_score": h4_score, "h1_bias": h1_bias, "h1_score": h1_score,
            "m15_bias": m15_bias, "m15_score": m15_score, "setup_15m_score": m15_score,
            "trigger_5m_score": trigger_5m_score, "execution_1m_score": execution_1m_score,
            "trigger_side": trigger_side, "regime": regime, "adx": float(p5.get("adx") or 0.0),
            "vwap_z": float(p5.get("vwap_z") or 0.0), "rsi": float(p5.get("rsi") or 50.0),
            "bb_position_m5": bb_position, "countertrend": countertrend, "opposing_biases": opposing_biases,
        }
        reasons = []
        if side not in valid_sides:
            reasons.append("invalid setup side")
        if not p4.get("ready"):
            reasons.append("H4 data unavailable")
        missing_htf = []
        if h1_score_value is None:
            missing_htf.append("H1")
        if m15_score_value is None:
            missing_htf.append("M15")
        if missing_htf:
            reasons.append(f"DATA_MISSING_HTF_CONTEXT: {','.join(missing_htf)} score unavailable")
        if trigger_side != side:
            reasons.append(f"5M trigger side {trigger_side or 'MISSING'} does not match setup side {side or 'MISSING'}")

        if countertrend:
            setup_type_allowed = setup_type in {"MEAN_REVERSION", "COUNTERTREND"}
            if not setup_type_allowed:
                reasons.append("countertrend setup is not explicitly tagged MEAN_REVERSION or COUNTERTREND")
            weak_range = regime == "RANGE" or float(p5.get("adx") or 0.0) < 18.0
            if not weak_range:
                reasons.append("countertrend requires RANGE regime or ADX below 18")
            if side == "BUY":
                extreme_ok = float(p5.get("vwap_z") or 0.0) <= -0.80 and float(p5.get("rsi") or 50.0) <= 48.0 and bb_position is not None and bb_position <= 0.20
            else:
                extreme_ok = float(p5.get("vwap_z") or 0.0) >= 0.80 and float(p5.get("rsi") or 50.0) >= 52.0 and bb_position is not None and bb_position >= 0.80
            if not extreme_ok:
                reasons.append("countertrend requires VWAP, RSI, and Bollinger extreme confirmation")
            profile = self._symbol_profile(canonical) or {}
            base_rr = float(cfg.get("tp_r") or 0.0)
            countertrend_rr = profile.get("countertrend_tp_r") or profile.get("scalp_tp_r") or cfg.get("countertrend_tp_r")
            risk_cap = profile.get("countertrend_max_trade_risk_usd") or profile.get("max_trade_risk_usd")
            if countertrend_rr is None or float(countertrend_rr or 0.0) <= 0.0 or (base_rr > 0.0 and float(countertrend_rr) >= base_rr):
                reasons.append("countertrend requires a smaller explicit RR profile")
            if risk_cap is None or float(risk_cap or 0.0) <= 0.0:
                reasons.append("countertrend requires an explicit risk cap profile")
            meta.update({"countertrend_rr": countertrend_rr, "countertrend_risk_cap": risk_cap, "countertrend_profile_checked": True})
        else:
            if not p1.get("ready") or h1_bias != side:
                reasons.append(f"H1 must support side; H1 bias={h1_bias or 'MISSING'} side={side or 'MISSING'}")
            if not p15.get("ready"):
                reasons.append("15M data unavailable")
            elif m15_bias in valid_sides and m15_bias != side:
                reasons.append(f"15M strongly opposes side; 15M bias={m15_bias} side={side}")
            if h1_score < 60.0:
                reasons.append(f"H1 score weak ({h1_score:.1f} < 60)")
            if m15_score <= 0.0 and not bool(cfg.get("allow_htf_score_fallback")):
                reasons.append("15M setup score missing/zero with no explicit fallback")
            if trigger_5m_score >= 80.0 and (h4_score < 50.0 or h1_score < 60.0 or m15_score <= 0.0):
                reasons.append("high 5M trigger cannot override weak/missing HTF scores")

        meta["reasons"] = list(reasons)
        sig["htf_alignment"] = meta
        event = "COUNTERTREND_MEAN_REVERSION_ALLOWED" if countertrend and not reasons else "HTF_ALIGNMENT_PASS" if not reasons else "NOT_QUALIFIED"
        decision = "allow" if not reasons else "block"
        self._audit_decision({"event": event, "decision": decision, **meta})
        self._trace_step(sig, event, "PASS" if not reasons else "BLOCKED", "; ".join(reasons) if reasons else "higher-timeframe alignment passed", **meta)
        return not reasons, "; ".join(reasons) if reasons else "OK", meta

    def _engine_policy_allows_entry(self, sym: str, market: str, sig: dict, details: dict) -> tuple[bool, str]:
        if not self._two_engine_enabled():
            return True, "OK"
        canonical = self._canonical_from_resolved(sym).upper()
        if _mt5_strategy_engines is None:
            reason = "MT5 two-engine module unavailable"
            self._audit_decision({"event": "strategy_engine_gate", "decision": "block", "symbol": canonical, "market": market, "reason": reason})
            self._mark_blocked(sig, "ENGINE_SELECTED", reason)
            return False, reason
        decision = _mt5_strategy_engines.apply_engine_policy(canonical, market, sig, details)
        payload = decision.as_dict()
        contract = _mt5_strategy_engines.engine_contract_for(payload.get("asset_class"))
        if contract:
            payload.update(contract)
            sig["score_function"] = contract.get("score_function")
            sig["score_function_source"] = contract.get("score_function_source")
            sig["adx_function"] = contract.get("adx_function")
            sig["spread_function"] = contract.get("spread_function")
            _mt5_strategy_engines.assert_engine_isolation(
                canonical,
                str(payload.get("asset_class") or ""),
                str(payload.get("engine") or ""),
                str(sig.get("score_function") or ""),
                str(sig.get("adx_function") or ""),
                str(sig.get("spread_function") or ""),
            )
        sig["asset_class"] = payload.get("asset_class")
        sig["engine"] = payload.get("engine")
        sig["strategy_before"] = payload.get("strategy_before")
        sig["strategy_after"] = payload.get("strategy_after")
        sig["score_before"] = payload.get("score_before")
        sig["score_after"] = payload.get("score_after")
        sig["min_score"] = payload.get("min_score")
        sig["adx_reason"] = payload.get("adx_reason")
        engine_trace = payload.get("engine_trace") if isinstance(payload.get("engine_trace"), dict) else {}
        sig["engine_trace"] = engine_trace
        sig["index_soft_allow"] = bool(engine_trace.get("soft_allow_used"))
        if sig.get("index_soft_allow"):
            sig["soft_allow_reason"] = str(engine_trace.get("soft_allow_reason") or decision.reason)
        sig["strategy_family"] = payload.get("strategy_after")
        sig["score"] = float(payload.get("score_after") or sig.get("score") or 0.0)
        sig.setdefault("conditions", {})["MT5 strategy engine"] = payload.get("engine") or "BLOCKED"
        sig.setdefault("conditions", {})["MT5 strategy family"] = payload.get("strategy_after") or "-"
        sig.setdefault("conditions", {})["MT5 score before/after"] = f"{payload.get('score_before')}/{payload.get('score_after')}"
        if sig.get("index_soft_allow"):
            sig.setdefault("conditions", {})["MT5 index soft allow"] = sig.get("soft_allow_reason")
        sig.setdefault("_mt5_thresholds", {})["strategy_engine"] = payload
        self._trace_step(sig, "ENGINE_SELECTED", "PASS", "engine selected", source="mt5_strategy_engines.apply_engine_policy", engine=payload.get("engine"), asset_class=payload.get("asset_class"))
        self._trace_step(sig, "SCORE_CALCULATED", "PASS", "score routed through engine contract", score_function=sig.get("score_function"), score_source=sig.get("score_function_source"), score_before=payload.get("score_before"), score_after=payload.get("score_after"), min_score=payload.get("min_score"))
        adx_status = "BLOCKED" if (not decision.allowed and "adx" in str(decision.reason or "").lower()) else "PASS"
        self._trace_step(sig, "ADX_LOGIC_APPLIED", adx_status, payload.get("adx_reason") or decision.reason, adx_function=sig.get("adx_function"))
        self._audit_decision({
            "event": "strategy_engine_gate",
            "decision": "allow" if decision.allowed else "block",
            "status": payload.get("status"),
            **payload,
        })
        if not decision.allowed:
            step = "ADX_LOGIC_APPLIED" if "adx" in str(decision.reason or "").lower() else "SCORE_CALCULATED"
            self._mark_blocked(sig, step, decision.reason)
            _ss.set_status("last_strategy_engine_gate", f"BLOCK {canonical}: {decision.reason}")
            return False, decision.reason
        _ss.set_status("last_strategy_engine_gate", f"ALLOW {canonical}: {payload.get('engine')} {payload.get('strategy_after')}")
        return True, "OK"

    def create_pending_setup(self, symbol: str, side: str, engine, strategy=None, score=None, trace: dict | None = None) -> tuple[bool, str]:
        # Supports the old positional order too: (symbol, side, score, engine, strategy, trace).
        if not isinstance(engine, str):
            old_score = float(engine or 0.0)
            old_engine = str(strategy or "")
            old_strategy = str(score or "")
            engine, strategy, score = old_engine, old_strategy, old_score
        canonical = canonical_symbol(self._canonical_from_resolved(symbol)).upper()
        sig = trace if isinstance(trace, dict) else {}
        direction = str(side or sig.get("direction") or "").upper()
        now = datetime.utcnow()
        self.cleanup_expired_pending_setups(now, publish_debug=False)
        pending_key, pending = self._active_pending_setup_for(canonical, direction, now)
        if isinstance(pending, dict):
            final_status = str(pending.get("state") or sig.get("state") or PENDING)
            reason = "SETUP_ALREADY_ACTIVE waiting for 1M confirmation"
            if isinstance(sig, dict):
                sig["pending_setup_created"] = False
                sig["pending_setup"] = pending
                sig["pending_key"] = pending_key
                sig["setup_id"] = pending.get("setup_id")
                sig["one_min_confirmation"] = False
                sig["execution_1m_result"] = "pending"
                sig["final_status"] = final_status
                sig["state"] = final_status
                sig["pending_reason"] = reason
                sig["block_reason"] = ""
                sig["blocked_at_step"] = ""
            self._log_pending_setup_event("SETUP_ALREADY_ACTIVE", pending_key, pending, reason, sig)
            self.analytics_engine.record(final_status, canonical, str(pending.get("engine") or engine or ""), str(pending.get("strategy") or strategy or ""), float(pending.get("score") or score or 0.0), reason, sig)
            return False, reason
        cfg = get_symbol_config(canonical) or {}
        market = str(sig.get("market") or self._market_for_symbol(canonical))
        ttl_seconds = self._pending_setup_ttl_seconds(canonical, market, str(engine or sig.get("engine") or get_engine_for_symbol(canonical)))
        final_status = PENDING_SOFT if bool(sig.get("soft_allow_used") or sig.get("index_soft_allow")) else PENDING
        setup_id = f"setup_{canonical}_{direction}_{now.strftime('%Y%m%d%H%M%S%f')}"
        setup = {
            "setup_id": setup_id,
            "pending_key": pending_key,
            "symbol": canonical,
            "side": direction,
            "market": market,
            "engine": str(engine or sig.get("engine") or get_engine_for_symbol(canonical)),
            "asset_class": str(cfg.get("asset_class") or sig.get("asset_class") or ""),
            "direction": direction,
            "strategy_before": str(sig.get("strategy_before") or strategy or ""),
            "strategy_after": str(sig.get("strategy_after") or strategy or ""),
            "strategy": str(strategy or sig.get("strategy_after") or sig.get("strategy") or ""),
            "score_before": float(sig.get("score_before") or score or 0.0),
            "score_after": float(sig.get("score_after") or score or 0.0),
            "score": float(score or sig.get("score") or 0.0),
            "min_score": float(sig.get("min_score") or cfg.get("min_score") or 0.0),
            "pending_floor": float(sig.get("pending_floor") or pending_floor_for_symbol(canonical, float(sig.get("min_score") or cfg.get("min_score") or 0.0), str(engine or sig.get("engine") or get_engine_for_symbol(canonical)))),
            "require_stronger_1m_confirmation": bool(sig.get("require_stronger_1m_confirmation") or sig.get("soft_allow_used") or sig.get("index_soft_allow")),
            "adx": float(sig.get("adx") or 0.0),
            "adx_reason": str(sig.get("adx_reason") or ""),
            "score_function": str(sig.get("score_function") or ""),
            "adx_function": str(sig.get("adx_function") or ""),
            "spread_function": str(sig.get("spread_function") or ""),
            "price": float(sig.get("price") or 0.0),
            "atr": float(sig.get("atr") or 0.0),
            "timeout_minutes": ttl_seconds / 60.0,
            "ttl_seconds": ttl_seconds,
            "created_at": now.isoformat(),
            "setup_created_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=ttl_seconds)).isoformat(),
            "status": "waiting_1m",
            "trigger_timeframe": "5M",
            "waiting_for": "1M_CONFIRMATION",
            "trigger_candle_id": str(sig.get("m5_trigger_candle_id") or ""),
            "baseline_m1_candle_id": str(sig.get("baseline_m1_candle_id") or ""),
            "baseline_m1_closed_at": str(sig.get("baseline_m1_closed_at") or ""),
            "last_m1_candle_id": str(sig.get("baseline_m1_candle_id") or ""),
            "last_m1_candle_closed_at": str(sig.get("baseline_m1_closed_at") or ""),
            "confirmation_requires_post_setup_candle": True,
            "setup_type": str(sig.get("setup_type") or "DIRECTIONAL"),
            "htf_alignment": self._audit_json_safe(sig.get("htf_alignment") or {}),
            "trigger_candle_m5": self._pending_setup_snapshot(sig, "m5"),
            "context_h4": self._pending_setup_snapshot(sig, "h4"),
            "context_m15": self._pending_setup_snapshot(sig, "m15"),
            "context_h1": self._pending_setup_snapshot(sig, "h1"),
            "trace": self._audit_json_safe(sig.get("_decision_trace") or sig.get("trace") or []),
            "decision_trace_id": str(sig.get("decision_trace_id") or ""),
            "ruleset_version": RULESET_VERSION,
            "build_id": BUILD_ID,
            "engine_context": self._audit_json_safe({
                "engine": str(engine or sig.get("engine") or get_engine_for_symbol(canonical)),
                "asset_class": str(cfg.get("asset_class") or sig.get("asset_class") or ""),
                "strategy_before": str(sig.get("strategy_before") or strategy or ""),
                "strategy_after": str(sig.get("strategy_after") or strategy or ""),
                "score_before": sig.get("score_before"),
                "score_after": sig.get("score_after"),
                "score": sig.get("score"),
                "min_score": sig.get("min_score") or cfg.get("min_score"),
                "pending_floor": sig.get("pending_floor"),
                "adx": sig.get("adx"),
                "adx_reason": sig.get("adx_reason"),
                "regime": sig.get("regime"),
                "setup_type": sig.get("setup_type"),
                "h4_bias": sig.get("h4_bias"),
                "h4_score": sig.get("h4_score"),
                "h1_bias": sig.get("h1_bias"),
                "h1_score": sig.get("h1_score"),
                "m15_bias": sig.get("m15_bias"),
                "m15_score": sig.get("m15_score"),
                "m15_structure": sig.get("m15_structure"),
                "htf_alignment": sig.get("htf_alignment"),
                "session_score": sig.get("session_score"),
                "volatility_expansion": sig.get("volatility_expansion"),
                "atr_expansion": sig.get("atr_expansion"),
                "direction_source": sig.get("direction_source"),
                "score_function": sig.get("score_function"),
                "adx_function": sig.get("adx_function"),
                "spread_function": sig.get("spread_function"),
                "replacement_strategy_v1": bool(sig.get("replacement_strategy_v1")),
            }),
            "state": final_status,
            "replacement_strategy_v1": bool(sig.get("replacement_strategy_v1")),
        }
        self.pending_setups[pending_key] = setup
        self._save_state()
        if isinstance(sig, dict):
            sig["pending_setup_created"] = True
            sig["pending_setup"] = setup
            sig["pending_key"] = pending_key
            sig["setup_id"] = setup_id
            sig["one_min_confirmation"] = False
            sig["execution_1m_result"] = "pending"
            sig["final_status"] = final_status
            sig["state"] = final_status
            sig["pending_reason"] = "SETUP_CREATED 5M setup created; waiting for 1M confirmation"
            sig["block_reason"] = ""
            sig["blocked_at_step"] = ""
            self._trace_step(sig, "CREATE_PENDING_SETUP_CALLED", final_status, "SETUP_CREATED 5M setup created; waiting for 1M confirmation", symbol=canonical, direction=setup["direction"], score=setup["score"], engine=setup["engine"], strategy=setup["strategy"], setup_id=setup_id, pending_key=pending_key, expires_at=setup["expires_at"])
            self._trace_step(sig, "SETUP_CREATED", final_status, "5M setup created; waiting for 1M confirmation", setup_id=setup_id, pending_key=pending_key)
            self._trace_step(sig, "PENDING_SETUP_CREATED", final_status, "5M setup created; waiting for 1M confirmation")
            self._trace_step(sig, "ONE_MIN_CONFIRMATION_WAITING", final_status, "waiting for pending monitor")
        self._log_pending_setup_event("SETUP_CREATED", pending_key, setup, "5M setup created; waiting for 1M confirmation", sig)
        self.analytics_engine.record(final_status, canonical, setup["engine"], setup["strategy"], setup["score"], "SETUP_CREATED 5M setup created; waiting for 1M confirmation", sig)
        self._audit_decision({"event": "CREATE_PENDING_SETUP_CALLED", "status": final_status, "symbol": canonical, "direction": setup["direction"], "score": setup["score"], "engine": setup["engine"], "strategy": setup["strategy"], "setup_id": setup_id, "pending_key": pending_key, "expires_at": setup["expires_at"], "ruleset_version": RULESET_VERSION, "setup": setup})
        _ss.set_status("last_pending_setup", f"{final_status} {pending_key}: {setup['engine']} expires={setup['expires_at']}")
        self._publish_pending_setup_debug(now)
        return True, "SETUP_CREATED 5M setup created; waiting for 1M confirmation"

    def _index_exhaustion_allows_entry(self, sym: str, market: str, sig: dict) -> tuple[bool, str, dict]:
        canonical = canonical_symbol(self._canonical_from_resolved(sym)).upper()
        cfg = get_symbol_config(canonical) or {}
        is_index = (
            str(market or "").lower() in {"index_cfd", "index_cfd_eu"}
            or str(cfg.get("asset_class") or "").lower() == "index"
            or str(sig.get("engine") or get_engine_for_symbol(canonical)) == INDEX_ENGINE
        )
        if not is_index:
            return True, "not an index symbol", {}
        if sig.get("_index_exhaustion_checked"):
            return bool(sig.get("_index_exhaustion_allowed", True)), str(sig.get("_index_exhaustion_reason") or "cached"), dict(sig.get("_index_exhaustion_meta") or {})
        side = str(sig.get("direction") or "").upper()
        if side not in {"BUY", "SELL"}:
            return False, "invalid side for index exhaustion guard", {"side": side}
        try:
            df5 = self.gateway.rates(canonical, "M5", 40)
            bars = _bars(df5)
            if len(bars) < 25:
                result = (True, "index exhaustion data insufficient", {"bars": len(bars)})
            else:
                opens = _col(bars, "Open")
                highs = _col(bars, "High")
                lows = _col(bars, "Low")
                closes = _col(bars, "Close")
                current_open = float(opens.iloc[-1])
                current_close = float(closes.iloc[-1])
                current_body = abs(current_close - current_open)
                recent_bodies = (closes - opens).abs().iloc[:-1].tail(12)
                average_body = max(float(recent_bodies.mean() or 0.0), 1e-12)
                body_ratio = current_body / average_body
                atr = max(float(_last(_atr_series(bars, 14)) or 0.0), 1e-12)
                ema20 = float(_last(_ema(closes, 20)) or current_close)
                vwap = float(_last(_vwap(bars)) or current_close)
                mid = closes.rolling(20).mean()
                std = closes.rolling(20).std(ddof=0)
                upper = float((mid + (2.0 * std)).iloc[-1])
                lower = float((mid - (2.0 * std)).iloc[-1])
                band_width = max(upper - lower, 1e-12)
                entry = float(sig.get("price") or current_close)
                close_location = (entry - lower) / band_width
                anchor_up = max((entry - vwap) / atr, (entry - ema20) / atr)
                anchor_down = min((entry - vwap) / atr, (entry - ema20) / atr)
                prior_close = float(closes.iloc[-4]) if len(closes) >= 4 else float(closes.iloc[0])
                vertical_move = abs(current_close - prior_close) / atr
                body_spike = body_ratio > 1.5
                vertical_spike = vertical_move >= 1.0 and current_close > current_open
                vertical_dump = vertical_move >= 1.0 and current_close < current_open
                band_edge_buy = entry >= upper or close_location >= 0.75
                band_edge_sell = entry <= lower or close_location <= 0.25
                far_above = anchor_up >= 1.0
                far_below = anchor_down <= -1.0
                exhausted = (
                    (side == "BUY" and far_above and band_edge_buy and body_spike and vertical_spike)
                    or (side == "SELL" and far_below and band_edge_sell and body_spike and vertical_dump)
                )
                meta = {
                    "side": side, "entry": entry, "vwap": vwap, "ema20": ema20,
                    "upper_band": upper, "lower_band": lower, "close_location": close_location,
                    "anchor_up_atr": anchor_up, "anchor_down_atr": anchor_down,
                    "body": current_body, "average_body": average_body, "body_ratio": body_ratio,
                    "vertical_move_atr": vertical_move, "far_above": far_above, "far_below": far_below,
                    "band_edge_buy": band_edge_buy, "band_edge_sell": band_edge_sell,
                    "body_spike": body_spike, "vertical_spike": vertical_spike, "vertical_dump": vertical_dump,
                }
                result = (not exhausted, "index trigger is extended/exhausted" if exhausted else "index exhaustion guard clear", meta)
        except Exception as exc:
            result = (True, f"index exhaustion guard unavailable: {str(exc)[:120]}", {"error": str(exc)[:180]})
        allowed, reason, meta = result
        sig["_index_exhaustion_checked"] = True
        sig["_index_exhaustion_allowed"] = bool(allowed)
        sig["_index_exhaustion_reason"] = reason
        sig["_index_exhaustion_meta"] = meta
        if not allowed:
            self._audit_decision({
                "event": "NOT_QUALIFIED", "decision": "not_qualified", "scope": "setup",
                "symbol": canonical, "direction": side, "setup_id": sig.get("setup_id"),
                "pending_key": sig.get("pending_key"), "reason": reason, "meta": self._audit_json_safe(meta),
            })
            _ss.set_status("last_index_exhaustion_block", f"{canonical} {side}: INDEX_EXHAUSTION_BLOCKED")
        return allowed, reason, meta

    def _setup_stale_entry_guard(self, canonical: str, market: str, pending: dict, sig: dict, enforce_fresh_age: bool = True) -> tuple[bool, str, dict]:
        now = datetime.utcnow()
        age_seconds = self._pending_setup_age_seconds(pending, now)
        ttl_seconds = self._pending_setup_ttl_seconds(canonical, market, pending.get("engine") or sig.get("engine") or "")
        # The setup TTL is the execution window. A separate 60s/half-TTL
        # cutoff caused valid pending setups to be cancelled before their
        # configured 90s/120s window elapsed.
        fresh_limit = ttl_seconds
        if enforce_fresh_age and age_seconds > fresh_limit:
            return False, f"setup age {age_seconds:.1f}s exceeds fresh confirmation window {fresh_limit:.1f}s", {"kind": "age", "setup_age_seconds": age_seconds, "fresh_limit_seconds": fresh_limit, "ttl_seconds": ttl_seconds}
        try:
            _resolved, tick = self.gateway.symbol_tick(canonical)
            bid = float(getattr(tick, "bid", 0.0) or 0.0)
            ask = float(getattr(tick, "ask", 0.0) or 0.0)
        except Exception:
            bid = ask = 0.0
        try:
            bars = _bars(self.gateway.rates(canonical, "M5", 40))
            p = _pack(bars)
        except Exception as exc:
            return True, f"stale guard data unavailable: {str(exc)[:120]}", {"kind": "unavailable", "error": str(exc)[:180]}
        side = str(sig.get("direction") or pending.get("direction") or "").upper()
        trigger_price = float(pending.get("price") or sig.get("price") or 0.0)
        current_price = ask if side == "BUY" and ask > 0 else bid if side == "SELL" and bid > 0 else float(p.get("close") or trigger_price)
        atr = max(float(pending.get("atr") or p.get("atr") or 0.0), 1e-12)
        move_atr = ((current_price - trigger_price) / atr) if side == "BUY" else ((trigger_price - current_price) / atr)
        stale_move_limit = _env_float("MT5_SETUP_STALE_MOVE_ATR", 0.75, lo=0.25, hi=3.0)
        if bool((sig or {}).get("replacement_strategy_v1")):
            # V1 uses M5 pullback/reclaim entries. A 0.75 ATR cap cancelled
            # fresh setups before the 90s/120s TTL when the reclaim moved.
            stale_move_limit = _env_float("MT5_STRATEGY_V1_STALE_MOVE_ATR", 1.25, lo=0.75, hi=3.0)
        vwap = float(p.get("vwap") or current_price)
        ema20 = float(p.get("ema20") or current_price)
        close = float(p.get("close") or current_price)
        close_series = _col(bars, "Close")
        mid = close_series.rolling(20).mean()
        std = close_series.rolling(20).std(ddof=0)
        upper = float((mid + 2.0 * std).iloc[-1]) if len(bars) >= 20 else close
        lower = float((mid - 2.0 * std).iloc[-1]) if len(bars) >= 20 else close
        vwap_atr = (current_price - vwap) / atr
        ema_atr = (current_price - ema20) / atr
        buy_extended = side == "BUY" and (current_price >= upper or (vwap_atr >= 0.75 and ema_atr >= 0.75))
        sell_extended = side == "SELL" and (current_price <= lower or (vwap_atr <= -0.75 and ema_atr <= -0.75))
        meta = {"kind": "price", "side": side, "setup_age_seconds": age_seconds, "ttl_seconds": ttl_seconds, "trigger_price": trigger_price, "current_price": current_price, "move_atr": move_atr, "stale_move_limit_atr": stale_move_limit, "vwap": vwap, "ema20": ema20, "upper_band": upper, "lower_band": lower, "vwap_atr": vwap_atr, "ema20_atr": ema_atr, "buy_extended": buy_extended, "sell_extended": sell_extended}
        if move_atr > stale_move_limit:
            return False, f"price moved {move_atr:.2f}x ATR from 5M trigger ({stale_move_limit:.2f}x limit)", meta
        if (buy_extended or sell_extended) and move_atr > stale_move_limit:
            return False, f"price extended beyond VWAP/EMA20/Bollinger for {side} after {move_atr:.2f}x ATR move", meta
        return True, "setup remains fresh and price is within entry window", meta

    def _pending_setup_allows_execution(self, sym: str, market: str, sig: dict, details: dict, pending_key: str = "", pending_setup: dict | None = None) -> tuple[bool, str]:
        canonical = canonical_symbol(self._canonical_from_resolved(sym)).upper()
        if _env_bool("MT5_ALLOW_DIRECT_5M_ENTRY", ALLOW_DIRECT_5M_ENTRY) and not _env_bool("MT5_REQUIRE_1M_CONFIRMATION", REQUIRE_1M_CONFIRMATION):
            _ss.set_status("direct_5m_entry_disabled", "1M confirmation required by multi-engine ruleset")
        direction_hint = str(sig.get("direction") or "").upper()
        pending_key = pending_key or self._pending_setup_key(canonical, direction_hint)
        pending = pending_setup if isinstance(pending_setup, dict) else self.pending_setups.get(pending_key)
        if not isinstance(pending, dict):
            return False, "no pending setup awaiting 1M confirmation"
        now = datetime.utcnow()
        status = self._pending_setup_status(pending)
        if status == "order_sent":
            return False, "ORDER_SENT setup locked; waiting for MT5 order result"
        if self._pending_setup_is_expired(pending, now):
            reason = "NOT_QUALIFIED pending setup TTL expired before 1M confirmation"
            self._mark_not_qualified(sig, reason, "PENDING_TTL")
            self.analytics_engine.record("NOT_QUALIFIED", canonical, str(pending.get("engine") or ""), str(pending.get("strategy") or ""), float(pending.get("score") or 0.0), reason, sig)
            self._remove_pending_setup(pending_key, pending, "expired", "NOT_QUALIFIED", reason, sig)
            return False, reason
        age_min = self._pending_setup_age_seconds(pending, now) / 60.0
        direction = str(pending.get("direction") or sig.get("direction") or "").upper()
        sig["pending_key"] = pending_key
        sig["setup_id"] = pending.get("setup_id")
        sig["pending_setup_status"] = status
        sig["pending_setup"] = pending
        stale_interval = _env_float("MT5_PENDING_STALE_GUARD_SECONDS", 2.0, lo=0.25, hi=10.0)
        stale_checked_at = float(pending.get("_stale_guard_checked_monotonic") or 0.0)
        stale_due = not stale_checked_at or (time.monotonic() - stale_checked_at) >= stale_interval
        if stale_due:
            pending["_stale_guard_checked_monotonic"] = time.monotonic()
            stale_ok, stale_reason, stale_meta = self._setup_stale_entry_guard(canonical, market, pending, sig, enforce_fresh_age=False)
        else:
            stale_ok, stale_reason, stale_meta = True, "stale guard cached until next interval", {"kind": "cached", "interval_seconds": stale_interval}
        if not stale_ok:
            # Keep the M1 confirmation opportunity alive; the final fresh-entry guard
            # below still blocks an actually stale confirmed entry.
            sig["pre_confirmation_stale_warning"] = stale_reason
            sig["pre_confirmation_stale_meta"] = stale_meta
            self._audit_decision({
                "event": "SETUP_STALE_WARNING_BEFORE_1M",
                "decision": "observe",
                "scope": "setup",
                "symbol": canonical,
                "side": direction,
                "setup_id": pending.get("setup_id"),
                "reason": stale_reason,
                "meta": self._audit_json_safe(stale_meta),
            })
        try:
            # Keep the raw bridge frame here. _bars() removes the still-forming
            # candle exactly once inside the timestamp and confirmation helpers.
            # Dropping it here as well evaluated the candle two positions back.
            df1 = self.gateway.rates(canonical, "M1", 41)
            m1_closed_at, m1_candle_id = _completed_m1_candle_id(df1)
            sig["m1_candle_closed_at"] = m1_closed_at
            setup_created_dt = _parse_dt(pending.get("setup_created_at") or pending.get("created_at"))
            candle_closed_dt = _parse_dt(m1_closed_at)
            same_candle = bool(m1_candle_id) and str(pending.get("last_m1_candle_id") or "") == m1_candle_id
            candle_predates_setup = not candle_closed_dt or not setup_created_dt or candle_closed_dt <= setup_created_dt
            if not m1_candle_id or same_candle:
                ok, confirm_reason, confirm_score = False, "waiting for new completed 1M candle", 0.0
            elif candle_predates_setup:
                pending["last_m1_candle_id"] = m1_candle_id
                pending["last_m1_candle_closed_at"] = m1_closed_at
                self._save_state()
                ok, confirm_reason, confirm_score = False, "waiting for first completed post-setup 1M candle", 0.0
            else:
                pending["last_m1_candle_id"] = m1_candle_id
                pending["last_m1_candle_closed_at"] = m1_closed_at
                try:
                    _ss.upsert_m1_candles(canonical, df1.tail(40), retention_days=7)
                except Exception as exc:
                    _ss.set_status("m1_candle_persist_error", f"{canonical}: {str(exc)[:180]}")
                ok, confirm_reason, confirm_score = valid_1m_confirmation(df1, direction, str(pending.get("engine") or sig.get("engine") or ""))
                self._save_state()
        except Exception as exc:
            ok, confirm_reason, confirm_score = False, f"waiting for 1M confirmation: {str(exc)[:120]}", 0.0
        sig["one_min_confirmation"] = bool(ok)
        sig["execution_1m_result"] = confirm_reason
        sig["execution_1m_score"] = float(confirm_score)
        sig["raw_signal_side"] = str(sig.get("raw_signal_side") or pending.get("raw_signal_side") or direction).upper()
        sig["setup_side"] = str(sig.get("setup_side") or pending.get("setup_side") or direction).upper()
        sig["pending_setup_side"] = str(pending.get("side") or pending.get("direction") or direction).upper()
        sig["confirmation_side"] = direction if ok else ""
        if not ok and confirm_reason in {"waiting for new completed 1M candle", "waiting for first completed post-setup 1M candle"}:
            sig["final_status"] = PENDING
            sig["state"] = PENDING
            sig["pending_reason"] = confirm_reason
            return False, confirm_reason
        if not ok:
            expires_at = self._pending_setup_expires_at(pending, now).isoformat()
            seconds_remaining = max(0.0, self._pending_setup_seconds_until_expiry(pending, now))
            failed_reason = f"NOT_QUALIFIED 1M confirmation {confirm_reason}; expires_at={expires_at} seconds_remaining={seconds_remaining:.1f}"
            sig["final_status"] = PENDING
            sig["state"] = PENDING
            sig["pending_reason"] = failed_reason
            sig["block_reason"] = failed_reason
            self._trace_step(sig, "ONE_MIN_CONFIRMATION_WAITING", PENDING, failed_reason, setup_id=pending.get("setup_id"), pending_key=pending_key)
            self._audit_decision({
                "event": "NOT_QUALIFIED", "decision": "not_qualified", "scope": "setup",
                "symbol": canonical, "side": direction, "setup_id": pending.get("setup_id"),
                "pending_key": pending_key, "reason": failed_reason,
                "expires_at": expires_at, "seconds_remaining": seconds_remaining,
            })
            return False, failed_reason
        sig["confirmation_detected_at"] = datetime.utcnow().isoformat()
        sig["setup_created_at"] = str(pending.get("setup_created_at") or pending.get("created_at") or "")
        sig["m1_candle_closed_at"] = str(sig.get("m1_candle_closed_at") or pending.get("last_m1_candle_closed_at") or "")
        setup_created_dt = _parse_dt(sig["setup_created_at"])
        detected_dt = _parse_dt(sig["confirmation_detected_at"])
        candle_closed_dt = _parse_dt(sig["m1_candle_closed_at"])
        sig["confirmation_detection_latency_ms"] = round(max(0.0, (detected_dt - candle_closed_dt).total_seconds() * 1000.0), 3) if detected_dt and candle_closed_dt else None
        sig["confirmation_latency_ms"] = sig.get("confirmation_detection_latency_ms")
        sig["total_setup_to_confirmation_ms"] = round(max(0.0, (detected_dt - setup_created_dt).total_seconds() * 1000.0), 3) if detected_dt and setup_created_dt else None
        stats = self.state.setdefault("setup_confirmation_age_stats", {"count": 0, "total_seconds": 0.0})
        stats["count"] = int(stats.get("count") or 0) + 1
        stats["total_seconds"] = float(stats.get("total_seconds") or 0.0) + float(age_min * 60.0)
        _ss.set_status("setup_confirmation_count", stats["count"])
        _ss.set_status("setup_confirmation_avg_age_seconds", round(stats["total_seconds"] / max(stats["count"], 1), 3))
        fresh_ok, fresh_reason, fresh_meta = self._setup_stale_entry_guard(canonical, market, pending, sig, enforce_fresh_age=True)
        if not fresh_ok:
            reason = f"NOT_QUALIFIED stale confirmation {fresh_reason}"
            self._mark_not_qualified(sig, reason, "STALE_ENTRY_QUALITY")
            self._trace_step(sig, "NOT_QUALIFIED", "NOT_QUALIFIED", reason, stale_entry=fresh_meta)
            self._remove_pending_setup(pending_key, pending, "cancelled", "NOT_QUALIFIED", reason, sig)
            self.analytics_engine.record("NOT_QUALIFIED", canonical, str(pending.get("engine") or ""), str(pending.get("strategy") or ""), float(pending.get("score") or 0.0), reason, sig)
            return False, reason
        confirmed_entry_price = float(fresh_meta.get("current_price") or sig.get("price") or pending.get("price") or 0.0)
        sig["entry_price_at_confirmation"] = confirmed_entry_price
        sig["price"] = confirmed_entry_price
        self._audit_decision({
            "event": "SETUP_CONFIRMED_FRESH", "decision": "allow", "scope": "setup",
            "symbol": canonical, "side": direction, "setup_id": pending.get("setup_id"),
            "setup_created_at": sig.get("setup_created_at"), "m1_candle_closed_at": sig.get("m1_candle_closed_at"),
            "confirmation_detected_at": sig.get("confirmation_detected_at"),
            "setup_age_seconds": round(age_min * 60.0, 3),
            "confirmation_detection_latency_ms": sig.get("confirmation_detection_latency_ms"),
            "total_setup_to_confirmation_ms": sig.get("total_setup_to_confirmation_ms"),
        })
        if bool(sig.get("replacement_strategy_v1")):
            exhaustion_ok, exhaustion_reason = True, "replacement exhaustion qualified before SETUP_CREATED"
            exhaustion_meta = dict((sig.get("_mt5_thresholds") or {}).get("index_exhaustion_pre_setup") or {})
        else:
            exhaustion_ok, exhaustion_reason, exhaustion_meta = self._index_exhaustion_allows_entry(canonical, market, sig)
        if not exhaustion_ok:
            reason = f"NOT_QUALIFIED index exhaustion {exhaustion_reason}"
            self._mark_not_qualified(sig, reason, "EXHAUSTION_QUALITY")
            self._trace_step(sig, "NOT_QUALIFIED", "NOT_QUALIFIED", reason, exhaustion=exhaustion_meta)
            self._remove_pending_setup(pending_key, pending, "cancelled", "NOT_QUALIFIED", reason, sig)
            self.analytics_engine.record("NOT_QUALIFIED", canonical, str(pending.get("engine") or ""), str(pending.get("strategy") or ""), float(pending.get("score") or 0.0), reason, sig)
            return False, reason
        sig["final_status"] = CONFIRMED
        sig["state"] = CONFIRMED
        sig["final_order_side"] = direction
        sig["mt5_order_type"] = "ORDER_TYPE_BUY" if direction == "BUY" else "ORDER_TYPE_SELL"
        sig["_pending_setup_confirmed_at"] = now.isoformat()
        sig["_pending_setup_created_at"] = pending.get("created_at")
        sig["_pending_setup_age_minutes"] = round(age_min, 3)
        pending["setup_created_at"] = sig.get("setup_created_at")
        pending["m1_candle_closed_at"] = sig.get("m1_candle_closed_at")
        pending["confirmation_detected_at"] = sig.get("confirmation_detected_at")
        pending["confirmation_detection_latency_ms"] = sig.get("confirmation_detection_latency_ms")
        pending["total_setup_to_confirmation_ms"] = sig.get("total_setup_to_confirmation_ms")
        self._set_pending_setup_status(pending_key, pending, "confirmed_1m", "SETUP_CONFIRMED_FRESH", "1M confirmation OK; SETUP_CONFIRMED_FRESH", sig)
        self._trace_step(sig, "ONE_MIN_CONFIRMATION_WAITING", CONFIRMED, "1M confirmation OK", setup_id=pending.get("setup_id"), pending_key=pending_key)
        self.analytics_engine.record(CONFIRMED, canonical, str(pending.get("engine") or ""), str(pending.get("strategy") or ""), float(pending.get("score") or 0.0), "SETUP_CONFIRMED_1M 1M confirmation OK", sig)
        return True, "SETUP_CONFIRMED_1M 1M confirmation OK"

    def _two_engine_test_risk_usd(self, canonical: str) -> float | None:
        if not self._two_engine_enabled() or _mt5_strategy_engines is None:
            return None
        if _mt5_strategy_engines.get_symbol_config(canonical) is None:
            return None
        try:
            realized_today = float(self._broker_day_realized_pnl())
        except Exception:
            realized_today = 0.0
        try:
            losing_streak = int(self._daily_loss_streak())
        except Exception:
            losing_streak = 0
        return float(_mt5_strategy_engines.calculate_test_risk_usd(realized_today, losing_streak))

    def _next_m15_scan_epoch(self, from_epoch: float | None = None) -> float:
        now = float(from_epoch if from_epoch is not None else time.time())
        delay = _env_float("MT5_M15_SCAN_DELAY_SECONDS", 8.0, lo=0.0, hi=120.0)
        interval = 15 * 60
        base = math.floor(now / interval) * interval
        candidate = base + delay
        if candidate <= now:
            candidate = base + interval + delay
        return candidate

    def _scan_due_text(self, epoch_seconds: float) -> str:
        return datetime.fromtimestamp(float(epoch_seconds)).isoformat()

    def _priority_scan_symbols(self) -> list[tuple[str, str, str]]:
        priority = set(self.priority_symbols)
        out: list[tuple[str, str, str]] = []
        seen: set[str] = set()
        for group, symbols in self.symbol_groups.items():
            for sym in symbols:
                canonical = self._canonical_from_resolved(sym)
                market = self._market_for_symbol(canonical)
                if canonical.upper() in priority and canonical not in seen:
                    seen.add(canonical)
                    out.append((canonical, group, market))
        return out

    def _open_position_count(self) -> int:
        live = self.gateway.positions()
        if live:
            if _env_bool("MT5_COUNT_DISABLED_POSITIONS_FOR_MAX_OPEN", False):
                return len(live)
            return sum(
                1
                for pos in live
                if self._canonical_from_resolved(pos.symbol) in self.symbol_markets
            )
        if _env_bool("MT5_COUNT_DISABLED_POSITIONS_FOR_MAX_OPEN", False):
            return len(self.open_trades)
        return sum(
            1
            for meta in self.open_trades.values()
            if isinstance(meta, dict) and str(meta.get("sym") or "") in self.symbol_markets
        )

    def _symbol_has_open_position(self, sym: str) -> bool:
        canonical = self._canonical_from_resolved(sym)
        if any(self._canonical_from_resolved(pos.symbol) == canonical for pos in self.gateway.positions()):
            return True
        return any(meta.get("sym") == canonical for meta in self.open_trades.values())

    def _assert_expected_account(self, acct) -> None:
        actual = int(getattr(acct, "login", 0) or 0)
        expected = int(self.runtime.login or 0)
        if expected and actual <= 0:
            _ss.set_status("mt5_connected", False)
            _ss.set_status("account_id", "")
            _ss.set_status("account_mismatch", "waiting for MT5 account snapshot")
            return
        if expected and actual != expected:
            _ss.set_status("mt5_connected", False)
            _ss.set_status("account_id", str(actual or ""))
            _ss.set_status("account_mismatch", f"expected {expected}, got {actual}")
            raise RuntimeError(f"MT5 account mismatch: expected {expected}, got {actual}")
        _ss.set_status("account_mismatch", "")
        self._update_demo_profile_status(acct)


    def _startup_self_check(self) -> None:
        checks = {
            "HAS_FOREX_ENGINE": FOREX_ENGINE == "FOREX_ENGINE",
            "HAS_INDEX_ENGINE": INDEX_ENGINE == "INDEX_ENGINE",
            "HAS_METALS_ENGINE": METALS_ENGINE == "METALS_ENGINE",
            "HAS_CREATE_PENDING_SETUP": callable(getattr(self, "create_pending_setup", None)),
            "HAS_FOREX_SPREAD_GATE": callable(forex_spread_gate),
            "HAS_INDEX_SPREAD_GATE": callable(index_spread_gate),
            "HAS_METALS_SPREAD_GATE": callable(metals_spread_gate),
        }
        for key, value in checks.items():
            print(f"{key}={bool(value)}", flush=True)
            _ss.set_status(key.lower(), bool(value))
        _ss.set_status("ruleset_version", RULESET_VERSION)
        _ss.set_status("build_id", BUILD_ID)
        _ss.set_status("scanner_process_started_at", self.scanner_process_started_at)
        if not all(checks.values()):
            missing = ",".join(key for key, value in checks.items() if not value)
            _ss.set_status("halt", f"CRITICAL multi-engine self-check failed: {missing}")
            print(f"CRITICAL multi-engine self-check failed: {missing}", flush=True)
            raise RuntimeError(f"CRITICAL multi-engine self-check failed: {missing}")

    def connect(self) -> None:
        self._startup_self_check()
        self.gateway.connect()
        acct = self.gateway.account_info()
        self._assert_expected_account(acct)
        self._update_demo_profile_status(acct)
        _ss.set_status("broker_name", "XM Global MT5")
        _ss.set_status("platform_name", "MetaTrader 5")
        _ss.set_status("mode", self.runtime.trade_mode.upper())
        _ss.set_status("ibkr_connected", False)
        _ss.set_status("mt5_connected", True)
        _ss.set_status("account_id", str(getattr(acct, "login", "")))
        _ss.set_status("equity", float(getattr(acct, "equity", 0.0) or 0.0))
        _ss.set_status("cap_usd", float(self.runtime.capital_cap_usd))
        _ss.set_status("strategy_equity", self._strategy_equity(acct))
        _ss.set_status("mt5_symbol_count", len(self.runtime.all_symbols()))
        _ss.set_status("mt5_markets", sorted(self.runtime.enabled_market_set))
        _ss.set_status("mt5_symbol_alias_count", len(self.runtime.symbol_aliases))
        self._update_demo_profile_status(acct)
        _ss.set_status("max_open_trades", self._effective_max_open_trades())
        _ss.set_status("max_daily_trades", self._effective_max_daily_trades())
        _ss.set_status("max_position_pct", float(self.runtime.max_position_pct))

    def run(self) -> None:
        self.connect()
        def _request_stop(signum, frame):
            self._stop_requested = True
            _ss.set_status("shutdown_requested", f"signal={signum}")
            print(f"[MT5_SHUTDOWN_REQUESTED] signal={signum}", flush=True)

        signal.signal(signal.SIGTERM, _request_stop)
        signal.signal(signal.SIGINT, _request_stop)
        align_full_scan = False
        next_scan_at = 0.0
        next_pending_monitor_at = 0.0
        next_housekeeping_at = 0.0
        next_heartbeat_at = 0.0
        next_close_reconcile_at = 0.0
        next_campaign_at = 0.0
        next_deal_reconcile_at = 0.0
        next_position_sync_at = 0.0
        next_loop_status_at = 0.0
        max_loop_ms = 0.0
        poll_interval = max(0.1, float(self.runtime.poll_seconds or 1.0))
        configured_scan_interval = _env_float("MT5_FULL_SCAN_SECONDS", FULL_SCAN_SECONDS, lo=1.0, hi=300.0)
        scan_interval = max(1, int(min(configured_scan_interval, FULL_SCAN_SECONDS)))
        pending_monitor_enabled = _env_bool("MT5_PENDING_MONITOR_ENABLE", True)
        pending_monitor_interval = _env_float("MT5_PENDING_MONITOR_SECONDS", PENDING_MONITOR_SECONDS, lo=0.25, hi=1.0)
        housekeeping_interval = _env_float("MT5_HOUSEKEEPING_SECONDS", HOUSEKEEPING_SECONDS, lo=60.0)
        heartbeat_interval = _env_float("MT5_HEARTBEAT_SECONDS", 1.0, lo=0.2)
        close_reconcile_interval = _env_float("MT5_CLOSE_RECONCILE_SECONDS", 0.5, lo=0.1)
        deal_reconcile_interval = _env_float("MT5_DEAL_RECONCILE_SECONDS", 3.0, lo=0.5)
        campaign_interval = _env_float("MT5_CAMPAIGN_MONITOR_SECONDS", 0.50, lo=0.25, hi=2.0)
        _ss.set_status("poll_seconds", poll_interval)
        _ss.set_status("scan_seconds", scan_interval)
        _ss.set_status("full_scan_aligned_to_m15", align_full_scan)
        _ss.set_status("m15_scan_delay_seconds", _env_float("MT5_M15_SCAN_DELAY_SECONDS", 8.0, lo=0.0, hi=120.0))
        _ss.set_status("pending_monitor_enabled", pending_monitor_enabled)
        _ss.set_status("pending_monitor_seconds", pending_monitor_interval)
        _ss.set_status("housekeeping_seconds", housekeeping_interval)
        _ss.set_status("priority_scan_enabled", bool(pending_monitor_enabled))
        _ss.set_status("priority_scan_seconds", pending_monitor_interval)
        _ss.set_status("last_priority_scan_result", "high-priority pending execution loop checks completed M1 candles")
        _ss.set_status("heartbeat_seconds", heartbeat_interval)
        _ss.set_status("close_reconcile_seconds", close_reconcile_interval)
        _ss.set_status("deal_reconcile_seconds", deal_reconcile_interval)
        _ss.set_status("campaign_monitor_seconds", campaign_interval)
        live_tickets = []
        try:
            while not self._stop_requested:
                loop_started = time.monotonic()
                now_monotonic = loop_started
                pending_active = bool(self.pending_setups)
                now_epoch = time.time()
                # Pending confirmation gets first use of the loop. Position/account work is
                # cadence-limited so it cannot starve the completed-M1 check.
                if pending_monitor_enabled and pending_active and now_epoch >= next_pending_monitor_at:
                    self._monitor_pending_setups()
                    next_pending_monitor_at = time.time() + pending_monitor_interval
                    _ss.set_status("next_pending_monitor_due", self._scan_due_text(next_pending_monitor_at))
                if now_monotonic >= next_heartbeat_at:
                    self._heartbeat()
                    next_heartbeat_at = time.monotonic() + heartbeat_interval
                if now_monotonic >= next_position_sync_at:
                    live_tickets = self._sync_positions()
                    sync_interval = max(1.0, poll_interval) if pending_active else poll_interval
                    next_position_sync_at = time.monotonic() + sync_interval
                now_monotonic = time.monotonic()
                if now_monotonic >= next_close_reconcile_at:
                    self._reconcile_closed_positions(live_tickets, reconcile_history=False)
                    next_close_reconcile_at = time.monotonic() + close_reconcile_interval
                if now_monotonic >= next_campaign_at:
                    self._campaign_cycle()
                    next_campaign_at = time.monotonic() + campaign_interval
                    _ss.set_status("last_campaign_cycle", datetime.utcnow().isoformat())
                now_monotonic = time.monotonic()
                if now_monotonic >= next_deal_reconcile_at:
                    self._reconcile_mt5_deal_history(live_tickets)
                    self._save_state()
                    next_deal_reconcile_at = time.monotonic() + deal_reconcile_interval
                if now_monotonic >= next_housekeeping_at:
                    self._housekeeping_loop()
                    next_housekeeping_at = time.monotonic() + housekeeping_interval
                now_epoch = time.time()
                if now_epoch >= next_scan_at:
                    self._scan_and_trade()
                    now_epoch = time.time()
                    next_scan_at = self._next_m15_scan_epoch(now_epoch) if align_full_scan else now_epoch + scan_interval
                    next_pending_monitor_at = now_epoch + pending_monitor_interval
                    _ss.set_status("next_scan_due", self._scan_due_text(next_scan_at))
                    _ss.set_status("next_pending_monitor_due", self._scan_due_text(next_pending_monitor_at))
                elif pending_monitor_enabled and now_epoch >= next_pending_monitor_at:
                    self._monitor_pending_setups()
                    next_pending_monitor_at = time.time() + pending_monitor_interval
                    _ss.set_status("next_scan_due", self._scan_due_text(next_scan_at))
                    _ss.set_status("next_pending_monitor_due", self._scan_due_text(next_pending_monitor_at))
                else:
                    _ss.set_status(
                        "next_scan_due",
                        self._scan_due_text(next_scan_at),
                    )
                    if pending_monitor_enabled:
                        _ss.set_status("next_pending_monitor_due", self._scan_due_text(next_pending_monitor_at))
                elapsed = time.monotonic() - loop_started
                max_loop_ms = max(max_loop_ms, elapsed * 1000.0)
                if time.monotonic() >= next_loop_status_at:
                    _ss.set_status("last_loop_ms", round(elapsed * 1000.0, 1))
                    _ss.set_status("max_loop_ms", round(max_loop_ms, 1))
                    max_loop_ms = 0.0
                    next_loop_status_at = time.monotonic() + 1.0
                pending_active = bool(self.pending_setups)
                sleep_target = pending_monitor_interval if pending_active else poll_interval
                time.sleep(max(0.0, sleep_target - elapsed))
        finally:
            _ss.set_status("shutdown_complete", datetime.utcnow().isoformat())
            _ss.set_status("mt5_connected", False)
            self.gateway.shutdown()

    def _housekeeping_loop(self) -> None:
        self._save_state()
        try:
            export_status = _ss.sync_mt5_trade_exports()
            _ss.set_status("last_export_reconciliation", json.dumps(export_status, sort_keys=True))
        except Exception as exc:
            _ss.set_status("last_export_reconciliation", f"FAIL: {str(exc)[:180]}")
        _ss.set_status("last_housekeeping", datetime.now().isoformat())
        _ss.set_status("last_housekeeping_result", "state saved; SQLite trade exports reconciled")
        print("[MT5_HOUSEKEEPING] state saved; SQLite trade exports reconciled", flush=True)

    def _heartbeat(self) -> None:
        acct = self.gateway.account_info()
        self._assert_expected_account(acct)
        _ss.set_status("last_heartbeat", datetime.now().isoformat())
        _ss.set_status("equity", float(getattr(acct, "equity", 0.0) or 0.0))
        _ss.set_status("balance", float(getattr(acct, "balance", 0.0) or 0.0))
        _ss.set_status("free_margin", float(getattr(acct, "free_margin", 0.0) or 0.0))
        _ss.set_status("cap_usd", float(self.runtime.capital_cap_usd))
        _ss.set_status("strategy_equity", self._strategy_equity(acct))
        _ss.set_status("daily_pnl", float(getattr(acct, "profit", 0.0) or 0.0))
        self._update_demo_profile_status(acct)
        _ss.set_status("max_open_trades", self._effective_max_open_trades())
        _ss.set_status("max_daily_trades", self._effective_max_daily_trades())
        _ss.set_status("max_position_pct", float(self.runtime.max_position_pct))
        _ss.set_status("daily_trade_count", self._daily_trade_count())
        allowed, reason = self._account_trading_allowed(acct)
        _ss.set_status("mt5_trade_allowed", allowed)
        if not allowed:
            _ss.set_status("halt", reason)
        elif not self._daily_risk_allows_entry():
            _ss.set_status("halt", "daily loss limit reached")
        else:
            _, streak_reason = self._daily_loss_streak_allows_entry()
            _ss.set_status("loss_streak_status", streak_reason)
        _ss.set_status("usdzar", get_usdzar())

    def _sync_positions(self) -> set[str]:
        live = self.gateway.positions()
        live_tickets = {str(pos.ticket) for pos in live}
        if self._risk_guard_live_positions(live):
            return live_tickets
        live_symbols = set()
        for pos in live:
            canonical = self._canonical_from_resolved(pos.symbol)
            market = self._market_for_symbol(canonical)
            live_symbols.add(canonical)
            self._adopt_live_position(pos, canonical, market)
            protected = self._ensure_position_protection(pos, canonical, market)
            self._open_position_cycle_snapshot(pos, canonical, market, protected)
            if self._monitor_open_position(pos, canonical, market):
                continue

            managed = self._cap_position_tp(pos, canonical, market, protected)
            if not managed:
                managed = self._manage_open_position(pos, canonical, market, protected)
            sl = managed[0] if managed else protected[0] if protected else pos.sl
            tp = managed[1] if managed else protected[1] if protected else pos.tp
            _ss.upsert_position(
                canonical,
                market,
                pos.direction,
                pos.volume,
                pos.price_open,
                pos.price_current,
                sl,
                tp,
                abs(pos.price_open - sl) if sl else 0.0,
                pos.profit,
            )
        for row in _ss.read_positions():
            if row.get("sym") not in live_symbols:
                _ss.remove_position(row["sym"])
        return live_tickets

    def _close_position_for_risk(self, pos: MT5PositionView, canonical: str, reason: str) -> bool:
        try:
            self.gateway.close_position(pos)
            _ss.set_status("last_risk_close", f"{canonical}:{pos.ticket}: {reason}")
            meta = self.open_trades.get(str(pos.ticket))
            if meta:
                meta["risk_close_reason"] = reason
                meta["exit_source"] = "MANUAL"
                meta["exit_reason"] = reason
            self._record_symbol_close(canonical, datetime.utcnow().isoformat(), "manual_close_sent", float(pos.profit or 0.0))
            self._save_state()
            return True
        except Exception as exc:
            _ss.set_status("last_risk_close_error", f"{canonical}:{pos.ticket}: {reason}: {str(exc)[:180]}")
            return False

    def _risk_guard_live_positions(self, live: list[MT5PositionView]) -> bool:
        if not live:
            return False
        total_profit = sum(float(pos.profit or 0.0) for pos in live)
        account_limit = _env_float("MT5_MAX_ACCOUNT_FLOATING_LOSS_USD", 250.0, lo=0.0)
        if _env_bool("MT5_CLOSE_ON_ACCOUNT_FLOATING_LOSS", True) and account_limit > 0 and total_profit <= -account_limit:
            _ss.set_status("halt", f"account floating loss limit reached ({total_profit:.2f} <= -{account_limit:.2f})")
            closed_any = False
            for pos in live:
                canonical = self._canonical_from_resolved(pos.symbol)
                closed_any = self._close_position_for_risk(pos, canonical, f"account floating loss {total_profit:.2f}") or closed_any
            return closed_any

        symbol_profit: dict[str, float] = {}
        symbol_market: dict[str, str] = {}
        for pos in live:
            canonical = self._canonical_from_resolved(pos.symbol)
            market = self._market_for_symbol(canonical)
            symbol_profit[canonical] = symbol_profit.get(canonical, 0.0) + float(pos.profit or 0.0)
            symbol_market[canonical] = market

        for canonical, profit in symbol_profit.items():
            market = symbol_market.get(canonical, "forex")
            limit = self._loss_limit_for_symbol(canonical, market, "MT5_MAX_SYMBOL_FLOATING_LOSS", 150.0)
            if limit > 0 and profit <= -limit:
                _ss.set_status("halt", f"{canonical} floating loss limit reached ({profit:.2f} <= -{limit:.2f})")
                closed_any = False
                for pos in live:
                    if self._canonical_from_resolved(pos.symbol) == canonical:
                        closed_any = self._close_position_for_risk(pos, canonical, f"symbol floating loss {profit:.2f}") or closed_any
                return closed_any

        for pos in live:
            canonical = self._canonical_from_resolved(pos.symbol)
            market = self._market_for_symbol(canonical)
            limit = self._loss_limit_for_symbol(canonical, market, "MT5_MAX_OPEN_POSITION_LOSS", 100.0)
            profit = float(pos.profit or 0.0)
            if limit > 0 and profit <= -limit:
                _ss.set_status("halt", f"{canonical} position loss limit reached ({profit:.2f} <= -{limit:.2f})")
                return self._close_position_for_risk(pos, canonical, f"position loss {profit:.2f}")
        return False

    def _adopt_live_position(self, pos: MT5PositionView, canonical: str, market: str) -> None:
        ticket = str(pos.ticket)
        if ticket in self.open_trades:
            return
        existing_trade = None
        try:
            existing_trade = _ss.find_live_trade_for_symbol(canonical, pos.direction, pos.price_open)
        except Exception:
            existing_trade = None
        opened_at = pos.time.isoformat() if pos.time else datetime.now().isoformat()
        atr = abs(pos.price_open - pos.sl) if pos.sl else self._recent_atr(canonical)
        if existing_trade:
            existing_trade_id = str(existing_trade.get("trade_id") or "")
            existing_ticket = None
            for state_ticket, meta in list(self.open_trades.items()):
                if isinstance(meta, dict) and str(meta.get("trade_id") or "") == existing_trade_id:
                    existing_ticket = str(state_ticket)
                    break
            if existing_ticket and existing_ticket != ticket:
                try:
                    live_ticket_set = {str(live_pos.ticket) for live_pos in self.gateway.positions()}
                except Exception:
                    live_ticket_set = set()
                if existing_ticket not in live_ticket_set:
                    existing_meta = self.open_trades.pop(existing_ticket)
                    if isinstance(existing_meta, dict):
                        existing_meta.update({
                            "sym": canonical,
                            "market": market,
                            "direction": pos.direction,
                            "qty": pos.volume,
                            "entry": pos.price_open,
                            "sl": pos.sl,
                            "tp": pos.tp,
                            "atr": atr,
                            "resolved_symbol": pos.symbol,
                        })
                        self.open_trades[ticket] = existing_meta
                        self._save_state()
                        _ss.set_status("last_adopted_position", f"{canonical}:{ticket}:remapped:{existing_trade_id}")
                        return
            if existing_ticket:
                existing_trade = None
        trade_id = str(existing_trade.get("trade_id")) if existing_trade else f"mt5_adopted_{canonical}_{ticket}"
        self.open_trades[ticket] = {
            "trade_id": trade_id,
            "sym": canonical,
            "market": market,
            "direction": pos.direction,
            "qty": pos.volume,
            "entry": pos.price_open,
            "sl": pos.sl,
            "tp": pos.tp,
            "atr": atr,
            "sig_mode": "mt5_adopted",
            "opened_at": opened_at,
            "trade_date": datetime.now().date().isoformat(),
            "resolved_symbol": pos.symbol,
            "risk_meta": {"adopted": True},
        }
        if existing_trade:
            try:
                _ss.update_trade_protection(trade_id, pos.sl, pos.tp, atr)
            except Exception:
                pass
            _ss.set_status("last_adopted_position", f"{canonical}:{ticket}:reused:{trade_id}")
        else:
            _ss.insert_trade(
                trade_id,
                canonical,
                market,
                pos.direction,
                pos.volume,
                pos.price_open,
                pos.sl,
                pos.tp,
                atr,
                "mt5_adopted",
                opened_at,
                status="live",
                strategy="mt5_adopted",
                score=0.0,
            )
            _ss.set_status("last_adopted_position", f"{canonical}:{ticket}")
        self._save_state()

    def _ensure_position_protection(self, pos: MT5PositionView, canonical: str, market: str) -> tuple[float, float] | None:
        if pos.sl > 0 and pos.tp > 0:
            return None
        price = float(pos.price_current or pos.price_open or 0.0)
        if price <= 0:
            return None
        atr = self._recent_atr(canonical)
        stop_d = _compute_stop_distance(price, atr, market=market, is_crypto=(market == "crypto"))
        rr = self._management_rr_for_position(canonical, market, str(pos.direction or ""), self.open_trades.get(str(pos.ticket), {}))
        if pos.direction == "BUY":
            sl = price - stop_d
            tp = price + (stop_d * rr)
        else:
            sl = price + stop_d
            tp = price - (stop_d * rr)
        spec = self.gateway.symbol_info(pos.symbol)
        sl = self.gateway.normalize_price(spec, sl)
        tp = self.gateway.normalize_price(spec, tp)
        try:
            result = self.gateway.modify_position(pos.ticket, pos.symbol, sl, tp)
            retcode = int(getattr(result, "retcode", 10009) or 10009)
            if retcode not in TRADE_RETCODE_DONE:
                _ss.set_status("last_protection_error", f"{canonical}:{pos.ticket}: retcode={retcode}")
                return None
        except Exception as exc:
            _ss.set_status("last_protection_error", f"{canonical}:{pos.ticket}: {str(exc)[:220]}")
            return None
        meta = self.open_trades.get(str(pos.ticket))
        if meta:
            meta["sl"] = sl
            meta["tp"] = tp
            meta["atr"] = atr
            self._save_state()
            try:
                _ss.update_trade_protection(meta["trade_id"], sl, tp, atr)
            except Exception:
                pass
        _ss.set_status("last_protection_update", f"{canonical}:{pos.ticket}: sl={sl} tp={tp}")
        return sl, tp

    def _is_more_protective_sl(self, direction: str, candidate: float, current_sl: float) -> bool:
        if candidate <= 0:
            return False
        if current_sl <= 0:
            return True
        return candidate > current_sl if direction == "BUY" else candidate < current_sl

    def _managed_sl_is_valid(
        self,
        direction: str,
        sl: float,
        spec: MT5SymbolSpec,
        bid: float,
        ask: float,
    ) -> tuple[bool, str]:
        if sl <= 0 or bid <= 0 or ask <= 0 or ask < bid:
            return False, "invalid tick or stop"
        point = float(spec.point or 0.0) or 0.00001
        stops_dist = max(float(spec.stops_level or 0) * point, point)
        spread = max(0.0, ask - bid)
        min_dist = max(stops_dist, spread + point)
        if direction == "BUY" and sl >= bid - min_dist:
            return False, f"buy SL too close to bid ({sl} >= {bid - min_dist})"
        if direction == "SELL" and sl <= ask + min_dist:
            return False, f"sell SL too close to ask ({sl} <= {ask + min_dist})"
        return True, "OK"

    def _apply_position_modify(
        self,
        pos: MT5PositionView,
        canonical: str,
        sl: float,
        tp: float,
        atr: float,
        reason: str,
    ) -> tuple[float, float] | None:
        try:
            result = self.gateway.modify_position(pos.ticket, pos.symbol, sl, tp)
            retcode = int(getattr(result, "retcode", 10009) or 10009)
            if retcode not in TRADE_RETCODE_DONE:
                _ss.set_status("last_trade_manager_error", f"{canonical}:{pos.ticket}: retcode={retcode} {reason}")
                return None
        except Exception as exc:
            _ss.set_status("last_trade_manager_error", f"{canonical}:{pos.ticket}: {str(exc)[:220]}")
            return None

        meta = self.open_trades.get(str(pos.ticket))
        if meta:
            meta["sl"] = sl
            meta["tp"] = tp
            meta["atr"] = atr
            meta["last_trade_manager_ts"] = time.time()
            meta["last_trade_manager_reason"] = reason
            self._save_state()
            try:
                _ss.update_trade_protection(meta["trade_id"], sl, tp, atr)
            except Exception:
                pass
        _ss.set_status("last_trade_manager_update", f"{canonical}:{pos.ticket}: {reason} sl={sl} tp={tp}")
        return sl, tp

    def _cap_position_tp(
        self,
        pos: MT5PositionView,
        canonical: str,
        market: str,
        protected: tuple[float, float] | None = None,
    ) -> tuple[float, float] | None:
        if not _env_bool("MT5_SCALP_TP_ENABLE", True):
            return None
        direction = str(pos.direction or "").upper()
        if direction not in {"BUY", "SELL"}:
            return None
        entry = float(pos.price_open or 0.0)
        current_sl = float(protected[0] if protected else pos.sl or 0.0)
        current_tp = float(protected[1] if protected else pos.tp or 0.0)
        if entry <= 0 or current_sl <= 0 or current_tp <= 0:
            return None
        risk_distance = abs(entry - current_sl)
        if risk_distance <= 0:
            return None
        target_r = self._management_rr_for_position(canonical, market, direction, self.open_trades.get(str(pos.ticket), {}))
        if target_r <= 0:
            return None
        try:
            spec = self.gateway.symbol_info(pos.symbol)
        except Exception as exc:
            _ss.set_status("last_trade_manager_error", f"{canonical}:{pos.ticket}: spec unavailable {str(exc)[:120]}")
            return None
        raw_tp = entry + (risk_distance * target_r) if direction == "BUY" else entry - (risk_distance * target_r)
        scalp_tp = self.gateway.normalize_price(spec, raw_tp)
        tighter_tp = (
            direction == "BUY" and scalp_tp > entry and scalp_tp < current_tp
        ) or (
            direction == "SELL" and scalp_tp < entry and scalp_tp > current_tp
        )
        if not tighter_tp:
            return None
        current = float(pos.price_current or 0.0)
        if current > 0:
            reached = (direction == "BUY" and current >= scalp_tp) or (direction == "SELL" and current <= scalp_tp)
            if reached:
                self._close_position_for_profit(pos, canonical, f"scalp TP reached {target_r:.2f}R")
                return None
        return self._apply_position_modify(pos, canonical, current_sl, scalp_tp, risk_distance, f"scalp TP cap {target_r:.2f}R")

    def _close_position_for_profit(self, pos: MT5PositionView, canonical: str, reason: str) -> bool:
        try:
            result = self.gateway.close_position(pos)
            retcode = int(getattr(result, "retcode", 10009) or 10009)
            if retcode not in TRADE_RETCODE_DONE:
                _ss.set_status("last_profit_close_error", f"{canonical}:{pos.ticket}: retcode={retcode} {reason}")
                return False
            meta = self.open_trades.get(str(pos.ticket))
            if meta:
                meta["profit_close_reason"] = reason
                meta["exit_source"] = "TP"
                meta["exit_reason"] = reason
                meta["last_trade_manager_ts"] = time.time()
            self._record_symbol_close(canonical, datetime.utcnow().isoformat(), "tp_close_sent", float(pos.profit or 0.0))
            self._save_state()
            _ss.set_status("last_profit_close", f"{canonical}:{pos.ticket}: {reason}")
            return True
        except Exception as exc:
            _ss.set_status("last_profit_close_error", f"{canonical}:{pos.ticket}: {reason}: {str(exc)[:180]}")
            return False

    def _close_position_for_monitor(self, pos: MT5PositionView, canonical: str, reason: str, exit_source: str = "TIMEOUT") -> bool:
        try:
            result = self.gateway.close_position(pos)
            retcode = int(getattr(result, "retcode", 10009) or 10009)
            if retcode not in TRADE_RETCODE_DONE:
                _ss.set_status("last_open_monitor_error", f"{canonical}:{pos.ticket}: retcode={retcode} {reason}")
                return False
            exit_source = str(exit_source or "TIMEOUT").upper()
            meta = self.open_trades.get(str(pos.ticket))
            if meta:
                meta["monitor_close_reason"] = reason
                meta["exit_source"] = exit_source
                meta["exit_reason"] = reason
                meta["last_open_monitor_ts"] = time.time()
            self._record_symbol_close(
                canonical,
                datetime.utcnow().isoformat(),
                f"{exit_source.lower()}_close_sent",
                float(pos.profit or 0.0),
            )
            self._audit_decision({"event": "position_close_requested", "symbol": canonical, "ticket": pos.ticket, "exit_source": exit_source, "reason": reason[:240], "retcode": retcode})
            self._save_state()
            _ss.set_status("last_open_monitor_close", f"{canonical}:{pos.ticket}: exit_source={exit_source} {reason}")
            return True
        except Exception as exc:
            _ss.set_status("last_open_monitor_error", f"{canonical}:{pos.ticket}: {reason}: {str(exc)[:180]}")
            return False

    def _position_age_minutes(self, pos: MT5PositionView, meta: dict) -> float | None:
        opened = _parse_dt(meta.get("opened_at"))
        if opened is None:
            opened = _parse_dt(getattr(pos, "time", None))
        if opened is None:
            return None
        return max(0.0, (datetime.utcnow() - opened).total_seconds() / 60.0)

    def _open_position_cycle_snapshot(
        self,
        pos: MT5PositionView,
        canonical: str,
        market: str,
        protected: tuple[float, float] | None = None,
    ) -> None:
        current = float(pos.price_current or pos.price_open or 0.0)
        sl = float(protected[0] if protected else pos.sl or 0.0)
        tp = float(protected[1] if protected else pos.tp or 0.0)
        sl_dist = abs(current - sl) if current > 0 and sl > 0 else 0.0
        tp_dist = abs(tp - current) if current > 0 and tp > 0 else 0.0
        spread_text = "unknown"
        try:
            _resolved, tick = self.gateway.symbol_tick(canonical)
            bid = float(getattr(tick, "bid", 0.0) or 0.0)
            ask = float(getattr(tick, "ask", 0.0) or 0.0)
            if bid > 0 and ask >= bid:
                spread_text = f"{ask - bid:.5f}"
        except Exception:
            pass
        news_text = "unknown"
        if _news_filter is not None:
            try:
                news_text = "blackout" if _news_filter.is_event_blackout() else "clear"
            except Exception:
                news_text = "error"
        meta = self.open_trades.get(str(pos.ticket), {})
        age_minutes = self._position_age_minutes(pos, meta)
        age_text = "unknown" if age_minutes is None else f"{age_minutes:.1f}m"
        session_text = "open" if in_trade_window(market) else "closed"
        _ss.set_status(
            "last_open_position_cycle_check",
            (
                f"{canonical}:{pos.ticket}: pnl={float(pos.profit or 0.0):.2f} "
                f"sl_dist={sl_dist:.5f} tp_dist={tp_dist:.5f} spread={spread_text} "
                f"session={session_text} news={news_text} age={age_text}"
            ),
        )

    def _trend_invalidated_on_timeframe(self, canonical: str, direction: str, timeframe: str, bars: int) -> tuple[bool, str]:
        try:
            df = self.gateway.rates(canonical, timeframe, bars)
        except Exception as exc:
            return False, f"{timeframe} unavailable ({str(exc)[:80]})"
        if df is None or len(df) < 53 or "Close" not in df:
            return False, f"{timeframe} history insufficient"

        completed = df.iloc[:-1]
        close = pd.to_numeric(completed["Close"], errors="coerce").dropna()
        if len(close) < 52:
            return False, f"{timeframe} closes insufficient"
        ema20 = close.ewm(span=20, adjust=False).mean()
        ema50 = close.ewm(span=50, adjust=False).mean()
        last_two = close.tail(2)
        ema20_last = float(ema20.iloc[-1])
        ema50_last = float(ema50.iloc[-1])
        last_close = float(close.iloc[-1])

        if direction == "BUY":
            invalid = bool((last_two < ema20.tail(2)).all() and ema20_last < ema50_last)
        else:
            invalid = bool((last_two > ema20.tail(2)).all() and ema20_last > ema50_last)
        state = (
            f"{timeframe} close={last_close:.5f} ema20={ema20_last:.5f} "
            f"ema50={ema50_last:.5f} two_bar_reversal={int(invalid)}"
        )
        return invalid, state

    def _trend_invalidated(self, canonical: str, direction: str) -> tuple[bool, str]:
        return self._trend_invalidated_on_timeframe(canonical, direction, "M15", 80)

    def _monitor_open_position(self, pos: MT5PositionView, canonical: str, market: str) -> bool:
        if not _env_bool("MT5_OPEN_MONITOR_ENABLE", True):
            return False
        if canonical not in self.symbol_markets:
            return False

        direction = str(pos.direction or "").upper()
        if direction not in {"BUY", "SELL"}:
            return False
        meta = self.open_trades.get(str(pos.ticket), {})
        now = time.time()
        interval = _env_int("MT5_OPEN_MONITOR_SECONDS", 20, lo=1)
        try:
            last_ts = float(meta.get("last_open_monitor_ts") or 0.0)
        except Exception:
            last_ts = 0.0
        if last_ts > 0 and now - last_ts < interval:
            return False
        if meta:
            meta["last_open_monitor_ts"] = now

        age_minutes = self._position_age_minutes(pos, meta)
        age_text = "unknown" if age_minutes is None else f"{age_minutes:.1f}m"

        session_state = "open" if in_trade_window(market) else "closed"

        if _env_bool("MT5_MAX_HOLD_ENABLE", True):
            max_hold = self._profile_cap_int(
                canonical,
                "max_duration_minutes",
                _env_int("MT5_MAX_HOLD_MINUTES", 30, lo=1),
            )
            if age_minutes is not None and age_minutes >= max_hold:
                return self._close_position_for_monitor(
                    pos,
                    canonical,
                    f"maximum hold exceeded age={age_minutes:.1f}m limit={max_hold}m",
                    exit_source="TIMEOUT",
                )

        tighten_after = self._profile_int(canonical, "tighten_after_minutes")
        tighten_profit_floor = self._profile_float(canonical, "tighten_if_profit_below_usd")
        if (
            age_minutes is not None
            and tighten_after is not None
            and tighten_after > 0
            and age_minutes >= tighten_after
            and tighten_profit_floor is not None
            and float(pos.profit or 0.0) <= float(tighten_profit_floor)
        ):
            return self._close_position_for_monitor(
                pos,
                canonical,
                (
                    f"profile tighten exit age={age_minutes:.1f}m "
                    f"profit={float(pos.profit or 0.0):.2f} <= {float(tighten_profit_floor):.2f}"
                ),
                exit_source="TIMEOUT",
            )

        news_state = "clear"
        if _env_bool("MT5_NEWS_MONITOR_ENABLE", True):
            if _news_filter is None:
                news_state = "unavailable"
            else:
                try:
                    if _news_filter.is_event_blackout():
                        return self._close_position_for_monitor(
                            pos,
                            canonical,
                            "high-impact news blackout active",
                            exit_source="MANUAL",
                        )
                except Exception as exc:
                    news_state = f"error:{str(exc)[:60]}"

        trend_close_enabled = _env_bool("MT5_ENABLE_TREND_INVALIDATION_CLOSE", False)
        trend_warning_enabled = _env_bool("MT5_TREND_MONITOR_ENABLE", True) or trend_close_enabled
        _ss.set_status("MT5_ENABLE_TREND_INVALIDATION_CLOSE", str(trend_close_enabled).lower())
        trend_state = "WARNING_ONLY disabled" if not trend_close_enabled else "close_enabled"
        if trend_warning_enabled:
            min_age = _env_int("MT5_TREND_MONITOR_MIN_AGE_MINUTES", 5, lo=0)
            if trend_close_enabled:
                min_age = max(15, _env_int("MT5_TREND_INVALIDATION_MIN_AGE_MINUTES", 15, lo=0))
            if age_minutes is None or age_minutes >= min_age:
                m15_invalid, m15_state = self._trend_invalidated_on_timeframe(canonical, direction, "M15", 80)
                trend_state = f"WARNING_ONLY M15_invalid={int(m15_invalid)} {m15_state}"
                if meta and m15_invalid:
                    meta["trend_invalidation_warning"] = True
                    self._save_state()
                if trend_close_enabled and m15_invalid:
                    m5_invalid, m5_state = self._trend_invalidated_on_timeframe(canonical, direction, "M5", 120)
                    min_loss = abs(_env_float("MT5_TREND_INVALIDATION_MIN_LOSS_USD", 1.0, lo=0.0))
                    loss_ok = float(pos.profit or 0.0) <= -min_loss
                    monitor_atr = float(meta.get("atr") or 0.0) if isinstance(meta, dict) else 0.0
                    spread_snapshot = self._spread_snapshot_now(canonical, market, monitor_atr)
                    spread_atr = float(spread_snapshot.get("spread_atr") or 0.0)
                    max_spread_atr = _env_float("MT5_TREND_INVALIDATION_MAX_SPREAD_ATR", 0.35, lo=0.0, hi=10.0)
                    spread_ok = spread_atr <= max_spread_atr
                    trend_state = (
                        f"M15_invalid={int(m15_invalid)} {m15_state}; "
                        f"M5_invalid={int(m5_invalid)} {m5_state}; "
                        f"loss_ok={int(loss_ok)} profit={float(pos.profit or 0.0):.2f} min_loss={min_loss:.2f}; "
                        f"spread_ok={int(spread_ok)} spread_atr={spread_atr:.4f} max={max_spread_atr:.4f}"
                    )
                    if m5_invalid and loss_ok and spread_ok:
                        return self._close_position_for_monitor(
                            pos,
                            canonical,
                            f"trend invalidated: {trend_state}",
                            exit_source="TREND_INVALIDATION",
                        )

        _ss.set_status(
            "last_open_monitor_check",
            f"{canonical}:{pos.ticket}: age={age_text} session={session_state} news={news_state} trend={trend_state}",
        )
        return False

    def _manage_open_position(
        self,
        pos: MT5PositionView,
        canonical: str,
        market: str,
        protected: tuple[float, float] | None = None,
    ) -> tuple[float, float] | None:
        if not _env_bool("MT5_ACTIVE_TRADE_MANAGEMENT", True):
            return None
        direction = str(pos.direction or "").upper()
        if direction not in {"BUY", "SELL"}:
            return None

        current_sl = float(protected[0] if protected else pos.sl or 0.0)
        current_tp = float(protected[1] if protected else pos.tp or 0.0)
        if current_sl <= 0 or current_tp <= 0:
            return None

        meta = self.open_trades.get(str(pos.ticket), {})
        now = time.time()
        min_seconds = _env_int("MT5_TRADE_MANAGER_MIN_SECONDS", 20, lo=1)
        try:
            last_ts = float(meta.get("last_trade_manager_ts") or 0.0)
        except Exception:
            last_ts = 0.0
        if last_ts > 0 and now - last_ts < min_seconds:
            return None

        try:
            spec = self.gateway.symbol_info(pos.symbol)
            _, tick = self.gateway.symbol_tick(pos.symbol)
        except Exception as exc:
            _ss.set_status("last_trade_manager_error", f"{canonical}:{pos.ticket}: tick/spec unavailable {str(exc)[:120]}")
            return None

        bid = float(getattr(tick, "bid", 0.0) or 0.0)
        ask = float(getattr(tick, "ask", 0.0) or 0.0)
        if bid <= 0 or ask <= 0 or ask < bid:
            return None

        entry = float(pos.price_open or 0.0)
        current_exit = bid if direction == "BUY" else ask
        if entry <= 0 or current_exit <= 0:
            return None

        atr = self._recent_atr(canonical)
        if atr <= 0:
            atr = abs(float(pos.price_current or current_exit) - entry)
        risk_distance = abs(entry - current_sl)
        if risk_distance <= 0 and atr > 0:
            risk_distance = _compute_stop_distance(entry, atr, market=market, is_crypto=(market == "crypto"))
        if risk_distance <= 0:
            return None

        profit_distance = (current_exit - entry) if direction == "BUY" else (entry - current_exit)
        current_r = profit_distance / risk_distance if risk_distance > 0 else 0.0
        try:
            max_r = max(float(meta.get("max_r_seen") or 0.0), current_r)
        except Exception:
            max_r = current_r
        if meta and max_r > float(meta.get("max_r_seen") or -999):
            meta["max_r_seen"] = round(max_r, 4)

        _ss.set_status("last_trade_manager_check", f"{canonical}:{pos.ticket}: r={current_r:.2f} max_r={max_r:.2f}")
        if profit_distance <= 0:
            return None

        point = float(spec.point or 0.0) or 0.00001
        spread = max(0.0, ask - bid)
        min_dist = max(float(getattr(spec, "stops_level", 0) or 0) * point, point)

        if _env_bool("MT5_SCALP_CLOSE_ENABLE", True):
            close_r = self._management_rr_for_position(canonical, market, direction, meta)
            min_profit = _env_float("MT5_SCALP_CLOSE_MIN_PROFIT_USD", 0.0, lo=0.0)
            if close_r > 0 and current_r >= close_r and float(pos.profit or 0.0) >= min_profit:
                self._close_position_for_profit(pos, canonical, f"scalp close r={current_r:.2f} >= {close_r:.2f}")
                return None

        if _env_bool("MT5_SCALP_TP_ENABLE", True):
            target_r = self._management_rr_for_position(canonical, market, direction, meta)
            if target_r > 0:
                raw_tp = entry + (risk_distance * target_r) if direction == "BUY" else entry - (risk_distance * target_r)
                scalp_tp = self.gateway.normalize_price(spec, raw_tp)
                tighter_tp = (
                    direction == "BUY" and scalp_tp > entry and (current_tp <= 0 or scalp_tp < current_tp)
                ) or (
                    direction == "SELL" and scalp_tp < entry and (current_tp <= 0 or scalp_tp > current_tp)
                )
                if tighter_tp:
                    if direction == "BUY" and scalp_tp <= ask + min_dist:
                        self._close_position_for_profit(pos, canonical, f"scalp TP reached {target_r:.2f}R")
                        return None
                    if direction == "SELL" and scalp_tp >= bid - min_dist:
                        self._close_position_for_profit(pos, canonical, f"scalp TP reached {target_r:.2f}R")
                        return None
                    return self._apply_position_modify(pos, canonical, current_sl, scalp_tp, atr, f"scalp TP cap {target_r:.2f}R")

        buffer_points = _env_int("MT5_BREAKEVEN_BUFFER_POINTS", 2, lo=0)
        be_buffer = max(spread, point * buffer_points)
        candidates: list[tuple[float, str]] = []

        if _env_bool("MT5_BREAKEVEN_ENABLE", True):
            trigger_r = self._profile_cap_float(canonical, "breakeven_trigger_r", _env_float("MT5_BREAKEVEN_TRIGGER_R", 0.50, lo=0.0))
            if current_r >= trigger_r:
                be_sl = entry + be_buffer if direction == "BUY" else entry - be_buffer
                candidates.append((be_sl, f"breakeven r={current_r:.2f}"))

        if _env_bool("MT5_ATR_TRAILING_ENABLE", True):
            trail_start_r = self._profile_cap_float(canonical, "trail_start_r", _env_float("MT5_TRAIL_START_R", 0.80, lo=0.0))
            if current_r >= trail_start_r and atr > 0:
                trail_mult = _env_float("MT5_TRAIL_ATR_MULT", 1.20, lo=0.1)
                trail_dist = max(atr * trail_mult, spread + point)
                trail_sl = current_exit - trail_dist if direction == "BUY" else current_exit + trail_dist
                candidates.append((trail_sl, f"atr trail r={current_r:.2f} atr={atr:.5f}"))

        if _env_bool("MT5_GIVEBACK_GUARD_ENABLE", True):
            max_r_trigger = _env_float("MT5_GIVEBACK_TRIGGER_R", 1.00, lo=0.0)
            giveback_r = _env_float("MT5_GIVEBACK_MAX_R", 0.45, lo=0.0)
            if max_r >= max_r_trigger and max_r - current_r >= giveback_r:
                guard_sl = entry + be_buffer if direction == "BUY" else entry - be_buffer
                candidates.append((guard_sl, f"giveback guard max_r={max_r:.2f} r={current_r:.2f}"))

        if not candidates:
            if meta and "max_r_seen" in meta:
                self._save_state()
            return None

        normalized: list[tuple[float, str]] = []
        for raw_sl, reason in candidates:
            sl = self.gateway.normalize_price(spec, raw_sl)
            if self._is_more_protective_sl(direction, sl, current_sl):
                valid, valid_reason = self._managed_sl_is_valid(direction, sl, spec, bid, ask)
                if valid:
                    normalized.append((sl, reason))
                else:
                    _ss.set_status("last_trade_manager_skip", f"{canonical}:{pos.ticket}: {valid_reason}")

        if not normalized:
            if meta and "max_r_seen" in meta:
                self._save_state()
            return None

        selected_sl, selected_reason = (
            max(normalized, key=lambda item: item[0]) if direction == "BUY"
            else min(normalized, key=lambda item: item[0])
        )
        min_step_points = _env_int("MT5_TRADE_MANAGER_MIN_STEP_POINTS", 5, lo=0)
        min_step = max(point * min_step_points, point)
        if current_sl > 0 and abs(selected_sl - current_sl) < min_step:
            return None

        return self._apply_position_modify(pos, canonical, selected_sl, current_tp, atr, selected_reason)

    def _recent_atr(self, sym: str, timeframe: str = "M15", period: int = 14) -> float:
        try:
            df = self.gateway.rates(sym, timeframe, max(period + 2, 40))
        except Exception:
            return 0.0
        if df is None or len(df) < 2:
            return 0.0
        prev_close = df["Close"].shift(1)
        tr = pd.concat(
            [
                df["High"] - df["Low"],
                (df["High"] - prev_close).abs(),
                (df["Low"] - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        value = float(tr.tail(period).mean() or 0.0)
        return value if math.isfinite(value) else 0.0

    def _deal_component(self, deal, name: str, default: float = 0.0) -> float:
        try:
            value = getattr(deal, name, default)
            return float(value if value is not None else default)
        except Exception:
            return default

    def _deal_net_profit(self, deal) -> float:
        profit = self._deal_component(deal, "profit")
        swap = self._deal_component(deal, "swap")
        commission = self._deal_component(deal, "commission")
        return profit + swap + commission

    def _mark_closed_pending_deal_recon(self, ticket: str, meta: dict, reason: str) -> None:
        now_iso = datetime.now().isoformat()
        meta["closed_pending_deal_recon"] = True
        meta.setdefault("closed_pending_deal_recon_since", now_iso)
        meta["closed_pending_deal_recon_reason"] = reason
        _ss.set_status("last_closed_pending_deal_recon", f"{meta.get('sym')}:{ticket}: {reason}")
        try:
            conn = _ss.get_conn()
            conn.execute(
                """
                UPDATE trades
                SET status=?, exit_reason=COALESCE(exit_reason, ?)
                WHERE trade_id=? AND closed_at IS NULL
                """,
                ("CLOSED_PENDING_DEAL_RECON", reason, str(meta.get("trade_id") or "")),
            )
            conn.commit()
            conn.close()
        except Exception as exc:
            _ss.set_status("closed_pending_deal_recon_error", str(exc)[:180])

    def _reconcile_closed_positions(self, live_tickets: set[str] | None = None, reconcile_history: bool = True) -> None:
        if live_tickets is None:
            live_tickets = {str(p.ticket) for p in self.gateway.positions()}
        tracked = list(self.open_trades.items())
        for ticket, meta in tracked:
            if ticket in live_tickets:
                continue
            live_pos = self._live_position_for_symbol_once(str(meta.get("sym", "")))
            if live_pos is not None:
                self.open_trades[str(live_pos.ticket)] = meta
                self.open_trades.pop(ticket, None)
                continue
            if str(ticket).startswith("pending_") and self._pending_ticket_still_fresh(meta):
                continue
            deals = self.gateway.deals_for_position(int(ticket)) if str(ticket).isdigit() else []
            if len(deals) < 2:
                self._mark_closed_pending_deal_recon(str(ticket), meta, "close deal missing from MT5 history")
                continue
            close_price = float(meta.get("entry", 0.0))
            last = deals[-1]
            close_price = float(getattr(last, "price", close_price) or close_price)
            realized = round(sum(self._deal_net_profit(deal) for deal in deals), 2)
            outcome = "breakeven" if abs(realized) <= 0.000001 else ("win" if realized > 0 else "loss")
            market = str(meta.get("market") or self._market_for_symbol(str(meta["sym"])))
            exit_source = self._infer_exit_source_from_prices(meta, close_price)
            exit_reason = str(meta.get("exit_reason") or meta.get("monitor_close_reason") or meta.get("profit_close_reason") or meta.get("risk_close_reason") or "mt5 position no longer live")
            result_r = self._trade_result_r(realized, float(meta.get("entry") or 0.0), float(meta.get("sl") or 0.0), float(meta.get("qty") or 0.0), market, meta.get("risk_meta") if isinstance(meta.get("risk_meta"), dict) else None)
            _ss.close_trade(
                trade_id=meta["trade_id"],
                exit_px=close_price,
                realized=realized,
                outcome=outcome,
                closed_at=datetime.now().isoformat(),
                sym=meta["sym"],
                market=market,
                direction=meta["direction"],
                qty=meta["qty"],
                entry=meta["entry"],
                sl=meta["sl"],
                tp=meta["tp"],
                atr=meta["atr"],
                sig_mode=meta.get("sig_mode"),
                opened_at=meta.get("opened_at"),
                trade_date=meta.get("trade_date"),
                status="closed",
                result_r=result_r,
                result_ccy=realized,
                commission=round(sum(self._deal_component(deal, "commission") for deal in deals), 2),
                exit_reason=f"exit_source={exit_source}; {exit_reason}",
            )
            self._record_symbol_close(str(meta["sym"]), datetime.now().isoformat(), outcome, realized)
            if self.systematic is not None:
                try:
                    trade_audit = meta.get("trade_audit") if isinstance(meta.get("trade_audit"), dict) else {}
                    fake_sig = {
                        "symbol": self._canonical_from_resolved(str(meta.get("sym") or "")),
                        "engine": (trade_audit.get("score_breakdown") or {}).get("engine") if isinstance(trade_audit.get("score_breakdown"), dict) else "",
                        "strategy": meta.get("sig_mode"),
                        "direction": meta.get("direction"),
                    }
                    risk_per_trade = abs(float(meta.get("entry") or 0.0) - float(meta.get("sl") or 0.0)) or 1.0
                    r_result = float(realized or 0.0) / max(abs(risk_per_trade * float(meta.get("qty") or 1.0)), 1e-9)
                    self.systematic.analytics.record_closed_trade(**self._systematic_csv_row(str(meta["sym"]), market, fake_sig, "CLOSED", outcome, profit_usd=realized, r_result=r_result))
                except Exception as exc:
                    _ss.set_status("systematic_closed_csv_error", str(exc)[:180])
            _ss.remove_position(meta["sym"])
            self.open_trades.pop(ticket, None)
        if reconcile_history:
            self._reconcile_mt5_deal_history(live_tickets)
        self._save_state()

    def _reconcile_mt5_deal_history(self, live_tickets: set[str]) -> None:
        current_live_tickets = {str(ticket) for ticket in (live_tickets or set())}
        try:
            current_live_tickets.update(str(pos.ticket) for pos in self.gateway.positions())
        except Exception as exc:
            _ss.set_status("last_mt5_live_ticket_refresh_error", str(exc)[:180])
        if hasattr(self.gateway, "deals_history_path"):
            deals_path = self.gateway.deals_history_path()
        else:
            deals_path = Path(self.runtime.bridge_dir) / "deals.csv"
        if not deals_path.exists():
            return
        try:
            df = pd.read_csv(deals_path, dtype=str)
        except Exception as exc:
            _ss.set_status("last_mt5_deal_reconcile_error", f"read deals.csv failed: {str(exc)[:180]}")
            return
        required = {"position_id", "symbol", "price", "profit", "time"}
        if df.empty or not required.issubset(set(df.columns)):
            return

        def safe_float(value, default: float = 0.0) -> float:
            try:
                if pd.isna(value):
                    return default
                return float(value)
            except Exception:
                return default

        def safe_int(value, default: int = 0) -> int:
            try:
                if pd.isna(value):
                    return default
                return int(float(value))
            except Exception:
                return default

        groups: dict[str, list[dict]] = {}
        for raw in df.to_dict("records"):
            pid = str(raw.get("position_id") or "").strip()
            if not pid or pid == "0":
                continue
            profit = safe_float(raw.get("profit"))
            swap = safe_float(raw.get("swap"))
            commission = safe_float(raw.get("commission"))
            row = {
                "position_id": pid,
                "symbol": str(raw.get("symbol") or "").strip(),
                "price": safe_float(raw.get("price")),
                "profit": profit,
                "swap": swap,
                "commission": commission,
                "net_profit": safe_float(raw.get("net_profit"), profit + swap + commission),
                "time": safe_int(raw.get("time")),
            }
            groups.setdefault(pid, []).append(row)

        closed_groups: list[dict] = []
        for pid, rows in groups.items():
            rows = sorted(rows, key=lambda item: item["time"])
            if len(rows) < 2 or pid in current_live_tickets:
                continue
            entry_row = rows[0]
            close_row = rows[-1]
            if close_row["time"] <= 0:
                continue
            symbol = close_row["symbol"] or entry_row["symbol"]
            canonical = self._canonical_from_resolved(symbol)
            entry = float(entry_row["price"] or 0.0)
            exit_px = float(close_row["price"] or entry)
            realized = round(sum(float(row.get("net_profit") or 0.0) for row in rows), 2)
            profit = round(sum(float(row.get("profit") or 0.0) for row in rows), 2)
            swap = round(sum(float(row.get("swap") or 0.0) for row in rows), 2)
            commission = round(sum(float(row.get("commission") or 0.0) for row in rows), 2)
            closed_at = datetime.utcfromtimestamp(close_row["time"]).isoformat()
            opened_at = datetime.utcfromtimestamp(entry_row["time"]).isoformat() if entry_row["time"] > 0 else None
            closed_groups.append({
                "pid": pid,
                "symbol": symbol,
                "canonical": canonical,
                "market": self._market_for_symbol(canonical),
                "entry": entry,
                "exit_px": exit_px,
                "profit": profit,
                "swap": swap,
                "commission": commission,
                "realized": realized,
                "outcome": "breakeven" if abs(realized) <= 0.000001 else ("win" if realized > 0 else "loss"),
                "opened_at": opened_at,
                "closed_at": closed_at,
                "direction": self._infer_direction_from_deal(entry, exit_px, realized),
            })

        if not closed_groups:
            return

        conn = _ss.get_conn()
        try:
            live_rows = [dict(row) for row in conn.execute(
                """
                SELECT *
                FROM trades
                WHERE closed_at IS NULL
                  AND lower(COALESCE(status, '')) IN ('live', 'closed_pending_deal_recon')
                """
            ).fetchall()]
            closed_rows = [dict(row) for row in conn.execute(
                """
                SELECT *
                FROM trades
                WHERE closed_at IS NOT NULL
                """
            ).fetchall()]
        finally:
            conn.close()

        ticket_to_trade_id = {
            str(ticket): str(meta.get("trade_id") or "")
            for ticket, meta in self.open_trades.items()
            if isinstance(meta, dict) and meta.get("trade_id")
        }
        live_by_trade_id = {str(row.get("trade_id") or ""): row for row in live_rows}
        used_live_ids: set[int] = set()
        used_closed_ids: set[int] = set()
        matched = 0
        inserted = 0
        skipped_existing = 0
        state_changed = False

        for group in sorted(closed_groups, key=lambda item: item["closed_at"]):
            live_row = self._match_live_trade_to_mt5_group(group, live_rows, live_by_trade_id, ticket_to_trade_id, used_live_ids)
            if live_row is not None:
                used_live_ids.add(int(live_row.get("id") or 0))
                self._close_dashboard_trade_from_mt5_group(str(live_row.get("trade_id") or ""), group, live_row)
                matched += 1
                state_changed = self._remove_open_trade_by_trade_id(str(live_row.get("trade_id") or "")) or state_changed
                _ss.remove_position(str(live_row.get("sym") or group["canonical"]))
                continue
            if self._mt5_group_already_recorded(group, closed_rows):
                skipped_existing += 1
                continue
            closed_row = self._match_closed_trade_to_mt5_group(group, closed_rows, used_closed_ids)
            if closed_row is not None:
                used_closed_ids.add(int(closed_row.get("id") or 0))
                self._close_dashboard_trade_from_mt5_group(str(closed_row.get("trade_id") or ""), group, closed_row)
                matched += 1
                continue
            trade_id = f"mt5_reconciled_{group['canonical']}_{group['pid']}"
            self._close_dashboard_trade_from_mt5_group(trade_id, group, None)
            inserted += 1

        if state_changed:
            self._save_state()
        if matched or inserted or skipped_existing:
            _ss.set_status(
                "last_mt5_deal_reconcile",
                f"matched={matched} inserted={inserted} already_recorded={skipped_existing}",
            )

    def _infer_direction_from_deal(self, entry: float, exit_px: float, realized: float) -> str:
        if abs(realized) <= 0.000001 or abs(exit_px - entry) <= 0.000001:
            return ""
        if exit_px > entry:
            return "BUY" if realized > 0 else "SELL"
        return "SELL" if realized > 0 else "BUY"

    def _deal_match_tolerance(self, price: float) -> float:
        return max(abs(float(price or 0.0)) * 0.0002, 0.05)

    def _mt5_group_already_recorded(self, group: dict, closed_rows: list[dict]) -> bool:
        entry_tol = self._deal_match_tolerance(group["entry"])
        exit_tol = self._deal_match_tolerance(group["exit_px"])
        for row in closed_rows:
            if self._canonical_from_resolved(str(row.get("sym") or "")) != group["canonical"]:
                continue
            if abs(float(row.get("realized") or 0.0) - float(group["realized"] or 0.0)) > 0.05:
                continue
            if abs(float(row.get("entry") or 0.0) - group["entry"]) > entry_tol:
                continue
            if abs(float(row.get("exit_px") or 0.0) - group["exit_px"]) > exit_tol:
                continue
            return True
        return False

    def _match_live_trade_to_mt5_group(
        self,
        group: dict,
        live_rows: list[dict],
        live_by_trade_id: dict[str, dict],
        ticket_to_trade_id: dict[str, str],
        used_live_ids: set[int],
    ) -> dict | None:
        trade_id = ticket_to_trade_id.get(str(group["pid"]))
        if trade_id and trade_id in live_by_trade_id:
            return live_by_trade_id[trade_id]
        for row in live_rows:
            row_id = int(row.get("id") or 0)
            if row_id in used_live_ids:
                continue
            row_trade_id = str(row.get("trade_id") or "")
            if row_trade_id.endswith(f"_{group['pid']}"):
                return row
        protected_trade_ids = {str(value or "") for value in ticket_to_trade_id.values() if value}
        entry_tol = self._deal_match_tolerance(group["entry"])
        candidates: list[tuple[float, dict]] = []
        for row in live_rows:
            row_id = int(row.get("id") or 0)
            if row_id in used_live_ids:
                continue
            row_trade_id = str(row.get("trade_id") or "")
            if row_trade_id in protected_trade_ids:
                continue
            if self._canonical_from_resolved(str(row.get("sym") or "")) != group["canonical"]:
                continue
            row_direction = str(row.get("direction") or "").upper()
            if group["direction"] and row_direction and row_direction != group["direction"]:
                continue
            entry_diff = abs(float(row.get("entry") or 0.0) - group["entry"])
            if entry_diff > entry_tol:
                continue
            opened = _parse_dt(row.get("opened_at"))
            group_opened = _parse_dt(group.get("opened_at"))
            time_diff = abs((opened - group_opened).total_seconds()) if opened and group_opened else 0.0
            candidates.append((entry_diff + (time_diff / 100000.0), row))
        if not candidates:
            return None
        return min(candidates, key=lambda item: item[0])[1]

    def _match_closed_trade_to_mt5_group(self, group: dict, closed_rows: list[dict], used_closed_ids: set[int]) -> dict | None:
        entry_tol = self._deal_match_tolerance(group["entry"])
        candidates: list[tuple[float, dict]] = []
        for row in closed_rows:
            row_id = int(row.get("id") or 0)
            if row_id in used_closed_ids:
                continue
            if self._canonical_from_resolved(str(row.get("sym") or "")) != group["canonical"]:
                continue
            row_direction = str(row.get("direction") or "").upper()
            if group["direction"] and row_direction and row_direction != group["direction"]:
                continue
            entry_diff = abs(float(row.get("entry") or 0.0) - group["entry"])
            if entry_diff > entry_tol:
                continue
            opened = _parse_dt(row.get("opened_at"))
            group_opened = _parse_dt(group.get("opened_at"))
            time_diff = abs((opened - group_opened).total_seconds()) if opened and group_opened else 0.0
            realized_diff = abs(float(row.get("realized") or 0.0) - float(group["realized"] or 0.0))
            candidates.append((entry_diff + (time_diff / 100000.0) + (realized_diff / 1000.0), row))
        if not candidates:
            return None
        return min(candidates, key=lambda item: item[0])[1]

    def _remove_open_trade_by_trade_id(self, trade_id: str) -> bool:
        changed = False
        for ticket, meta in list(self.open_trades.items()):
            if isinstance(meta, dict) and str(meta.get("trade_id") or "") == trade_id:
                self.open_trades.pop(ticket, None)
                changed = True
        return changed

    def _infer_qty_from_deal_group(self, group: dict) -> float:
        move = abs(float(group["exit_px"] or 0.0) - float(group["entry"] or 0.0))
        if move <= 0 or abs(float(group["realized"] or 0.0)) <= 0:
            return 0.0
        try:
            spec = self.gateway.symbol_info(str(group.get("symbol") or group["canonical"]))
            contract_size = float(spec.contract_size or 0.0)
        except Exception:
            contract_size = 0.0
        if contract_size <= 0:
            return 0.0
        qty = abs(float(group["realized"])) / (move * contract_size)
        return round(qty, 4)

    def _close_dashboard_trade_from_mt5_group(self, trade_id: str, group: dict, live_row: dict | None) -> None:
        qty = float(live_row.get("qty") or 0.0) if live_row else self._infer_qty_from_deal_group(group)
        direction = str((live_row or {}).get("direction") or group.get("direction") or "")
        close_meta = dict(live_row or {})
        close_meta.setdefault("entry", float((live_row or {}).get("entry") or group["entry"]))
        close_meta.setdefault("sl", float((live_row or {}).get("sl") or 0.0))
        close_meta.setdefault("tp", float((live_row or {}).get("tp") or 0.0))
        close_meta.setdefault("atr", float((live_row or {}).get("atr") or 0.0))
        exit_source = self._infer_exit_source_from_prices(close_meta, float(group["exit_px"]))
        result_r = self._trade_result_r(float(group.get("realized") or 0.0), float(close_meta.get("entry") or 0.0), float(close_meta.get("sl") or 0.0), qty, str((live_row or {}).get("market") or group["market"]), close_meta.get("risk_meta") if isinstance(close_meta.get("risk_meta"), dict) else None)
        _ss.close_trade(
            trade_id=trade_id,
            exit_px=group["exit_px"],
            realized=group["realized"],
            outcome=group["outcome"],
            closed_at=group["closed_at"],
            sym=str((live_row or {}).get("sym") or group["canonical"]),
            market=str((live_row or {}).get("market") or group["market"]),
            direction=direction,
            qty=qty,
            entry=float((live_row or {}).get("entry") or group["entry"]),
            sl=float((live_row or {}).get("sl") or 0.0),
            tp=float((live_row or {}).get("tp") or 0.0),
            atr=float((live_row or {}).get("atr") or 0.0),
            sig_mode=str((live_row or {}).get("sig_mode") or "mt5_reconciled"),
            opened_at=str((live_row or {}).get("opened_at") or group.get("opened_at") or ""),
            trade_date=str((live_row or {}).get("trade_date") or ""),
            status="closed",
            result_r=result_r,
            result_ccy=group["realized"],
            commission=float(group.get("commission") or 0.0),
            exit_reason=f"exit_source={exit_source}; mt5 history reconciliation",
        )
        self._record_symbol_close(
            str((live_row or {}).get("sym") or group["canonical"]),
            group["closed_at"],
            str(group.get("outcome") or ""),
            float(group.get("realized") or 0.0),
        )
        if self.systematic is not None:
            try:
                fake_sig = {
                    "symbol": str((live_row or {}).get("sym") or group["canonical"]),
                    "engine": "",
                    "strategy": str((live_row or {}).get("sig_mode") or "mt5_reconciled"),
                    "direction": direction,
                }
                r_result = result_r
                self.systematic.analytics.record_closed_trade(**self._systematic_csv_row(str((live_row or {}).get("sym") or group["canonical"]), str((live_row or {}).get("market") or group["market"]), fake_sig, "CLOSED", str(group.get("outcome") or ""), profit_usd=float(group.get("realized") or 0.0), r_result=r_result))
            except Exception as exc:
                _ss.set_status("systematic_closed_csv_error", str(exc)[:180])

    def _pending_ticket_still_fresh(self, meta: dict) -> bool:
        try:
            opened = datetime.fromisoformat(str(meta.get("opened_at")))
        except Exception:
            return False
        return (datetime.now() - opened).total_seconds() < 180

    def _live_position_for_symbol_once(self, sym: str) -> MT5PositionView | None:
        canonical = self._canonical_from_resolved(sym)
        for pos in self.gateway.positions():
            if self._canonical_from_resolved(pos.symbol) == canonical:
                return pos
        return None

    def _scan_priority_symbols(self) -> None:
        self._monitor_pending_setups()

    def _monitor_pending_setups(self) -> None:
        now = datetime.utcnow()
        self.cleanup_expired_pending_setups(now, publish_debug=True)
        pending_items = [
            (str(key), setup)
            for key, setup in list(self.pending_setups.items())
            if isinstance(setup, dict) and not self._pending_setup_is_terminal(setup)
        ]
        checked = waiting = confirmed = executed = cancelled = 0
        _ss.set_status("pending_monitor_symbol_count", len(pending_items))
        _ss.set_status("scan_counter_pending_active", len(pending_items))
        if not pending_items:
            _ss.set_status("last_pending_monitor", datetime.now().isoformat())
            _ss.set_status("last_pending_monitor_result", "no pending setups")
            print(f"[MT5_PENDING_MONITOR] ruleset_version={RULESET_VERSION} checked=0 waiting=0 confirmed=0 executed=0 cancelled=0", flush=True)
            return
        strategy_equity = None
        for pending_key, pending in pending_items:
            checked += 1
            pending_key, pending = self._normalize_pending_setup_key(pending_key, pending, datetime.utcnow())
            canonical = canonical_symbol(str(pending.get("symbol") or pending_key.split(":")[0])).upper()
            cfg = get_symbol_config(canonical) or {}
            market = str(pending.get("market") or self._market_for_symbol(canonical))
            engine = str(pending.get("engine") or get_engine_for_symbol(canonical))
            direction = str(pending.get("side") or pending.get("direction") or "").upper()
            engine_context = pending.get("engine_context") if isinstance(pending.get("engine_context"), dict) else {}
            sig = {
                "symbol": canonical, "market": market, "direction": direction,
                "pending_key": pending_key, "setup_id": pending.get("setup_id"),
                "pending_setup_status": self._pending_setup_status(pending),
                "price": float(pending.get("price") or 0.0), "atr": float(pending.get("atr") or 0.0),
                "engine": engine, "asset_class": str(pending.get("asset_class") or cfg.get("asset_class") or market or ""),
                "strategy_before": str(pending.get("strategy_before") or ""),
                "strategy_after": str(pending.get("strategy_after") or pending.get("strategy") or ""),
                "strategy": str(pending.get("strategy") or pending.get("strategy_after") or ""),
                "regime": str(engine_context.get("regime") or pending.get("regime") or ""),
                "h1_bias": engine_context.get("h1_bias"),
                "m15_structure": engine_context.get("m15_structure"),
                "session_score": engine_context.get("session_score"),
                "volatility_expansion": engine_context.get("volatility_expansion"),
                "atr_expansion": engine_context.get("atr_expansion"),
                "engine_context": engine_context,
                "score_before": float(pending.get("score_before") or engine_context.get("score_before") or 0.0),
                "score_after": float(pending.get("score_after") or pending.get("score") or 0.0),
                "score": float(pending.get("score_after") or pending.get("score") or 0.0),
                "min_score": float(pending.get("min_score") or cfg.get("min_score") or 0.0),
                "pending_floor": float(pending.get("pending_floor") or pending_floor_for_symbol(canonical, float(pending.get("min_score") or cfg.get("min_score") or 0.0), engine)),
                "require_stronger_1m_confirmation": bool(pending.get("require_stronger_1m_confirmation")),
                "adx": float(pending.get("adx") or 0.0), "adx_reason": str(pending.get("adx_reason") or ""),
                "score_function": str(pending.get("score_function") or ""),
                "adx_function": str(pending.get("adx_function") or ""),
                "spread_function": str(pending.get("spread_function") or ""),
                "conditions": {"scheduler_loop": "pending_monitor"},
                "decision_trace_id": self._new_decision_trace_id(canonical), "_decision_trace": [],
                "generated_at": datetime.utcnow().isoformat(), "source_loop_name": "pending_monitor",
                "direction_source": str(engine_context.get("direction_source") or (f"{engine}_M5_TRIGGER" if engine else "PENDING_SETUP")),
                "evaluate_probability_master_gate": False,
                "replacement_strategy_v1": bool(pending.get("replacement_strategy_v1") or engine_context.get("replacement_strategy_v1")),
                "ruleset_version": RULESET_VERSION, "build_id": BUILD_ID,
                "scanner_process_started_at": self.scanner_process_started_at,
                "row_source": "current", "state": PENDING, "final_status": PENDING,
            }
            self._trace_step(sig, "PENDING_MONITOR_STARTED", "PASS", "pending monitor checked existing setup", source="mt5_bot._monitor_pending_setups")
            ok, reason = self._pending_setup_allows_execution(canonical, market, sig, {}, pending_key=pending_key, pending_setup=pending)
            if not ok:
                if sig.get("final_status") == CANCELLED:
                    cancelled += 1
                else:
                    waiting += 1
                self._audit_decision({"event": "pending_monitor", "loop": "pending_monitor", "status": sig.get("final_status") or PENDING, "symbol": canonical, "engine": engine, "reason": reason, "ruleset_version": RULESET_VERSION})
                continue
            min_score = float(sig.get("min_score") or cfg.get("min_score") or 0.0)
            pending_floor = self._effective_pending_floor(canonical, min_score, engine)
            setup_state = str(pending.get("state") or sig.get("state") or "").upper()
            soft_setup = bool(pending.get("require_stronger_1m_confirmation")) or setup_state == PENDING_SOFT or float(sig.get("score") or 0.0) < min_score
            strong_1m_required = _env_float("MT5_PENDING_SOFT_1M_CONFIRM_SCORE", 90.0, lo=0.0, hi=100.0)
            confirm_score = float(sig.get("execution_1m_score") or 0.0)
            sig["pending_floor"] = pending_floor
            sig.setdefault("_mt5_thresholds", {})["pending_quality"] = {"min_score": min_score, "pending_floor": pending_floor, "soft_setup": bool(soft_setup), "execution_1m_score": confirm_score, "strong_1m_required": strong_1m_required}
            if self._demo_winrate_test_effective() and soft_setup:
                strong_1m_required = max(0.0, strong_1m_required - 5.0)
                sig["why_trade_allowed_in_demo"] = "demo win-rate mode uses relaxed PENDING_SOFT 1M confirmation threshold"
                sig.setdefault("_mt5_thresholds", {}).setdefault("demo_winrate", {})["strong_1m_required_relaxed"] = strong_1m_required
            if soft_setup and confirm_score < strong_1m_required:
                warning = f"PENDING_SOFT confirmation score below advisory target ({confirm_score:.0f} < {strong_1m_required:.0f}); strict directional confirmation already passed"
                sig["pending_soft_confirmation_warning"] = warning
                self._audit_decision({
                    "event": "PENDING_SOFT_CONFIRMATION_WARNING",
                    "decision": "observe",
                    "scope": "setup",
                    "symbol": canonical,
                    "engine": engine,
                    "reason": warning,
                    "ruleset_version": RULESET_VERSION,
                })
            confirmed += 1
            price = float(sig.get("price") or 0.0)
            atr = float(sig.get("atr") or 0.0)
            stop_d = _compute_stop_distance(price, atr, market=market, vwap_stop_dist=0.0, is_crypto=(market == "crypto"))
            stop_d = self._cap_stop_distance_for_symbol(canonical, market, stop_d, sig)
            rr = self._entry_rr_for_symbol(canonical, market, sig, configured_rr=cfg.get("tp_r"))
            sl = price - stop_d if direction == "BUY" else price + stop_d
            tp = price + (stop_d * rr) if direction == "BUY" else price - (stop_d * rr)
            if strategy_equity is None:
                acct = self.gateway.account_info()
                self._assert_expected_account(acct)
                strategy_equity = self._strategy_equity(acct)
            volume, size_reason, risk_meta = self._mt5_volume_for_trade(canonical, market, price, stop_d, strategy_equity, sig)
            if volume <= 0:
                self._remove_pending_setup(pending_key, pending, "cancelled", "SETUP_CANCELLED", size_reason, sig); cancelled += 1
                self.analytics_engine.record(CANCELLED, canonical, engine, str(sig.get("strategy") or ""), float(sig.get("score") or 0.0), size_reason, sig)
                continue
            cost_ok, cost_reason, adjusted_score, cost_details = self.cost_model.check(canonical, market, sig, volume, sl, tp, risk_meta, stage="pre_order")
            self._trace_step(sig, "COST_GATE_APPLIED", "PASS" if cost_ok else "BLOCKED", cost_reason, cost=cost_details, cost_to_target_stage=cost_details.get("cost_to_target_stage"))
            if not cost_ok:
                self._remove_pending_setup(pending_key, pending, "cancelled", "SETUP_CANCELLED", cost_reason, sig); cancelled += 1
                self._mark_blocked(sig, "COST_GATE_APPLIED", cost_reason)
                self.analytics_engine.record(CANCELLED, canonical, engine, str(sig.get("strategy") or ""), float(sig.get("score") or 0.0), cost_reason, sig)
                continue
            net_ok, net_reason, net_details = self.aggressive_sizing_engine.check_expected_net_profit(canonical, market, sig, volume, price, tp, risk_meta, cost_details)
            self._trace_step(sig, "MIN_NET_PROFIT_FILTER", "PASS" if net_ok else "BLOCKED", net_reason, net_profit=net_details)
            if not net_ok:
                self._remove_pending_setup(pending_key, pending, "cancelled", "SETUP_CANCELLED", net_reason, sig); cancelled += 1
                self._mark_blocked(sig, "MIN_NET_PROFIT_FILTER", net_reason)
                self.analytics_engine.record(CANCELLED, canonical, engine, str(sig.get("strategy") or ""), float(sig.get("score") or 0.0), net_reason, sig)
                continue
            systematic_cost_ok, systematic_cost_reason = self._systematic_cost_allows_entry(canonical, market, sig, volume, sl, tp, risk_meta)
            if not systematic_cost_ok:
                self._remove_pending_setup(pending_key, pending, "cancelled", "SETUP_CANCELLED", systematic_cost_reason, sig); cancelled += 1
                self.analytics_engine.record(CANCELLED, canonical, engine, str(sig.get("strategy") or ""), float(sig.get("score") or 0.0), systematic_cost_reason, sig)
                continue
            risk_ok, risk_reason = self.portfolio_risk_manager.check(canonical, market, sig)
            if not risk_ok:
                self._remove_pending_setup(pending_key, pending, "cancelled", "SETUP_CANCELLED", risk_reason, sig); cancelled += 1
                self._mark_blocked(sig, "PORTFOLIO_RISK_GATE_APPLIED", risk_reason)
                self.analytics_engine.record(CANCELLED, canonical, engine, str(sig.get("strategy") or ""), float(sig.get("score") or 0.0), risk_reason, sig)
                continue
            self._trace_step(sig, "PORTFOLIO_RISK_GATE_APPLIED", "PASS", "PortfolioRiskManager allowed entry")
            entry_ok, entry_reason = self._execution_gate_allows_entry(canonical, market, sig, volume, sl, tp, risk_meta)
            if not entry_ok:
                self._remove_pending_setup(pending_key, pending, "cancelled", "SETUP_CANCELLED", entry_reason, sig); cancelled += 1
                self.analytics_engine.record(CANCELLED, canonical, engine, str(sig.get("strategy") or ""), float(sig.get("score") or 0.0), entry_reason, sig)
                continue
            eq_ok, eq_reason, eq_details = self.execution_quality_engine.pre_order_check(canonical, market, sig, volume, sl, tp)
            sig.setdefault("_mt5_thresholds", {})["execution_quality_engine"] = eq_details
            if not eq_ok:
                self._remove_pending_setup(pending_key, pending, "cancelled", "SETUP_CANCELLED", eq_reason, sig); cancelled += 1
                self._mark_blocked(sig, "ORDER_EXECUTION_CHECK", eq_reason)
                self.analytics_engine.record(CANCELLED, canonical, engine, str(sig.get("strategy") or ""), float(sig.get("score") or 0.0), eq_reason, sig)
                continue
            try:
                risk_meta = dict(risk_meta or {})
                risk_meta["pending_setup_id"] = pending.get("setup_id")
                risk_meta["pending_key"] = pending_key
                self._set_pending_setup_status(pending_key, pending, "order_sent", "ORDER_SENT", "MT5 order_send called", sig)
                order_result = self._place_trade(canonical, market, sig, volume, sl, tp, risk_meta)
                result_ok, result_reason, result_details = self.execution_quality_engine.confirm_order_result(canonical, sig, order_result)
                sig.setdefault("_mt5_thresholds", {})["mt5_order_result"] = result_details
                if result_ok:
                    executed += 1
                    sig["final_status"] = EXECUTED; sig["state"] = EXECUTED
                    self._remove_pending_setup(pending_key, pending, "entered", "ORDER_FILLED", result_reason, sig)
                    self.analytics_engine.record(EXECUTED, canonical, engine, str(sig.get("strategy") or ""), float(sig.get("score") or 0.0), result_reason, sig)
                else:
                    cancelled += 1
                    sig["final_status"] = CANCELLED; sig["state"] = CANCELLED
                    self._remove_pending_setup(pending_key, pending, "order_rejected", "ORDER_REJECTED", result_reason, sig)
                    self.analytics_engine.record(CANCELLED, canonical, engine, str(sig.get("strategy") or ""), float(sig.get("score") or 0.0), result_reason, sig)
            except Exception as exc:
                _ok, _reason, result_details = self.execution_quality_engine.confirm_order_result(canonical, sig, None, error=str(exc))
                sig.setdefault("_mt5_thresholds", {})["mt5_order_result"] = result_details
                self._record_rejected_trade(canonical, market, sig, volume, price, sl, tp, atr, str(exc))
                self._remove_pending_setup(pending_key, pending, "order_rejected", "ORDER_REJECTED", str(exc), sig); cancelled += 1
                self.analytics_engine.record(CANCELLED, canonical, engine, str(sig.get("strategy") or ""), float(sig.get("score") or 0.0), f"ORDER REJECTED: {str(exc)[:160]}", sig)
        result = f"ruleset_version={RULESET_VERSION} checked={checked} waiting={waiting} confirmed={confirmed} executed={executed} cancelled={cancelled}"
        _ss.set_status("last_pending_monitor", datetime.now().isoformat())
        _ss.set_status("last_pending_monitor_result", result)
        _ss.set_status("scan_counter_pending_active", len(self.pending_setups))
        _ss.set_status("scan_counter_confirmed", confirmed)
        _ss.set_status("scan_counter_executed", executed)
        _ss.set_status("scan_counter_cancelled", cancelled)
        print(f"[MT5_PENDING_MONITOR] {result}", flush=True)

    def _log_scan_block_reason(self, symbol: str, side: str = "", setup_id: str = "", reason: str = "", scope: str = "", sig: dict | None = None) -> None:
        canonical = canonical_symbol(self._canonical_from_resolved(symbol)).upper()
        safe_reason = str(reason or "BLOCKED").replace("\n", " ").replace("\r", " ")[:320]
        text_reason = safe_reason.lower()
        clean_side = str(side or (sig or {}).get("direction") or "").upper()
        clean_setup = str(setup_id or (sig or {}).get("setup_id") or "")
        scope = str(scope or "").lower()
        if not scope:
            if "symbol daily" in text_reason or "symbol cooldown" in text_reason or "symbol re-entry" in text_reason:
                scope = "symbol"
            elif any(token in text_reason for token in ("daily losing", "daily loss", "daily trade", "max open", "account", "terminal", "safety guard", "drawdown", "global")):
                scope = "global"
            elif any(token in text_reason for token in ("spread", "news", "market closed", "session")):
                scope = "symbol"
            else:
                scope = "setup"
        now = datetime.utcnow()
        expires_at = ""
        seconds_remaining = ""
        if scope == "setup":
            pending = None
            if isinstance((sig or {}).get("pending_setup"), dict):
                pending = (sig or {}).get("pending_setup")
            if pending is None and clean_setup:
                pending = next((item for item in self.pending_setups.values() if isinstance(item, dict) and str(item.get("setup_id") or "") == clean_setup), None)
            if isinstance(pending, dict):
                expires_at = self._pending_setup_expires_at(pending, now).isoformat()
                seconds_remaining = f"{max(0.0, self._pending_setup_seconds_until_expiry(pending, now)):.1f}"
        elif "cooldown" in text_reason:
            match = re.search(r"(\d+)\s*([sm])\s+left", text_reason)
            if match:
                seconds = int(match.group(1)) * (60 if match.group(2) == "m" else 1)
                seconds_remaining = str(seconds)
                expires_at = (now + timedelta(seconds=seconds)).isoformat()
        elif scope == "global" and any(token in text_reason for token in ("daily", "losing streak")):
            next_day = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            expires_at = next_day.isoformat()
            seconds_remaining = f"{max(0.0, (next_day - now).total_seconds()):.1f}"
        msg = (
            f"symbol={canonical} side={clean_side} setup_id={clean_setup} block_reason={safe_reason} "
            f"block_scope={scope} expires_at={expires_at} seconds_remaining={seconds_remaining}"
        )
        print(f"[MT5_BLOCK_REASON] {msg}", flush=True)
        _ss.set_status("last_block_reason", msg)
        self._audit_decision({
            "event": "scan_block_reason", "decision": "block", "symbol": canonical,
            "side": clean_side, "setup_id": clean_setup, "block_reason": safe_reason,
            "block_scope": scope, "expires_at": expires_at, "seconds_remaining": seconds_remaining,
        })

    def _log_global_scan_block(self, reason: str) -> None:
        seen = set()
        for group, symbols in self.symbol_groups.items():
            for symbol in symbols:
                canonical = canonical_symbol(self._canonical_from_resolved(symbol)).upper()
                if canonical in seen:
                    continue
                seen.add(canonical)
                self._log_scan_block_reason(canonical, reason=reason, scope="global")

    def _scan_and_trade(self) -> None:
        self.cleanup_expired_pending_setups(datetime.utcnow(), publish_debug=True)
        safety_ok, safety_reason = self._new_entries_allowed("full_scan")
        if not safety_ok:
            _ss.set_status("halt", safety_reason)
            self._log_global_scan_block(safety_reason)
            _ss.set_status("last_scan", datetime.now().isoformat())
            return

        acct = self.gateway.account_info()
        self._assert_expected_account(acct)
        if not _global_trading_enabled():
            reason = "global trading lock active"
            _ss.set_status("halt", reason)
            self._log_global_scan_block(reason)
            _ss.set_status("last_scan", datetime.now().isoformat())
            return
        allowed, reason = self._account_trading_allowed(acct)
        if not allowed:
            _ss.set_status("halt", reason)
            self._log_global_scan_block(reason)
            _ss.set_status("last_scan", datetime.now().isoformat())
            return
        if not self._daily_risk_allows_entry():
            reason = "daily loss limit reached"
            _ss.set_status("halt", reason)
            self._log_global_scan_block(reason)
            _ss.set_status("last_scan", datetime.now().isoformat())
            return
        if not self._daily_trade_count_allows_entry():
            reason = f"daily trade count limit reached ({self._daily_trade_count()}/{self._effective_max_daily_trades()})"
            self._log_global_scan_block(reason)
            _ss.set_status("last_scan", datetime.now().isoformat())
            return
        _, streak_reason = self._daily_loss_streak_allows_entry()
        _ss.set_status("loss_streak_status", streak_reason)

        strategy_equity = self._strategy_equity(acct)
        _ss.set_status("strategy_equity", strategy_equity)
        _ss.set_status("cap_usd", float(self.runtime.capital_cap_usd))
        _ss.set_status("max_open_trades", self._effective_max_open_trades())
        _ss.set_status("max_daily_trades", self._effective_max_daily_trades())
        _ss.set_status("halt", "")

        self._begin_scan_stats("full")
        try:
            for group, symbols in self.symbol_groups.items():
                for sym in symbols:
                    market = self._market_for_symbol(sym)
                    market_window_open = in_trade_window(market)
                    priority_symbol = self._is_priority_symbol(sym)
                    if not market_window_open and not (
                        priority_symbol and _env_bool("MT5_PRIORITY_SYMBOLS_IGNORE_WINDOWS", False)
                    ):
                        reason = "market_window_closed"
                        self._log_scan_block_reason(self._canonical_from_resolved(sym), reason=reason, scope="symbol")
                        if get_engine_for_symbol(self._canonical_from_resolved(sym).upper()) == INDEX_ENGINE:
                            self._record_index_market_window_closed_scan(sym, group, market)
                        continue
                    if not self._daily_trade_count_allows_entry():
                        self._log_global_scan_block(f"daily trade count limit reached ({self._daily_trade_count()}/{self._effective_max_daily_trades()})")
                        return
                    max_open = self._effective_max_open_trades()
                    if self._open_position_count() >= max_open:
                        reason = f"max open trades reached ({max_open})"
                        _ss.set_status("halt", reason)
                        self._log_global_scan_block(reason)
                        return
                    if self._symbol_has_open_position(sym) and not self._pyramiding_enabled():
                        continue
                    self._scan_symbol(sym, group, market, strategy_equity)
        finally:
            self._finish_scan_stats("full")
            _ss.set_status("last_scan", datetime.now().isoformat())

    def _broker_day_realized_pnl(self) -> float:
        try:
            return float(_ss.read_broker_day_pnl() or 0.0)
        except Exception:
            return float(_ss.read_todays_pnl() or 0.0)

    def _daily_risk_allows_entry(self) -> bool:
        if _env_bool("MT5_EA_CONTROLS_DAILY_RISK", False):
            _ss.set_status("daily_loss_limit_usd", "EA input")
            return True
        try:
            realized_today = self._broker_day_realized_pnl()
        except Exception:
            realized_today = 0.0
        try:
            acct = self.gateway.account_info()
        except Exception:
            acct = None
        loss_limit = self._daily_loss_limit_used(acct)
        override_limit = _env_float("MT5_DAILY_LOSS_OVERRIDE_USD", 0.0, lo=0.0)
        if override_limit > 0:
            loss_limit = min(loss_limit, override_limit)
        elif os.getenv("MT5_MAX_DAILY_LOSS_USD") is not None:
            absolute_limit = _env_float("MT5_MAX_DAILY_LOSS_USD", loss_limit, lo=0.0)
            if not self._demo_winrate_test_effective(acct):
                loss_limit = min(loss_limit, absolute_limit)
        _ss.set_status("daily_loss_limit_usd", round(abs(loss_limit), 2))
        _ss.set_status("daily_loss_limit_used", round(abs(loss_limit), 2))
        allowed = realized_today > -abs(loss_limit)
        if not allowed:
            _ss.set_status("halt", f"daily closed loss limit reached ({realized_today:.2f} <= -{abs(loss_limit):.2f})")
            self._audit_decision({
                "event": "daily_risk_gate",
                "decision": "block",
                "realized_today": realized_today,
                "loss_limit": abs(loss_limit),
                "reason": "daily closed loss limit reached",
            })
        return allowed

    def _daily_trade_count(self) -> int:
        try:
            return int(_ss.read_todays_trade_count() or 0)
        except Exception:
            return 0

    def _daily_trade_count_allows_entry(self) -> bool:
        count = self._daily_trade_count()
        limit = self._effective_max_daily_trades()
        _ss.set_status("daily_trade_count", count)
        _ss.set_status("max_daily_trades", limit)
        if count >= limit:
            self._audit_decision({
                "event": "daily_trade_count_gate",
                "decision": "block",
                "daily_trade_count": count,
                "max_daily_trades": limit,
                "count_semantics": "accepted bot entries in current broker day; adopted/manual and rejected rows excluded",
                "reason": "accepted-entry daily trade limit reached",
            })
            return False
        return True

    def _today_closed_trades(self, limit: int = 250) -> list[dict]:
        try:
            rows = _ss.read_trades(limit=limit, today_only=True)
        except Exception:
            return []
        return [dict(row) for row in rows]

    def _counts_as_loss_for_limits(self, row: dict, sym: str = "") -> bool:
        return str(row.get("outcome") or "").lower() == "loss"

    def _fresh_setup_allows_reentry(self, symbol_rows: list[dict], canonical: str) -> tuple[bool, str]:
        closed_times = [
            parsed
            for parsed in (_parse_dt(row.get("closed_at")) for row in symbol_rows)
            if parsed is not None
        ]
        if not closed_times:
            return True, "OK"
        newest = max(closed_times)
        elapsed_s = (datetime.utcnow() - newest).total_seconds()
        cooldown_s = float(SETUP_TIMEFRAME_MINUTES * 60)
        if elapsed_s < cooldown_s:
            remaining_s = max(1, math.ceil(cooldown_s - max(0.0, elapsed_s)))
            return False, (
                f"fresh M15 setup required after close "
                f"({canonical}: {math.ceil(remaining_s / 60)}m left)"
            )
        return True, "OK"

    def _daily_loss_streak(self) -> int:
        rows = sorted(
            self._today_closed_trades(),
            key=lambda row: str(row.get("closed_at") or row.get("opened_at") or ""),
            reverse=True,
        )
        streak = 0
        for row in rows:
            outcome = str(row.get("outcome") or "").lower()
            if outcome == "loss":
                if self._counts_as_loss_for_limits(row):
                    streak += 1
                    continue
                break
            if outcome in {"win", "breakeven", "flat"}:
                break
        return streak

    def _daily_loss_streak_allows_entry(self) -> tuple[bool, str]:
        limit = _env_int("MT5_MAX_DAILY_LOSING_STREAK", 3, lo=0)
        policy = str(os.getenv("MT5_DAILY_LOSS_STREAK_MODE", "observe_only") or "observe_only").strip().lower()
        policy = "observe_only"
        _ss.set_status("daily_loss_streak_policy", policy)
        if limit <= 0:
            return True, "OK; daily losing streak policy observe_only"
        streak = self._daily_loss_streak()
        _ss.set_status("daily_losing_streak", streak)
        _ss.set_status("max_daily_losing_streak", limit)
        self._audit_decision({
            "event": "LOSS_STREAK_OBSERVE_ONLY",
            "decision": "observe_only",
            "daily_losing_streak": streak,
            "max_daily_losing_streak": limit,
            "policy": policy,
            "reason": "daily losing streak is observe-only; global limits are max open trades and max daily trades",
        })
        return True, f"OK; daily losing streak observed ({streak}/{limit})"

    def _symbol_risk_allows_entry(self, sym: str, *, confirmed_pending: bool = False) -> tuple[bool, str]:
        canonical = self._canonical_from_resolved(sym).upper()
        rows = self._today_closed_trades()
        symbol_rows = [
            row for row in rows
            if self._canonical_from_resolved(str(row.get("sym") or "")).upper() == canonical
        ]
        loss_count = sum(
            1 for row in symbol_rows
            if self._counts_as_loss_for_limits(row, canonical)
        )
        _ss.set_status(f"symbol_loss_count_{canonical}", loss_count)
        self._audit_decision({
            "event": "SYMBOL_RISK_OBSERVE_ONLY",
            "decision": "observe_only",
            "scope": "symbol",
            "symbol": canonical,
            "loss_count": loss_count,
            "confirmed_pending": bool(confirmed_pending),
            "reason": "symbol loss caps, loss cooldowns, and symbol re-entry quality are not execution vetoes",
        })
        return True, "OK; symbol risk quality is observe-only"

    def _decision_audit_path(self) -> Path:
        configured = str(os.getenv("MT5_DECISION_AUDIT_LOG") or "").strip()
        if configured:
            return Path(configured)
        state_file = getattr(self, "state_file", None)
        if state_file:
            return Path(state_file).with_name("mt5_decision_audit.jsonl")
        return Path("mt5_decision_audit.jsonl")

    def _audit_decision(self, event: dict) -> None:
        try:
            payload = {
                "ts": datetime.utcnow().isoformat(),
                **dict(event or {}),
            }
            path = self._decision_audit_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, sort_keys=True, default=str) + "\n")
        except Exception:
            pass

    def _new_entries_allowed(self, context: str, sym: str = "", sig: dict | None = None) -> tuple[bool, str]:
        canonical = self._canonical_from_resolved(sym) if sym else ""
        if _mt5_safety_guards is None:
            reason = "MT5 safety guard module unavailable"
            _ss.set_status("halt", reason)
            self._audit_decision({
                "event": "safety_guard",
                "decision": "block",
                "context": context,
                "symbol": canonical,
                "reason": reason,
            })
            return False, reason
        try:
            allowed, reason = _mt5_safety_guards.new_entries_allowed()
        except Exception as exc:
            allowed, reason = False, f"MT5 safety guard failed: {str(exc)[:160]}"
        if not allowed:
            _ss.set_status("halt", reason)
            _ss.set_status("last_safety_guard", f"BLOCK {canonical or context}: {reason}")
            self._audit_decision({
                "event": "safety_guard",
                "decision": "block",
                "context": context,
                "symbol": canonical,
                "strategy": str((sig or {}).get("strategy") or ""),
                "score": float((sig or {}).get("score") or 0.0),
                "reason": reason,
            })
        return allowed, reason

    def _news_gate_allows_entry(self, sym: str, market: str, sig: dict) -> tuple[bool, str, dict]:
        canonical = self._canonical_from_resolved(sym)
        mode = str(os.getenv("MT5_NEWS_ENTRY_GATE_MODE", "audit") or "audit").strip().lower()
        if _mt5_news_gate is None:
            allowed = mode != "block"
            reason = "MT5 news gate module unavailable"
            details = {"mode": mode, "module_available": False}
        else:
            try:
                allowed, reason, details = _mt5_news_gate.check_entry_allowed(canonical)
            except Exception as exc:
                allowed = mode != "block"
                reason = f"MT5 news gate failed: {str(exc)[:160]}"
                details = {"mode": mode, "error": str(exc)[:240]}
        decision = "allow"
        if not allowed:
            decision = "block"
            _ss.set_status("last_news_gate", f"BLOCK {canonical}: {reason}")
        elif str(reason or "").startswith("audit would block"):
            decision = "audit_would_block"
            _ss.set_status("last_news_gate", f"AUDIT {canonical}: {reason}")
        else:
            decision = "audit_allow" if mode == "audit" else "allow"
            _ss.set_status("last_news_gate", f"ALLOW {canonical}: {reason}")
        self._audit_decision({
            "event": "news_gate",
            "decision": decision,
            "symbol": canonical,
            "market": market,
            "direction": sig.get("direction"),
            "strategy": str(sig.get("strategy") or ""),
            "score": float(sig.get("score") or 0.0),
            "reason": reason,
            "details": self._audit_json_safe(details),
        })
        return allowed, reason, details

    def _strategy_allows_entry(self, sig: dict) -> tuple[bool, str]:
        strategy = str((sig or {}).get("strategy") or (sig or {}).get("mode") or "").strip().upper()
        if not strategy:
            return False, "missing strategy"
        if bool((sig or {}).get("replacement_strategy_v1")):
            return True, "STRATEGY_V1_ENGINE_LOGIC"
        configured = {str(cfg.get("strategy") or "").strip().upper() for cfg in SYMBOL_CONFIG.values() if cfg.get("strategy")}
        if strategy in configured:
            return True, "OK"
        active = {str(item).strip().upper() for item in ACTIVE_PROBABILITY_STRATEGIES}
        if strategy not in active:
            return False, f"strategy not an active live strategy: {strategy}"
        return True, "OK"

    def _magic_number_allows_entry(self) -> tuple[bool, str]:
        try:
            runtime_magic = int(getattr(self.runtime, "magic", 0) or 0)
        except Exception:
            runtime_magic = 0
        if runtime_magic <= 0:
            return False, "invalid MT5 magic number"
        raw_magic = os.getenv("MT5_MAGIC")
        if raw_magic not in (None, ""):
            try:
                env_magic = int(float(raw_magic))
            except Exception:
                return False, f"invalid MT5_MAGIC env value: {raw_magic}"
            if env_magic != runtime_magic:
                return False, f"runtime magic mismatch ({runtime_magic} != env {env_magic})"
        return True, "OK"

    def _signal_fresh_enough(self, sig: dict) -> tuple[bool, str]:
        max_age_s = _env_int("MT5_SIGNAL_EXPIRY_SECONDS", 120, lo=1)
        timestamp = None
        for key in ("generated_at", "signal_generated_at", "updated_at", "timestamp"):
            timestamp = _parse_dt((sig or {}).get(key))
            if timestamp is not None:
                break
        if timestamp is None:
            return False, "missing signal timestamp"
        age_s = (datetime.utcnow() - timestamp).total_seconds()
        sig["signal_age_seconds"] = round(max(0.0, age_s), 3)
        if age_s < -10:
            return False, f"signal timestamp is in the future ({age_s:.1f}s)"
        if age_s > max_age_s:
            return False, f"stale signal ({age_s:.1f}s > {max_age_s}s)"
        return True, "OK"

    def _daily_symbol_trade_count(self, sym: str) -> int:
        canonical = self._canonical_from_resolved(sym)
        try:
            today = _ss.trading_today()
            conn = _ss.get_conn()
            try:
                rows = conn.execute(
                    """
                    SELECT trade_id, sym, opened_at, trade_date, status
                    FROM trades
                    WHERE COALESCE(status, '') NOT IN ('rejected', 'cancelled')
                    """
                ).fetchall()
            finally:
                conn.close()
        except Exception:
            return 0
        count = 0
        for row in rows:
            row = dict(row)
            trade_id = str(row.get("trade_id") or "")
            if trade_id.startswith("mt5_adopted_"):
                continue
            if self._canonical_from_resolved(str(row.get("sym") or "")) != canonical:
                continue
            try:
                trade_day = _ss.trade_date_for(row.get("opened_at"), fallback=row.get("trade_date"))
            except Exception:
                trade_day = str(row.get("opened_at") or row.get("trade_date") or "")[:10]
            if trade_day == today:
                count += 1
        return count

    def _daily_symbol_trade_count_allows_entry(self, sym: str, market: str = "") -> tuple[bool, str]:
        canonical = self._canonical_from_resolved(sym).upper()
        count = self._daily_symbol_trade_count(canonical)
        global_limit = self._effective_max_daily_trades()
        _ss.set_status(f"symbol_daily_trade_count_{canonical}", count)
        _ss.set_status(f"symbol_max_daily_trades_{canonical}", global_limit)
        if count >= global_limit:
            self._audit_decision({
                "event": "SYMBOL_DAILY_LIMIT_OBSERVE_ONLY",
                "decision": "observe_only",
                "scope": "symbol",
                "symbol": canonical,
                "count": count,
                "global_limit": global_limit,
                "reason": "only global daily trade limit is enforced",
            })
        return True, "OK; per-symbol daily trade limit is observe-only"

    def _record_symbol_close(self, sym: str, closed_at, outcome: str = "", realized: float | None = None) -> None:
        canonical = self._canonical_from_resolved(sym)
        self.last_symbol_closes[canonical] = {
            "closed_at": str(closed_at or datetime.utcnow().isoformat()),
            "outcome": str(outcome or ""),
            "realized": realized,
        }

    def _last_symbol_close_allows_entry(self, sym: str, *, confirmed_pending: bool = False) -> tuple[bool, str]:
        canonical = self._canonical_from_resolved(sym).upper()
        self._audit_decision({
            "event": "SYMBOL_REENTRY_OBSERVE_ONLY",
            "decision": "observe_only",
            "scope": "symbol",
            "symbol": canonical,
            "confirmed_pending": bool(confirmed_pending),
            "reason": "symbol re-entry cooldown is not an execution veto",
        })
        return True, "OK; symbol re-entry cooldown is observe-only"

    def _symbol_lock_allows_entry(self, sym: str) -> tuple[bool, str]:
        canonical = self._canonical_from_resolved(sym).upper()
        self._audit_decision({
            "event": "SYMBOL_TRADE_LOCK_OBSERVE_ONLY",
            "decision": "observe_only",
            "scope": "symbol",
            "symbol": canonical,
            "reason": "persistent symbol trade lock is not an execution veto",
        })
        return True, "OK; symbol trade lock is observe-only"

    def _set_symbol_trade_lock(self, sym: str, gate_id: str) -> None:
        canonical = self._canonical_from_resolved(sym)
        self.symbol_trade_locks[canonical] = {
            "gate_id": gate_id,
            "locked_at": datetime.utcnow().isoformat(),
        }
        self._save_state()

    def _pyramid_context_for_trade(self, sym: str, direction: str) -> dict:
        canonical = self._canonical_from_resolved(sym)
        direction = str(direction or "").upper()
        try:
            positions = [pos for pos in self.gateway.positions() if self._canonical_from_resolved(pos.symbol) == canonical]
        except Exception:
            positions = []
        same_direction = [pos for pos in positions if str(pos.direction or "").upper() == direction]
        metas = []
        for pos in same_direction:
            meta = self.open_trades.get(str(pos.ticket), {})
            metas.append((pos, meta if isinstance(meta, dict) else {}))
        parent_trade_id = ""
        initial_lot = 0.0
        basket_risk = 0.0
        basket_target = 0.0
        for pos, meta in metas:
            if not parent_trade_id:
                parent_trade_id = str(meta.get("parent_trade_id") or meta.get("trade_id") or "")
            if initial_lot <= 0:
                initial_lot = float(meta.get("initial_lot") or meta.get("qty") or pos.volume or 0.0)
            risk_meta = meta.get("risk_meta") if isinstance(meta.get("risk_meta"), dict) else {}
            basket_risk += abs(float(risk_meta.get("planned_risk") or 0.0))
            aggressive = risk_meta.get("aggressive_sizing") if isinstance(risk_meta.get("aggressive_sizing"), dict) else {}
            basket_target += abs(float(risk_meta.get("basket_target_profit") or aggressive.get("target_profit_usd") or 0.0))
        basket_pnl = sum(float(pos.profit or 0.0) for pos in same_direction)
        return {
            "parent_trade_id": parent_trade_id,
            "pyramid_level": len(same_direction) + 1,
            "initial_lot": initial_lot,
            "basket_unrealized_pnl": round(basket_pnl, 2),
            "basket_risk": round(basket_risk, 2),
            "basket_target_profit": round(basket_target, 2),
            "basket_expected_profit": round(basket_target, 2),
            "same_direction_positions": len(same_direction),
        }

    def _pyramid_addon_volume_cap(self, sym: str, direction: str) -> float | None:
        ctx = self._pyramid_context_for_trade(sym, direction)
        level = int(ctx.get("pyramid_level") or 1)
        initial_lot = float(ctx.get("initial_lot") or 0.0)
        if level <= 1 or initial_lot <= 0:
            return None
        fraction = _env_float("MT5_PYRAMID_ADDON_FRACTION", 0.50, lo=0.01, hi=1.0)
        return initial_lot * fraction

    def _pyramiding_allows_entry(self, sym: str, direction: str) -> tuple[bool, str]:
        canonical = self._canonical_from_resolved(sym)
        direction = str(direction or "").upper()
        if not self._pyramiding_enabled():
            return False, "pyramiding disabled"
        if not self._entry_window_open(self._market_for_symbol(canonical)):
            return False, "pyramid add blocked outside session"
        try:
            positions = [pos for pos in self.gateway.positions() if self._canonical_from_resolved(pos.symbol) == canonical]
        except Exception as exc:
            return False, f"cannot confirm existing positions for pyramid check: {str(exc)[:120]}"
        if not positions:
            return True, "OK"
        same_direction = [pos for pos in positions if str(pos.direction or "").upper() == direction]
        if not same_direction:
            active_dir = str(positions[0].direction or "").upper()
            return False, f"active position is {active_dir}, not {direction}"
        max_levels = _env_int("MT5_PYRAMID_MAX_LEVELS", 10, lo=1, hi=10)
        if len(same_direction) >= max_levels:
            return False, f"pyramid max level reached ({max_levels})"
        for pos in same_direction:
            meta = self.open_trades.get(str(pos.ticket), {})
            if isinstance(meta, dict) and meta.get("trend_invalidation_warning"):
                return False, "pyramid blocked after trend invalidation warning"
        ctx = self._pyramid_context_for_trade(canonical, direction)
        basket_pnl = float(ctx.get("basket_unrealized_pnl") or 0.0)
        if basket_pnl <= 0:
            return False, f"cannot average down: basket not profitable ({basket_pnl:.2f})"
        basket_risk = float(ctx.get("basket_risk") or 0.0)
        if basket_risk <= 0:
            anchor = max(same_direction, key=lambda pos: float(pos.profit or 0.0))
            basket_risk = max(abs(float(anchor.profit or 0.0)) * 2.0, 1.0)
        current_r = basket_pnl / basket_risk if basket_risk > 0 else 0.0
        next_level = len(same_direction) + 1
        required_r = _env_float("MT5_PYRAMID_MIN_ADD_R", 0.10, lo=0.01, hi=5.0) if next_level == 2 else _env_float("MT5_PYRAMID_SUBSEQUENT_MIN_R", 1.00, lo=0.10, hi=5.0)
        if current_r < required_r:
            return False, f"pyramid level {next_level} requires basket >= {required_r:.2f}R ({current_r:.2f}R now)"
        return True, f"pyramid level {next_level} allowed basket={current_r:.2f}R pnl={basket_pnl:.2f}"

    def _campaign_key(self, symbol: str, direction: str) -> str:
        canonical = self._canonical_from_resolved(symbol).upper()
        return f"{canonical}:{str(direction or '').upper()}"

    def _campaign_alignment_allows_add(self, canonical: str, direction: str) -> tuple[bool, str, dict]:
        try:
            h1 = self._strategy_v1_frame(self.gateway.rates(canonical, "H1", 90), "H1", completed_only=True)
            m15 = self._strategy_v1_frame(self.gateway.rates(canonical, "M15", 70), "M15", completed_only=True)
            h1_side, h1_score = _strategy_v1.context(h1)
            m15_side, m15_score = _strategy_v1.context(m15)
        except Exception as exc:
            return False, f"campaign HTF context unavailable: {str(exc)[:140]}", {}
        details = {
            "h1_side": h1_side,
            "h1_score": round(float(h1_score), 2),
            "m15_side": m15_side,
            "m15_score": round(float(m15_score), 2),
        }
        if h1_side != direction:
            return False, f"campaign H1 no longer supports {direction}", details
        if m15_side not in {direction, "FLAT"}:
            return False, f"campaign M15 opposes {direction}", details
        return True, "campaign HTF alignment valid", details

    def _campaign_cycle(self) -> None:
        if not self._pyramiding_enabled():
            return
        try:
            positions = list(self.gateway.positions())
        except Exception as exc:
            _ss.set_status("last_campaign_error", f"positions unavailable: {str(exc)[:160]}")
            return

        grouped: dict[tuple[str, str], list[MT5PositionView]] = {}
        symbol_directions: dict[str, set[str]] = {}
        for pos in positions:
            canonical = self._canonical_from_resolved(pos.symbol).upper()
            direction = str(pos.direction or "").upper()
            if canonical not in self.symbol_markets or direction not in {"BUY", "SELL"}:
                continue
            grouped.setdefault((canonical, direction), []).append(pos)
            symbol_directions.setdefault(canonical, set()).add(direction)

        active_keys = {self._campaign_key(symbol, side) for symbol, side in grouped}
        now_iso = datetime.utcnow().isoformat()
        for key, campaign in list(self.campaigns.items()):
            if isinstance(campaign, dict) and key not in active_keys and campaign.get("status") == "active":
                campaign["status"] = "closed"
                campaign["closed_at"] = now_iso

        for (canonical, direction), legs in grouped.items():
            owned = []
            for pos in legs:
                meta = self.open_trades.get(str(pos.ticket), {})
                risk_meta = meta.get("risk_meta") if isinstance(meta, dict) and isinstance(meta.get("risk_meta"), dict) else {}
                trade_id = str(meta.get("trade_id") or "") if isinstance(meta, dict) else ""
                if trade_id and not trade_id.startswith("mt5_adopted_") and not risk_meta.get("adopted"):
                    owned.append((pos, meta))
            if not owned:
                continue

            key = self._campaign_key(canonical, direction)
            campaign = self.campaigns.setdefault(key, {
                "campaign_id": str(owned[0][1].get("parent_trade_id") or owned[0][1].get("trade_id") or f"campaign_{canonical}_{int(time.time())}"),
                "symbol": canonical,
                "direction": direction,
                "engine": str((owned[0][1].get("trade_audit") or {}).get("engine") or get_engine_for_symbol(canonical)),
                "strategy": str((owned[0][1].get("trade_audit") or {}).get("strategy") or ""),
                "created_at": str(owned[0][1].get("opened_at") or now_iso),
                "accepted_legs": len(owned),
                "status": "active",
            })
            campaign["status"] = "active"
            campaign["active_tickets"] = [str(pos.ticket) for pos, _meta in owned]
            campaign["accepted_legs"] = max(int(campaign.get("accepted_legs") or 0), len(owned))
            campaign["basket_profit_usd"] = round(sum(float(pos.profit or 0.0) for pos, _meta in owned), 2)
            campaign["basket_risk_usd"] = round(sum(
                abs(float(((meta.get("risk_meta") or {}).get("planned_risk") or 0.0)))
                for _pos, meta in owned
            ), 2)
            campaign["updated_at"] = now_iso

            peak_profit = max(float(campaign.get("peak_profit_usd") or 0.0), float(campaign["basket_profit_usd"]))
            campaign["peak_profit_usd"] = round(peak_profit, 2)
            basket_risk_now = float(campaign.get("basket_risk_usd") or 0.0)
            basket_profit_now = float(campaign.get("basket_profit_usd") or 0.0)
            if basket_risk_now > 0:
                basket_target_r = self._entry_rr_for_symbol(
                    canonical, self._market_for_symbol(canonical), {}, configured_rr=(get_symbol_config(canonical) or {}).get("tp_r")
                )
                basket_target_usd = basket_risk_now * basket_target_r
                basket_stop_usd = -basket_risk_now
                lock_trigger_r = _env_float("MT5_CAMPAIGN_LOCK_TRIGGER_R", 0.75, lo=0.10, hi=5.0)
                lock_giveback_r = _env_float("MT5_CAMPAIGN_LOCK_GIVEBACK_R", 0.40, lo=0.05, hi=2.0)
                minimum_lock_r = _env_float("MT5_CAMPAIGN_MIN_LOCK_R", 0.20, lo=0.0, hi=2.0)
                peak_r = peak_profit / basket_risk_now
                lock_floor_r = max(0.0, min(peak_r, max(minimum_lock_r, peak_r - lock_giveback_r))) if peak_r >= lock_trigger_r else 0.0
                lock_floor_usd = lock_floor_r * basket_risk_now
                campaign.update({
                    "basket_stop_usd": round(basket_stop_usd, 2),
                    "basket_target_usd": round(basket_target_usd, 2),
                    "profit_lock_floor_usd": round(lock_floor_usd, 2),
                    "peak_r": round(peak_r, 4),
                })
                close_reason = ""
                close_mode = ""
                if basket_profit_now <= basket_stop_usd:
                    close_reason = f"campaign basket stop reached {basket_profit_now:.2f} <= {basket_stop_usd:.2f}"
                    close_mode = "risk"
                elif basket_profit_now >= basket_target_usd:
                    close_reason = f"campaign basket target reached {basket_profit_now:.2f} >= {basket_target_usd:.2f}"
                    close_mode = "profit"
                elif lock_floor_usd > 0 and basket_profit_now <= lock_floor_usd:
                    close_reason = f"campaign profit lock reached {basket_profit_now:.2f} <= {lock_floor_usd:.2f}"
                    close_mode = "profit"
                if close_reason:
                    closed = 0
                    for pos, _meta in owned:
                        ok = (
                            self._close_position_for_profit(pos, canonical, close_reason)
                            if close_mode == "profit"
                            else self._close_position_for_risk(pos, canonical, close_reason)
                        )
                        closed += int(bool(ok))
                    campaign["last_observation"] = f"CAMPAIGN_BASKET_CLOSE_SENT {closed}/{len(owned)} {close_reason}"
                    campaign["close_requested_at"] = datetime.utcnow().isoformat()
                    self._audit_decision({
                        "event": "CAMPAIGN_BASKET_CLOSE_SENT", "decision": "PASS" if closed else "ERROR",
                        "symbol": canonical, "direction": direction, "campaign_id": campaign.get("campaign_id"),
                        "closed_positions": closed, "position_count": len(owned), "reason": close_reason,
                    })
                    continue

            max_levels = _env_int("MT5_PYRAMID_MAX_LEVELS", 10, lo=1, hi=10)
            level = int(campaign.get("accepted_legs") or len(owned))
            if level >= max_levels or len(symbol_directions.get(canonical, set())) != 1:
                continue
            if not self._entry_window_open(self._market_for_symbol(canonical)):
                continue
            if not self._daily_risk_allows_entry() or not self._daily_trade_count_allows_entry():
                continue
            if self._open_position_count() >= self._effective_max_open_trades():
                continue

            basket_profit = float(campaign.get("basket_profit_usd") or 0.0)
            basket_risk = float(campaign.get("basket_risk_usd") or 0.0)
            if basket_profit <= 0 or basket_risk <= 0:
                continue
            basket_r = basket_profit / basket_risk
            first_trigger = _env_float("MT5_PYRAMID_MIN_ADD_R", 0.10, lo=0.01, hi=2.0)
            step_r = _env_float("MT5_PYRAMID_STEP_R", 0.10, lo=0.01, hi=1.0)
            required_r = first_trigger + max(0, level - 1) * step_r
            campaign["basket_r"] = round(basket_r, 4)
            campaign["next_add_required_r"] = round(required_r, 4)
            if basket_r < required_r:
                continue

            cooldown = _env_float("MT5_PYRAMID_ADD_COOLDOWN_SECONDS", 1.0, lo=0.25, hi=30.0)
            last_add = _parse_dt(campaign.get("last_add_at"))
            if last_add and (datetime.utcnow() - last_add).total_seconds() < cooldown:
                continue
            weighted_entry = sum(float(pos.price_open) * float(pos.volume) for pos, _meta in owned) / max(
                sum(float(pos.volume) for pos, _meta in owned), 1e-12
            )
            try:
                _resolved, tick = self.gateway.symbol_tick(canonical)
                bid = float(getattr(tick, "bid", 0.0) or 0.0)
                ask = float(getattr(tick, "ask", 0.0) or 0.0)
            except Exception:
                continue
            price = ask if direction == "BUY" else bid
            if price <= 0 or (direction == "BUY" and price <= weighted_entry) or (direction == "SELL" and price >= weighted_entry):
                continue
            last_add_price = float(campaign.get("last_add_price") or weighted_entry)
            if (direction == "BUY" and price <= last_add_price) or (direction == "SELL" and price >= last_add_price):
                continue

            aligned, alignment_reason, alignment = self._campaign_alignment_allows_add(canonical, direction)
            campaign["alignment"] = alignment
            if not aligned:
                campaign["last_observation"] = alignment_reason
                continue

            market = self._market_for_symbol(canonical)
            cfg = get_symbol_config(canonical) or {}
            atr = self._recent_atr(canonical)
            if atr <= 0:
                continue
            stop_d = self._cap_stop_distance_for_symbol(
                canonical, market, atr * float(cfg.get("atr_stop_mult") or 1.3), {}
            )
            rr = self._entry_rr_for_symbol(canonical, market, {}, configured_rr=cfg.get("tp_r"))
            sl = price - stop_d if direction == "BUY" else price + stop_d
            tp = price + stop_d * rr if direction == "BUY" else price - stop_d * rr
            engine = str(campaign.get("engine") or get_engine_for_symbol(canonical))
            strategy = str(campaign.get("strategy") or engine)
            sig = {
                "symbol": canonical, "market": market, "asset_class": str(cfg.get("asset_class") or ""),
                "engine": engine, "strategy": strategy, "mode": "PROFIT_PYRAMID",
                "setup_type": "PROFIT_PYRAMID", "population": "profit_pyramid",
                "direction": direction, "price": price, "atr": atr, "score": 100.0,
                "min_score": 0.0, "replacement_strategy_v1": True,
                "one_min_confirmation": True, "execution_1m_score": 100.0,
                "raw_signal_side": direction, "setup_side": direction,
                "pending_setup_side": direction, "confirmation_side": direction,
                "final_order_side": direction,
                "generated_at": now_iso, "setup_created_at": now_iso,
                "confirmation_detected_at": now_iso, "source_loop_name": "campaign_cycle",
                "campaign_id": campaign.get("campaign_id"), "pyramid_level": level + 1,
                "conditions": {"profitable_basket": True, "basket_r": basket_r, "required_r": required_r},
            }
            try:
                acct = self.gateway.account_info()
                volume, size_reason, risk_meta = self._mt5_volume_for_trade(
                    canonical, market, price, stop_d, self._strategy_equity(acct), sig
                )
                if volume <= 0:
                    raise RuntimeError(size_reason)
                campaign_risk_cap = _env_float("MT5_MAX_CAMPAIGN_RISK_USD", 1000.0, lo=1.0, hi=1000.0)
                planned_risk = float((risk_meta or {}).get("planned_risk") or 0.0)
                if basket_risk + planned_risk > campaign_risk_cap:
                    raise RuntimeError(
                        f"campaign risk cap reached ({basket_risk + planned_risk:.2f}/{campaign_risk_cap:.2f})"
                    )
                cost_ok, cost_reason, _adjusted, cost_details = self.cost_model.check(
                    canonical, market, sig, volume, sl, tp, risk_meta, stage="pre_order"
                )
                if not cost_ok:
                    raise RuntimeError(cost_reason)
                risk_ok, risk_reason = self.portfolio_risk_manager.check(canonical, market, sig)
                if not risk_ok:
                    raise RuntimeError(risk_reason)
                entry_ok, entry_reason = self._execution_gate_allows_entry(
                    canonical, market, sig, volume, sl, tp, risk_meta
                )
                if not entry_ok:
                    raise RuntimeError(entry_reason)
                quality_ok, quality_reason, _quality = self.execution_quality_engine.pre_order_check(
                    canonical, market, sig, volume, sl, tp
                )
                if not quality_ok:
                    raise RuntimeError(quality_reason)
                result = self._place_trade(canonical, market, sig, volume, sl, tp, risk_meta)
                accepted, result_reason, _details = self.execution_quality_engine.confirm_order_result(canonical, sig, result)
                if not accepted:
                    raise RuntimeError(result_reason)
                campaign["accepted_legs"] = level + 1
                campaign["last_add_at"] = datetime.utcnow().isoformat()
                campaign["last_add_price"] = price
                campaign["last_add_volume"] = volume
                campaign["last_observation"] = f"PYRAMID_ADD_FILLED level={level + 1}"
                self._audit_decision({
                    "event": "PYRAMID_ADD_FILLED", "decision": "PASS", "symbol": canonical,
                    "direction": direction, "campaign_id": campaign.get("campaign_id"),
                    "pyramid_level": level + 1, "basket_r": basket_r, "volume": volume,
                })
            except Exception as exc:
                campaign["last_observation"] = f"PYRAMID_ADD_SKIPPED {str(exc)[:180]}"
                campaign["last_attempt_at"] = datetime.utcnow().isoformat()
                self._audit_decision({
                    "event": "PYRAMID_ADD_SKIPPED", "decision": "observe", "symbol": canonical,
                    "direction": direction, "campaign_id": campaign.get("campaign_id"),
                    "pyramid_level": level + 1, "reason": str(exc)[:180],
                })
        self._save_state()

    def _duplicate_order_allows_entry(self, sym: str, direction: str) -> tuple[bool, str]:
        canonical = self._canonical_from_resolved(sym)
        try:
            positions = list(self.gateway.positions())
        except Exception as exc:
            return False, f"cannot confirm live positions before order send: {str(exc)[:120]}"
        same_positions = [pos for pos in positions if self._canonical_from_resolved(pos.symbol) == canonical]
        if same_positions:
            if not self._pyramiding_enabled():
                tickets = ",".join(str(pos.ticket) for pos in same_positions[:5])
                return False, f"duplicate live position exists for {canonical}: {tickets}"
            return self._pyramiding_allows_entry(sym, direction)

        try:
            orders = list(self.gateway.orders())
        except Exception as exc:
            return False, f"cannot confirm pending orders before order send: {str(exc)[:120]}"
        same_orders = [order for order in orders if self._canonical_from_resolved(order.symbol) == canonical]
        if same_orders:
            tickets = ",".join(str(order.ticket) for order in same_orders[:5])
            return False, f"pending order already exists for {canonical}: {tickets}"

        live_tickets = {str(pos.ticket) for pos in positions}
        for ticket, meta in list((getattr(self, "open_trades", {}) or {}).items()):
            if not isinstance(meta, dict):
                continue
            if self._canonical_from_resolved(str(meta.get("sym") or "")) != canonical:
                continue
            if str(ticket).startswith("pending_") and self._pending_ticket_still_fresh(meta):
                return False, f"fresh pending state already exists for {canonical}: {ticket}"
            if str(ticket) in live_tickets:
                return False, f"tracked live state already exists for {canonical}: {ticket}"
        return True, "OK"

    def _final_engine_policy_recheck(
        self,
        canonical: str,
        market: str,
        sig: dict,
        qty: float,
        sl: float,
        tp: float,
        risk_meta: dict | None = None,
    ) -> tuple[bool, str, dict]:
        original_engine = str(sig.get("engine") or get_engine_for_symbol(canonical) or "").upper()
        original_asset = str(sig.get("asset_class") or "").lower()
        original_direction = str(sig.get("direction") or "").upper()
        cfg = get_symbol_config(canonical) or {}
        effective_rr = self._entry_rr_for_symbol(canonical, market, sig, configured_rr=cfg.get("tp_r"))
        price = float(sig.get("price") or 0.0)
        stop_distance = abs(price - float(sl or 0.0))
        actual_rr = abs(float(tp or 0.0) - price) / stop_distance if stop_distance > 0 else 0.0
        expected_tp = price + stop_distance * effective_rr if original_direction == "BUY" else price - stop_distance * effective_rr
        details = {
            "original_engine": original_engine,
            "original_asset_class": original_asset,
            "direction": original_direction,
            "effective_rr": round(float(effective_rr), 6),
            "actual_rr": round(float(actual_rr), 6),
            "expected_tp": round(float(expected_tp), 6),
            "actual_tp": round(float(tp or 0.0), 6),
        }

        def result(allowed: bool, reason: str) -> tuple[bool, str, dict]:
            details["decision"] = "allow" if allowed else "block"
            details["reason"] = reason
            sig.setdefault("_mt5_thresholds", {})["engine_policy_recheck"] = details
            quality = (not allowed) and self._is_quality_filter_reason(reason, sig)
            event = "ENGINE_POLICY_RECHECK" if allowed else ("NOT_QUALIFIED" if quality else "ENGINE_POLICY_RECHECK_BLOCKED")
            self._audit_decision({
                "event": event,
                "decision": "allow" if allowed else ("not_qualified" if quality else "block"),
                "scope": "order",
                "symbol": canonical,
                "market": market,
                "engine": original_engine,
                "side": original_direction,
                "setup_id": sig.get("setup_id"),
                "details": self._audit_json_safe(details),
                "reason": reason,
            })
            print(
                f"[MT5_ENGINE_POLICY_RECHECK] symbol={canonical} engine={original_engine} "
                f"side={original_direction} decision={'PASS' if allowed else ('NOT_QUALIFIED' if quality else 'BLOCK')} reason={reason}",
                flush=True,
            )
            return allowed, reason, details

        if original_direction not in {"BUY", "SELL"}:
            return result(False, f"invalid direction for engine policy recheck: {original_direction or '<missing>'}")
        rr_tolerance = max(0.005, abs(float(effective_rr)) * 0.02)
        if abs(actual_rr - effective_rr) > rr_tolerance:
            return result(False, f"RR mismatch actual={actual_rr:.3f} intended={effective_rr:.3f}")

        pending = sig.get("pending_setup") if isinstance(sig.get("pending_setup"), dict) else None
        if pending is not None:
            stale_ok, stale_reason, stale_meta = self._setup_stale_entry_guard(
                canonical, market, pending, sig, enforce_fresh_age=True
            )
            details["stale_entry"] = stale_meta
            if not stale_ok:
                return result(False, f"stale entry policy failed: {stale_reason}")

        engine_context = pending.get("engine_context") if isinstance(pending, dict) and isinstance(pending.get("engine_context"), dict) else {}
        original_pending_state = str((pending or {}).get("state") or (pending or {}).get("status") or "").upper()
        policy_sig = dict(sig)
        policy_sig.update({key: value for key, value in engine_context.items() if value is not None})
        policy_sig["direction"] = original_direction
        policy_sig["engine"] = original_engine
        policy_sig["asset_class"] = original_asset
        policy_sig["score"] = float(engine_context.get("score") or engine_context.get("score_after") or sig.get("score") or 0.0)
        policy_sig["score_before"] = float(engine_context.get("score_before") or sig.get("score_before") or policy_sig["score"])
        policy_sig["score_after"] = float(engine_context.get("score_after") or policy_sig["score"])
        policy_sig["strategy_before"] = str(engine_context.get("strategy_before") or sig.get("strategy_before") or sig.get("strategy") or "")
        policy_sig["strategy_after"] = str(engine_context.get("strategy_after") or sig.get("strategy_after") or sig.get("strategy") or "")
        policy_sig["strategy"] = policy_sig["strategy_after"]
        policy_details = dict(engine_context)
        policy_ok, policy_reason = self._engine_policy_allows_entry(canonical, market, policy_sig, policy_details)
        details["policy_reason"] = policy_reason
        details["policy_engine_after"] = str(policy_sig.get("engine") or "").upper()
        details["policy_asset_after"] = str(policy_sig.get("asset_class") or "").lower()
        if not policy_ok:
            policy_score = float(policy_sig.get("score_after") or policy_sig.get("score") or 0.0)
            pending_floor = float(
                engine_context.get("pending_floor")
                or (pending or {}).get("pending_floor")
                or 0.0
            )
            soft_floor_recheck = (
                original_pending_state in {"PENDING", "PENDING_SOFT", "WAITING_1M"}
                and pending_floor > 0
                and policy_score >= pending_floor
                and "score below min_score" in str(policy_reason)
            )
            if soft_floor_recheck:
                details["policy_soft_floor_recheck"] = {
                    "original_pending_state": original_pending_state,
                    "policy_score": policy_score,
                    "pending_floor": pending_floor,
                }
                policy_ok = True
                policy_reason = f"engine policy remained above original pending floor ({policy_score:.1f} >= {pending_floor:.1f})"
            else:
                return result(False, policy_reason)
        if details["policy_engine_after"] != original_engine or (
            original_asset and details["policy_asset_after"] != original_asset
        ):
            return result(False, f"engine context changed during recheck ({original_engine}/{original_asset} -> {details['policy_engine_after']}/{details['policy_asset_after']})")

        cost_ok, cost_reason, _adjusted_score, cost_details = self.cost_model.check(
            canonical,
            market,
            sig,
            qty,
            sl,
            expected_tp,
            risk_meta,
            stage="engine_policy_recheck",
        )
        details["cost"] = cost_details
        if not cost_ok:
            return result(False, f"cost-to-target policy failed: {cost_reason}")
        return result(True, "engine policy, RR/TP, stale-entry, and cost checks passed")

    def _execution_gate_allows_entry(
        self,
        sym: str,
        market: str,
        sig: dict,
        qty: float,
        sl: float,
        tp: float,
        risk_meta: dict | None = None,
    ) -> tuple[bool, str]:
        canonical = self._canonical_from_resolved(sym)
        gate_id = f"mt5gate_{canonical}_{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}"
        direction = str((sig or {}).get("direction") or "").upper()
        price = float((sig or {}).get("price") or 0.0)

        def finish(allowed: bool, reason: str) -> tuple[bool, str]:
            quality = (not allowed) and self._is_quality_filter_reason(reason, sig)
            decision = "allow" if allowed else ("not_qualified" if quality else "block")
            if not allowed and quality:
                _ss.set_status("last_execution_gate", f"NOT_QUALIFIED {canonical}: {reason}")
                self._mark_not_qualified(sig, reason, "FINAL_QUALITY_FILTER")
            elif not allowed:
                _ss.set_status("last_execution_gate", f"BLOCK {canonical}: {reason}")
                self._mark_blocked(sig, "ORDER_EXECUTION_CHECK", reason)
                self._log_scan_block_reason(canonical, direction, str((sig or {}).get("setup_id") or ""), reason, sig=sig)
            else:
                self._trace_step(sig, "ORDER_EXECUTION_CHECK", "PASS", reason, gate_id=gate_id)
            self._audit_decision({
                "event": "execution_gate",
                "decision": decision,
                "gate_id": gate_id,
                "symbol": canonical,
                "market": market,
                "direction": direction,
                "qty": qty,
                "price": price,
                "sl": sl,
                "tp": tp,
                "strategy": str((sig or {}).get("strategy") or ""),
                "score": float((sig or {}).get("score") or 0.0),
                "reason": reason,
                "dry_run": bool(getattr(self.runtime, "dry_run", False)),
                "risk_meta": risk_meta or {},
            })
            return allowed, reason

        if direction not in {"BUY", "SELL"}:
            return finish(False, f"invalid direction for execution gate: {direction or '<missing>'}")
        if qty <= 0 or price <= 0 or sl <= 0 or tp <= 0:
            return finish(False, "invalid price/volume/SL/TP for execution gate")
        if direction == "BUY" and not (sl < price < tp):
            return finish(False, "BUY order has invalid SL/TP geometry")
        if direction == "SELL" and not (tp < price < sl):
            return finish(False, "SELL order has invalid SL/TP geometry")
        if bool((sig or {}).get("replacement_strategy_v1") or ((sig or {}).get("pending_setup") or {}).get("replacement_strategy_v1")):
            profile_ok, profile_reason = True, "STRATEGY_V1_ENGINE_PROFILE"
        else:
            profile_ok, profile_reason = self._symbol_profile_gate_allows_entry(canonical, market, sig)
        if not profile_ok:
            return finish(False, f"NOT_QUALIFIED profile gate {profile_reason}")
        if bool((sig or {}).get("replacement_strategy_v1") or ((sig or {}).get("pending_setup") or {}).get("replacement_strategy_v1")):
            exhaustion_ok, exhaustion_reason = True, "replacement exhaustion qualified before SETUP_CREATED"
            exhaustion_meta = dict((sig.get("_mt5_thresholds") or {}).get("index_exhaustion_pre_setup") or {})
        else:
            exhaustion_ok, exhaustion_reason, exhaustion_meta = self._index_exhaustion_allows_entry(canonical, market, sig)
        if not exhaustion_ok:
            sig.setdefault("_mt5_thresholds", {})["index_exhaustion"] = exhaustion_meta
            return finish(False, f"NOT_QUALIFIED index exhaustion {exhaustion_reason}")

        if bool((sig or {}).get("replacement_strategy_v1") or ((sig or {}).get("pending_setup") or {}).get("replacement_strategy_v1")):
            policy_ok, policy_reason, policy_details = True, "STRATEGY_V1_POLICY_ALREADY_PASSED", {"source": "strategy_architecture_v1"}
            self._audit_decision({
                "event": "MT5_ENGINE_POLICY_RECHECK",
                "decision": "PASS",
                "symbol": canonical,
                "engine": sig.get("engine"),
                "reason": policy_reason,
                "source": "strategy_architecture_v1",
            })
        else:
            policy_ok, policy_reason, policy_details = self._final_engine_policy_recheck(
                canonical, market, sig, qty, sl, tp, risk_meta
            )
        if not policy_ok:
            return finish(False, f"NOT_QUALIFIED engine policy {policy_reason}")

        try:
            broker_geometry = self.gateway.order_calc_trade_geometry(
                canonical, direction, qty, price, sl, tp
            )
            broker_sl_loss = float(broker_geometry.get("sl_loss_usd") or 0.0)
            broker_tp_profit = float(broker_geometry.get("tp_profit_usd") or 0.0)
            broker_margin = float(broker_geometry.get("margin_usd") or 0.0)
            account = self.gateway.account_info()
            free_margin = float(getattr(account, "free_margin", 0.0) or 0.0)
        except Exception as exc:
            self._audit_decision({
                "event": "BROKER_CALC_MISMATCH",
                "decision": "BLOCK",
                "symbol": canonical,
                "direction": direction,
                "reason": f"broker geometry unavailable: {str(exc)[:180]}",
            })
            return finish(False, f"broker geometry unavailable: {str(exc)[:180]}")

        intended_rr = self._entry_rr_for_symbol(
            canonical,
            market,
            sig,
            configured_rr=(get_symbol_config(canonical) or {}).get("tp_r"),
        )
        broker_rr = broker_tp_profit / broker_sl_loss if broker_sl_loss > 0 else 0.0
        geometry_meta = {
            **broker_geometry,
            "broker_rr": round(broker_rr, 6),
            "intended_rr": round(float(intended_rr), 6),
            "free_margin_usd": round(free_margin, 2),
        }
        sig.setdefault("_mt5_thresholds", {})["broker_geometry"] = geometry_meta
        if broker_sl_loss <= 0 or broker_tp_profit <= 0 or broker_margin <= 0:
            return finish(False, f"broker geometry invalid: {geometry_meta}")
        if free_margin <= 0 or broker_margin > free_margin:
            return finish(False, f"broker margin insufficient: required={broker_margin:.2f} free={free_margin:.2f}")
        rr_tolerance = _env_float("MT5_BROKER_RR_TOLERANCE_PCT", 10.0, lo=1.0, hi=25.0) / 100.0
        if intended_rr > 0 and abs(broker_rr - intended_rr) / intended_rr > rr_tolerance:
            self._audit_decision({
                "event": "BROKER_CALC_MISMATCH",
                "decision": "BLOCK",
                "symbol": canonical,
                "direction": direction,
                "reason": "effective broker RR differs from intended RR",
                "broker_geometry": geometry_meta,
            })
            return finish(False, f"broker RR mismatch: actual={broker_rr:.3f} intended={intended_rr:.3f}")
        planned_risk = float((risk_meta or {}).get("planned_risk") or 0.0)
        if planned_risk > 0 and abs(broker_sl_loss - planned_risk) / planned_risk > 0.10:
            self._audit_decision({
                "event": "BROKER_CALC_MISMATCH",
                "decision": "BLOCK",
                "symbol": canonical,
                "direction": direction,
                "reason": "sizing risk differs from final broker loss",
                "planned_risk_usd": planned_risk,
                "broker_geometry": geometry_meta,
            })
            return finish(False, f"broker risk mismatch: actual={broker_sl_loss:.2f} planned={planned_risk:.2f}")
        if isinstance(risk_meta, dict):
            risk_meta["planned_risk"] = broker_sl_loss
            risk_meta["broker_geometry"] = geometry_meta
        self._audit_decision({
            "event": "BROKER_GEOMETRY_VALIDATED",
            "decision": "PASS",
            "symbol": canonical,
            "direction": direction,
            "broker_geometry": geometry_meta,
        })

        checks = [
            self._new_entries_allowed("execution_gate", canonical, sig),
            self._strategy_allows_entry(sig),
            self._magic_number_allows_entry(),
            self._signal_fresh_enough(sig),
            (self._daily_trade_count_allows_entry(), "daily trade count quality filter"),
            self._daily_symbol_trade_count_allows_entry(canonical, market),
            self._last_symbol_close_allows_entry(canonical, confirmed_pending=bool(sig.get("one_min_confirmation"))),
            self._symbol_lock_allows_entry(canonical),
            self._duplicate_order_allows_entry(canonical, direction),
        ]
        for ok, reason in checks:
            if not ok:
                return finish(False, reason)

        sig["_execution_gate_id"] = gate_id
        sig["_execution_gate_approved_at"] = datetime.utcnow().isoformat()
        self._set_symbol_trade_lock(canonical, gate_id)
        _ss.set_status("last_execution_gate", f"ALLOW {canonical}: {gate_id}")
        return finish(True, "OK")

    def _default_quality_min_score(self, market: str, strategy: str) -> float:
        strategy = str(strategy or "").upper()
        market = str(market or "").lower()
        if market == "forex":
            if strategy == "MEAN_REV":
                return float(CFG.get("mean_rev_min_score_forex", 70) or 70)
            if strategy == "BB_SQUEEZE":
                return float(CFG.get("bbs_min_score_forex", 70) or 70)
            if strategy == "PULLBACK":
                return 66.0
            return 65.0
        return 82.0

    def _quality_allows_entry(self, sym: str, group: str, market: str, sig: dict) -> tuple[bool, str]:
        score = float(sig.get("score") or 0.0)
        strategy = str(sig.get("strategy") or sig.get("mode") or "").upper()
        canonical = self._canonical_from_resolved(sym)
        clean_symbol = canonical.upper().replace(".", "_")
        conditions = sig.setdefault("conditions", {})
        thresholds = sig.setdefault("_mt5_thresholds", {})

        strategy_ok, strategy_reason = self._strategy_allows_entry(sig)
        if not strategy_ok:
            return False, strategy_reason

        blocked_strategies = _env_symbol_set("MT5_BLOCK_STRATEGIES", "") | _env_symbol_set(
            f"MT5_BLOCK_STRATEGIES_{clean_symbol}", ""
        )
        if strategy and strategy in blocked_strategies:
            return False, f"strategy blocked for {clean_symbol}: {strategy}"
        allowed_strategies = _env_symbol_set(f"MT5_ALLOW_STRATEGIES_{clean_symbol}", "")
        if strategy and allowed_strategies and strategy not in allowed_strategies:
            return False, f"strategy not allowed for {clean_symbol}: {strategy}"

        min_score, min_score_source = self._env_float_for_symbol_with_source(
            "MT5_MIN_SCORE",
            canonical,
            market,
            _env_float("MT5_MIN_SCORE", self._default_quality_min_score(market, strategy), lo=0.0, hi=100.0),
            lo=0.0,
            hi=100.0,
        )
        min_score_sources = [min_score_source]
        if market in {"index_cfd", "index_cfd_eu"} and os.getenv("MT5_INDEX_MIN_SCORE") is not None:
            index_min = _env_float("MT5_INDEX_MIN_SCORE", min_score, lo=0.0, hi=100.0)
            if index_min >= min_score:
                min_score_sources.append("MT5_INDEX_MIN_SCORE")
            min_score = max(min_score, index_min)
        if market == "stock_us" and os.getenv("MT5_STOCK_MIN_SCORE") is not None:
            stock_min = _env_float("MT5_STOCK_MIN_SCORE", min_score, lo=0.0, hi=100.0)
            if stock_min >= min_score:
                min_score_sources.append("MT5_STOCK_MIN_SCORE")
            min_score = max(min_score, stock_min)
        if group in {"metals", "energies", "crypto"}:
            alt_min, alt_source = self._env_float_for_symbol_with_source(
                "MT5_ALT_MARKET_MIN_SCORE", canonical, market, min_score, lo=0.0, hi=100.0
            )
            if alt_min >= min_score:
                min_score_sources.append(alt_source)
            min_score = max(
                min_score,
                alt_min,
            )
        if strategy == "PULLBACK":
            pullback_min, pullback_source = self._env_float_for_symbol_with_source(
                "MT5_PULLBACK_MIN_SCORE",
                canonical,
                market,
                _env_float("MT5_PULLBACK_MIN_SCORE", min_score, lo=0.0, hi=100.0),
                lo=0.0,
                hi=100.0,
            )
            if pullback_min >= min_score:
                min_score_sources.append(pullback_source)
            min_score = max(
                min_score,
                pullback_min,
            )
        if strategy == "MEAN_REV":
            mean_rev_min, mean_rev_source = self._env_float_for_symbol_with_source(
                "MT5_MEAN_REV_MIN_SCORE",
                canonical,
                market,
                _env_float("MT5_MEAN_REV_MIN_SCORE", min_score, lo=0.0, hi=100.0),
                lo=0.0,
                hi=100.0,
            )
            if mean_rev_min >= min_score:
                min_score_sources.append(mean_rev_source)
            min_score = max(
                min_score,
                mean_rev_min,
            )
        priority_symbol = self._is_priority_symbol(sym)
        if priority_symbol:
            min_score, priority_source = self._env_float_for_symbol_with_source(
                "MT5_PRIORITY_MIN_SCORE",
                canonical,
                market,
                _env_float("MT5_PRIORITY_MIN_SCORE", min_score, lo=0.0, hi=100.0),
                lo=0.0,
                hi=100.0,
            )
            min_score_sources.append(priority_source)
            min_score_floor, floor_source = self._env_float_for_symbol_with_source(
                "MT5_MIN_SCORE", canonical, market, min_score, lo=0.0, hi=100.0
            )
            if min_score_floor >= min_score:
                min_score_sources.append(floor_source)
            min_score = max(
                min_score,
                min_score_floor,
            )

        profile_min_score = self._profile_float(canonical, "min_score")
        if profile_min_score is not None:
            if profile_min_score >= min_score:
                min_score_sources.append("symbol_profiles.json:min_score")
            else:
                min_score_sources.append("symbol_profiles.json:min_score_clamped_to_env")
            min_score = max(min_score, profile_min_score)

        base_reject_at_or_below = _env_float("MT5_SCORE_REJECT_AT_OR_BELOW", 0.0, lo=0.0, hi=100.0)
        reject_at_or_below, reject_source = self._env_float_for_symbol_with_source(
            "MT5_SCORE_REJECT_AT_OR_BELOW",
            canonical,
            market,
            base_reject_at_or_below,
            lo=0.0,
            hi=100.0,
        )
        if reject_at_or_below >= min_score and min_score > 0:
            capped = max(0.0, min_score - 1.0)
            conditions["MT5 reject floor capped"] = f"{reject_at_or_below:.0f}->{capped:.0f}"
            reject_at_or_below = capped
        if reject_at_or_below > 0.0 and score <= reject_at_or_below:
            return False, f"quality score too low ({score:.0f} <= {reject_at_or_below:.0f})"

        thresholds["quality"] = {
            "score": round(score, 2),
            "min_score": round(min_score, 2),
            "min_score_sources": min_score_sources,
            "reject_at_or_below": round(reject_at_or_below, 2),
            "reject_source": reject_source,
            "strategy": strategy,
            "env": self._env_threshold_snapshot(
                [
                    "MT5_MIN_SCORE",
                    "MT5_INDEX_MIN_SCORE",
                    "MT5_STOCK_MIN_SCORE",
                    "MT5_ALT_MARKET_MIN_SCORE",
                    "MT5_PULLBACK_MIN_SCORE",
                    "MT5_MEAN_REV_MIN_SCORE",
                    "MT5_PRIORITY_MIN_SCORE",
                    "MT5_SCORE_REJECT_AT_OR_BELOW",
                    "MT5_PRIORITY_CONFIRMED_MIN_SCORE",
                    "MT5_PRIORITY_CONFIRMED_SESSION_SCORE",
                ],
                canonical,
                market,
            ),
        }
        if score < min_score:
            if priority_symbol and strategy.startswith("PRIORITY_"):
                confirmed_min = _env_float("MT5_PRIORITY_CONFIRMED_MIN_SCORE", 80.0, lo=0.0, hi=100.0)
                min_session = _env_float("MT5_PRIORITY_CONFIRMED_SESSION_SCORE", 65.0, lo=0.0, hi=100.0)
                details = _score_details_from_signal(sig, market)
                session_score = float(sig.get("session_score") or details.get("session_score") or 0.0)
                execution_score = float(sig.get("execution_1m_score") or 0.0)
                smc_score = float(sig.get("smc_score") or 0.0)
                execution_label = str(sig.get("execution_1m_result") or "").lower()
                execution_confirmed = (
                    execution_score > 0.0
                    or smc_score > 0.0
                    or bool(sig.get("smc_confirmation"))
                    or execution_label.startswith("confirmed")
                )
                session_confirmed = session_score >= min_session
                thresholds["quality"]["priority_confirmed_min_score"] = round(confirmed_min, 2)
                thresholds["quality"]["priority_confirmed_session_score"] = round(min_session, 2)
                thresholds["quality"]["priority_execution_confirmed"] = execution_confirmed
                thresholds["quality"]["priority_session_confirmed"] = session_confirmed
                if score >= confirmed_min and (session_confirmed or execution_confirmed):
                    conditions["MT5 priority confirmed floor"] = True
                    conditions["MT5 priority session confirm"] = session_confirmed
                    conditions["MT5 priority execution confirm"] = execution_confirmed
                    min_score = confirmed_min
                    thresholds["quality"]["min_score"] = round(min_score, 2)
                    thresholds["quality"]["min_score_sources"].append("MT5_PRIORITY_CONFIRMED_MIN_SCORE")
                else:
                    return (
                        False,
                        "quality score too low "
                        f"({score:.0f} < {min_score:.0f}; priority confirm needs "
                        f"{confirmed_min:.0f}+ with session {session_score:.0f}/{min_session:.0f} "
                        "or execution/SMC)",
                    )
            else:
                return False, f"quality score too low ({score:.0f} < {min_score:.0f})"

        direction = str(sig.get("direction") or "").upper()
        adx = float(sig.get("adx") or 0.0)
        vwap_z = float(sig.get("vwap_z") or 0.0)
        stoch_k = float(sig.get("stoch_k") or 50.0)
        stoch_d = float(sig.get("stoch_d") or 50.0)
        conditions["MT5 min score"] = score >= min_score
        conditions["MT5 min score required"] = round(min_score, 2)

        if strategy == "MEAN_REV":
            default_min_z = {
                "forex": 1.2,
                "stock_us": 1.5,
                "index_cfd": 1.4,
                "metal": 1.3,
                "energy": 1.3,
                "crypto": 1.6,
            }.get(market, 1.5)
            default_max_adx = {
                "forex": 28.0,
                "stock_us": 24.0,
                "index_cfd": 26.0,
                "metal": 30.0,
                "energy": 28.0,
                "crypto": 25.0,
            }.get(market, 24.0)
            min_z = self._env_float_for_symbol(
                "MT5_MEAN_REV_MIN_VWAP_Z",
                canonical,
                market,
                _env_float("MT5_MEAN_REV_MIN_VWAP_Z", default_min_z, lo=0.0),
                lo=0.0,
                hi=10.0,
            )
            max_adx = self._env_float_for_symbol(
                "MT5_MEAN_REV_MAX_ADX",
                canonical,
                market,
                _env_float("MT5_MEAN_REV_MAX_ADX", default_max_adx, lo=0.0),
                lo=0.0,
                hi=100.0,
            )
            thresholds["mean_reversion"] = {
                "min_vwap_z": round(min_z, 4),
                "max_adx": round(max_adx, 2),
                "adx_buffer": self._env_float_for_symbol("MT5_MEAN_REV_ADX_BUFFER", canonical, market, 0.0, lo=0.0, hi=20.0),
                "soft_score": _env_float("MT5_MEAN_REV_ADX_SOFT_SCORE", 95.0, lo=0.0, hi=100.0),
                "require_stoch": _env_bool("MT5_MEAN_REV_REQUIRE_STOCH", False),
                "env": self._env_threshold_snapshot(
                    [
                        "MT5_MEAN_REV_MIN_VWAP_Z",
                        "MT5_MEAN_REV_MAX_ADX",
                        "MT5_MEAN_REV_ADX_BUFFER",
                        "MT5_MEAN_REV_ADX_SOFT_SCORE",
                        "MT5_MEAN_REV_REQUIRE_STOCH",
                    ],
                    canonical,
                    market,
                ),
            }
            if abs(vwap_z) < min_z:
                if _mt5_strategy_engines is not None and hasattr(_mt5_strategy_engines, "mean_reversion_zscore_gate"):
                    z_ok, adjusted_score, z_reason, z_meta = _mt5_strategy_engines.mean_reversion_zscore_gate(
                        canonical,
                        vwap_z,
                        min_z,
                        score,
                    )
                    thresholds["mean_reversion"]["zscore_gate"] = self._audit_json_safe(z_meta)
                    conditions["MT5 mean reversion zscore gate"] = str(z_meta.get("decision") or "")
                    if adjusted_score != score:
                        sig["score_before_zscore"] = score
                        sig["score"] = float(adjusted_score)
                        sig["score_after_zscore"] = float(adjusted_score)
                        score = float(adjusted_score)
                    if not z_ok:
                        return False, z_reason
                else:
                    return False, f"mean reversion z-score too shallow ({vwap_z:.2f} < {min_z:.2f})"
            adx_buffer = self._env_float_for_symbol("MT5_MEAN_REV_ADX_BUFFER", canonical, market, 0.0, lo=0.0, hi=20.0)
            soft_score = _env_float("MT5_MEAN_REV_ADX_SOFT_SCORE", 95.0, lo=0.0, hi=100.0)
            adx_soft_ok = adx_buffer > 0 and score >= soft_score and adx <= max_adx + adx_buffer
            if market not in {"forex", "index_cfd", "index_cfd_eu", "metal"} and adx > max_adx and not adx_soft_ok:
                return False, f"mean reversion ADX too high ({adx:.1f} > {max_adx:.1f})"
            if market in {"forex", "index_cfd", "index_cfd_eu", "metal"}:
                conditions["MT5 mean reversion ADX handled by strategy engine"] = True
            stoch_ok = (direction == "BUY" and stoch_k <= 20 and stoch_k <= stoch_d) or (
                direction == "SELL" and stoch_k >= 80 and stoch_k >= stoch_d
            )
            conditions["MT5 mean reversion stochastic"] = stoch_ok
            if _env_bool("MT5_MEAN_REV_REQUIRE_STOCH", False) and not stoch_ok:
                if direction == "BUY":
                    return False, f"mean reversion BUY lacks stochastic exhaustion ({stoch_k:.1f}/{stoch_d:.1f})"
                if direction == "SELL":
                    return False, f"mean reversion SELL lacks stochastic exhaustion ({stoch_k:.1f}/{stoch_d:.1f})"
            conditions["MT5 deep mean reversion"] = True

        return True, "OK"

    def _spread_allows_entry(self, sym: str, market: str, sig: dict, price: float, atr: float) -> tuple[bool, str]:
        canonical = self._canonical_from_resolved(sym).upper()

        def finish(allowed: bool, reason: str, meta: dict | None = None) -> tuple[bool, str]:
            payload = {
                "event": "spread_gate",
                "decision": "allow" if allowed else "block",
                "symbol": canonical,
                "market": market,
                "engine": str((sig or {}).get("engine") or ""),
                "asset_class": str((sig or {}).get("asset_class") or ""),
                "reason": reason,
                "score": float((sig or {}).get("score") or 0.0),
                "min_score": float((sig or {}).get("min_score") or 0.0),
                "spread": self._audit_json_safe(meta or {}),
            }
            try:
                self._audit_decision(payload)
                _ss.set_status("last_spread_gate", f"{payload['decision'].upper()} {canonical}: {reason}"[:240])
            except Exception:
                pass
            self._trace_step(
                sig,
                "SPREAD_GATE_APPLIED",
                "PASS" if allowed else "BLOCKED",
                reason,
                spread_function=str((sig or {}).get("spread_function") or ""),
                spread=meta or {},
            )
            if not allowed:
                self._mark_blocked(sig, "SPREAD_GATE_APPLIED", reason)
            return allowed, reason

        try:
            resolved, tick = self.gateway.symbol_tick(sym)
        except Exception as exc:
            return finish(False, f"tick unavailable: {str(exc)[:120]}", {"error": str(exc)[:160]})
        bid = float(getattr(tick, "bid", 0.0) or 0.0)
        ask = float(getattr(tick, "ask", 0.0) or 0.0)
        if bid <= 0 or ask <= 0 or ask < bid:
            return finish(False, f"invalid tick for {resolved} (bid={bid} ask={ask})", {"bid": bid, "ask": ask})
        mid = (bid + ask) / 2.0
        spread = ask - bid
        spread_pct = spread / mid if mid > 0 else 0.0
        cfg = _mt5_strategy_engines.get_symbol_config(canonical) if _mt5_strategy_engines is not None else None
        engine = str(sig.get("engine") or (cfg or {}).get("engine") or "")
        asset_class = str(sig.get("asset_class") or (cfg or {}).get("asset_class") or "")
        if engine and not sig.get("engine"):
            sig["engine"] = engine
        if asset_class and not sig.get("asset_class"):
            sig["asset_class"] = asset_class

        engine_key = engine.upper()
        spread_engine = (
            _mt5_strategy_engines.FOREX_ENGINE if engine_key.startswith("FOREX_")
            else _mt5_strategy_engines.INDEX_ENGINE if engine_key.startswith("INDEX_")
            else _mt5_strategy_engines.METALS_ENGINE if engine_key.startswith(("METAL_", "METALS_"))
            else engine
        ) if _mt5_strategy_engines is not None else engine
        if (
            _mt5_strategy_engines is not None
            and spread_engine in {
                _mt5_strategy_engines.FOREX_ENGINE,
                _mt5_strategy_engines.INDEX_ENGINE,
                _mt5_strategy_engines.METALS_ENGINE,
            }
        ):
            score_before = float(sig.get("score") or 0.0)
            spread_ok, adjusted_score, spread_reason, spread_meta = _mt5_strategy_engines.spread_gate_for_engine(
                canonical,
                spread_engine,
                bid,
                ask,
                atr,
                score_before,
            )
            spread_atr = float(spread_meta.get("spread_to_atr") or 0.0)
            spread_snapshot = {
                "resolved_symbol": resolved,
                "bid": bid,
                "ask": ask,
                "mid": mid,
                "spread": spread,
                "spread_pct": spread_pct,
                "spread_atr": spread_atr,
                "spread_to_atr": spread_atr,
                "atr": atr,
                **spread_meta,
            }
            sig["spread_pct"] = spread_pct
            sig["spread_signal"] = spread
            sig["spread_signal_pct"] = spread_pct
            sig["spread_signal_atr"] = spread_atr
            sig["_mt5_spread_signal"] = spread_snapshot
            thresholds = sig.setdefault("_mt5_thresholds", {})
            thresholds["spread"] = {
                "engine": engine,
                "asset_class": asset_class,
                "decision": spread_meta.get("decision"),
                "reason": spread_reason,
                "legacy_profile_spread_fields_ignored": [
                    field for field in ("max_spread_atr_frac", "max_spread_pct") if self._profile_float(canonical, field) is not None
                ],
                **spread_meta,
            }
            conditions = sig.setdefault("conditions", {})
            conditions["MT5 spread gate"] = str(spread_meta.get("decision") or "")
            conditions["MT5 spread/ATR"] = bool(spread_ok)
            conditions["MT5 spread/ATR ratio"] = round(spread_atr, 4)
            if spread_engine == _mt5_strategy_engines.FOREX_ENGINE:
                conditions["MT5 spread pips"] = round(float(spread_meta.get("spread_pips") or 0.0), 2)
                conditions["MT5 max spread pips"] = round(float(spread_meta.get("max_spread_pips") or 0.0), 2)
            else:
                conditions["MT5 spread points"] = round(float(spread_meta.get("spread_points") or 0.0), 2)
                conditions["MT5 max spread points"] = round(float(spread_meta.get("max_spread_points") or 0.0), 2)
            conditions["MT5 spread/ATR limit"] = round(float(spread_meta.get("max_spread_atr_ratio") or 0.0), 4)
            if adjusted_score != score_before and not bool(sig.get("replacement_strategy_v1")):
                sig["score_before_spread"] = score_before
                sig["score"] = float(adjusted_score)
                sig["score_after_spread"] = float(adjusted_score)
                conditions["MT5 spread score penalty"] = f"{score_before:.0f}->{adjusted_score:.0f}"
            min_score = float(sig.get("min_score") or 0.0)
            if spread_ok and min_score > 0 and adjusted_score < min_score and not bool(sig.get("replacement_strategy_v1")):
                spread_ok = False
                spread_meta["decision"] = "block_after_soft_penalty"
                spread_snapshot["decision"] = "block_after_soft_penalty"
                thresholds["spread"]["decision"] = "block_after_soft_penalty"
                spread_reason = f"{canonical} spread penalty score below min_score ({adjusted_score:.0f} < {min_score:.0f})"
                conditions["MT5 spread/ATR"] = False
            return finish(bool(spread_ok), "OK" if spread_ok else spread_reason, spread_snapshot)

        spread_atr = spread / atr if atr > 0 else 0.0

        max_spread_atr = self._env_float_for_symbol(
            "MT5_MAX_SPREAD_ATR_FRAC",
            canonical,
            market,
            _env_float("MT5_MAX_SPREAD_ATR_FRAC", 0.24, lo=0.0),
            lo=0.0,
        )
        if market in {"index_cfd", "index_cfd_eu"} and os.getenv("MT5_INDEX_MAX_SPREAD_ATR_FRAC") is not None:
            max_spread_atr = _env_float("MT5_INDEX_MAX_SPREAD_ATR_FRAC", max_spread_atr, lo=0.0)
        if market == "stock_us" and os.getenv("MT5_STOCK_MAX_SPREAD_ATR_FRAC") is not None:
            max_spread_atr = _env_float("MT5_STOCK_MAX_SPREAD_ATR_FRAC", max_spread_atr, lo=0.0)
        if self._is_priority_symbol(sym):
            max_spread_atr = self._env_float_for_symbol(
                "MT5_PRIORITY_MAX_SPREAD_ATR_FRAC",
                canonical,
                market,
                _env_float("MT5_PRIORITY_MAX_SPREAD_ATR_FRAC", max_spread_atr, lo=0.0),
                lo=0.0,
            )
            if market in {"index_cfd", "index_cfd_eu"}:
                max_spread_atr = _env_float("MT5_PRIORITY_INDEX_MAX_SPREAD_ATR_FRAC", max_spread_atr, lo=0.0)
            elif market == "metal":
                max_spread_atr = _env_float("MT5_PRIORITY_METAL_MAX_SPREAD_ATR_FRAC", max_spread_atr, lo=0.0)
            elif market == "forex":
                max_spread_atr = _env_float("MT5_PRIORITY_FOREX_MAX_SPREAD_ATR_FRAC", max_spread_atr, lo=0.0)

        max_spread_pct = self._env_float_for_symbol(
            "MT5_MAX_SPREAD_PCT",
            canonical,
            market,
            _env_float("MT5_MAX_SPREAD_PCT", 0.0008, lo=0.0),
            lo=0.0,
        )
        if market == "stock_us" and os.getenv("MT5_STOCK_MAX_SPREAD_PCT") is not None:
            max_spread_pct = _env_float("MT5_STOCK_MAX_SPREAD_PCT", max_spread_pct, lo=0.0)

        spread_snapshot = {
            "resolved_symbol": resolved,
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "spread": spread,
            "spread_pct": spread_pct,
            "spread_atr": spread_atr,
            "atr": atr,
        }
        sig["spread_pct"] = spread_pct
        sig["spread_signal"] = spread
        sig["spread_signal_pct"] = spread_pct
        sig["spread_signal_atr"] = spread_atr
        sig["_mt5_spread_signal"] = spread_snapshot
        sig.setdefault("_mt5_thresholds", {})["spread"] = {
            "max_spread_atr_frac": round(max_spread_atr, 6),
            "max_spread_pct": round(max_spread_pct, 8),
            "env": self._env_threshold_snapshot(
                [
                    "MT5_MAX_SPREAD_ATR_FRAC",
                    "MT5_INDEX_MAX_SPREAD_ATR_FRAC",
                    "MT5_STOCK_MAX_SPREAD_ATR_FRAC",
                    "MT5_PRIORITY_MAX_SPREAD_ATR_FRAC",
                    "MT5_PRIORITY_INDEX_MAX_SPREAD_ATR_FRAC",
                    "MT5_PRIORITY_METAL_MAX_SPREAD_ATR_FRAC",
                    "MT5_PRIORITY_FOREX_MAX_SPREAD_ATR_FRAC",
                    "MT5_MAX_SPREAD_PCT",
                    "MT5_STOCK_MAX_SPREAD_PCT",
                ],
                canonical,
                market,
            ),
        }
        sig.setdefault("conditions", {})["MT5 spread/ATR"] = spread_atr <= max_spread_atr
        sig.setdefault("conditions", {})["MT5 spread/ATR limit"] = round(max_spread_atr, 4)
        sig.setdefault("conditions", {})["MT5 spread pct limit"] = round(max_spread_pct, 6)
        max_spread_atr = self._profile_cap_float(canonical, "max_spread_atr_frac", max_spread_atr)
        max_spread_pct = self._profile_cap_float(canonical, "max_spread_pct", max_spread_pct)
        sig.setdefault("_mt5_thresholds", {})["spread"]["profile_fields"] = [
            field for field in ("max_spread_atr_frac", "max_spread_pct") if self._profile_float(canonical, field) is not None
        ]
        sig.setdefault("_mt5_thresholds", {})["spread"]["max_spread_atr_frac"] = round(max_spread_atr, 6)
        sig.setdefault("_mt5_thresholds", {})["spread"]["max_spread_pct"] = round(max_spread_pct, 8)
        sig.setdefault("conditions", {})["MT5 spread/ATR limit"] = round(max_spread_atr, 4)
        sig.setdefault("conditions", {})["MT5 spread pct limit"] = round(max_spread_pct, 6)
        if atr > 0 and spread_atr > max_spread_atr:
            return finish(False, f"spread too wide ({spread_atr:.2f}x ATR > {max_spread_atr:.2f})", spread_snapshot)
        if max_spread_pct > 0 and spread_pct > max_spread_pct:
            return finish(False, f"spread pct too wide ({spread_pct:.3%} > {max_spread_pct:.3%})", spread_snapshot)
        return finish(True, "OK", spread_snapshot)

    def _systematic_csv_row(self, sym: str, market: str, sig: dict, status: str, reason: str = "",
                            *, cost: float | None = None, slippage: float | None = None,
                            profit_usd: float | None = None, r_result: float | None = None) -> dict:
        canonical = self._canonical_from_resolved(sym)
        return {
            "symbol": canonical,
            "engine": str(sig.get("engine") or ""),
            "strategy": str(sig.get("strategy_after") or sig.get("strategy") or sig.get("mode") or ""),
            "side": str(sig.get("direction") or ""),
            "score": float(sig.get("score") or 0.0),
            "status": str(status or ""),
            "block_reason": str(reason or ""),
            "spread": float(sig.get("spread_signal") or sig.get("spread_pct") or 0.0),
            "slippage": "" if slippage is None else float(slippage),
            "cost": "" if cost is None else float(cost),
            "R_result": "" if r_result is None else float(r_result),
            "profit_usd": "" if profit_usd is None else float(profit_usd),
            "session": self._normalized_session_name(sig.get("session")),
            "regime": str(sig.get("regime") or ""),
            "population": self._trade_population(sig, status),
        }

    def _systematic_prepare_signal(self, sym: str, market: str, sig: dict, details: dict) -> None:
        if self.systematic is None:
            return
        canonical = self._canonical_from_resolved(sym).upper()
        sig["symbol"] = canonical
        asset_class = str(sig.get("asset_class") or market or "")
        try:
            regime = self.systematic.regime.detect(sig, market)
            session = self.systematic.session.session_info(canonical, asset_class)
        except Exception as exc:
            _ss.set_status("systematic_prepare_error", str(exc)[:180])
            return
        sig["regime"] = str(regime.get("regime") or sig.get("regime") or "")
        sig["regime_reason"] = str(regime.get("reason") or "")
        sig["session"] = str(session.get("session") or "")
        sig["session_score"] = float(session.get("session_score") or details.get("session_score") or 0.0)
        sig["session_reason"] = str(session.get("reason") or "")
        sig.setdefault("conditions", {})["systematic regime"] = sig["regime"]
        sig.setdefault("conditions", {})["systematic session"] = sig["session"]
        sig.setdefault("_mt5_thresholds", {})["systematic"] = {"regime": regime, "session": session}
        self._trace_step(sig, "REGIME_DETECTED", "PASS", sig.get("regime_reason") or "regime detected", source="mt5_systematic_engine.RegimeDetector.detect", regime=regime, session=session)
        try:
            self.systematic.analytics.record_signal(**self._systematic_csv_row(canonical, market, sig, "RAW", "engine scored"))
        except Exception as exc:
            _ss.set_status("systematic_signal_csv_error", str(exc)[:180])

    def _systematic_health_allows_entry(self, sym: str, market: str, sig: dict) -> tuple[bool, str]:
        if self.systematic is None:
            return True, "OK"
        canonical = self._canonical_from_resolved(sym).upper()
        try:
            disabled, reason = self.systematic.health.is_symbol_disabled(canonical)
        except Exception as exc:
            _ss.set_status("systematic_health_error", str(exc)[:180])
            return True, "OK"
        if disabled:
            self._audit_decision({"event": "systematic_health_gate", "decision": "block", "symbol": canonical, "market": market, "reason": reason})
            return False, reason
        return True, "OK"

    def _systematic_cost_allows_entry(self, sym: str, market: str, sig: dict, volume: float,
                                      sl: float, tp: float, risk_meta: dict) -> tuple[bool, str]:
        if self.systematic is None:
            return True, "OK"
        price = float(sig.get("price") or 0.0)
        spread_snapshot = dict(sig.get("_mt5_spread_signal") or {})
        spread_price = float(spread_snapshot.get("spread") or sig.get("spread_signal") or 0.0)
        if bool(sig.get("replacement_strategy_v1")):
            self._audit_decision({
                "event": "CENTRAL_COST_MODEL_AUTHORITATIVE",
                "decision": "PASS",
                "symbol": self._canonical_from_resolved(sym),
                "market": market,
                "reason": "replacement pipeline uses broker-aware central CostModel; duplicate systematic cost veto skipped",
            })
            return True, "central broker-aware cost model authoritative"

        spec = (risk_meta or {}).get("spec") or {}
        contract_size = float(spec.get("contract_size") or 1.0) if isinstance(spec, dict) else 1.0
        planned_risk = float((risk_meta or {}).get("planned_risk") or 0.0)
        expected_profit = 0.0
        if planned_risk > 0 and abs(price - sl) > 0:
            expected_profit = planned_risk * (abs(tp - price) / abs(price - sl))
        try:
            cost = self.systematic.cost.estimate_trade_cost(
                self._canonical_from_resolved(sym), str(sig.get("direction") or ""), price, sl, tp, volume,
                spread_price=spread_price, contract_size=contract_size, expected_profit_usd=expected_profit or None,
            )
            allowed, reason, adjusted_score, details = self.systematic.cost.cost_gate(cost, float(sig.get("score") or 0.0))
        except Exception as exc:
            _ss.set_status("systematic_cost_error", str(exc)[:180])
            return False, f"systematic cost model failed: {str(exc)[:120]}"
        sig["cost_to_target_pct"] = float(cost.get("cost_to_target_pct") or 0.0)
        sig.setdefault("_mt5_thresholds", {})["systematic_cost"] = {"cost": cost, "gate": details}
        if adjusted_score != float(sig.get("score") or 0.0):
            sig["score_before_cost"] = float(sig.get("score") or 0.0)
            sig["score"] = float(adjusted_score)
            sig["score_after_cost"] = float(adjusted_score)
        if not allowed:
            self._mark_blocked(sig, "COST_GATE_APPLIED", reason)
            self._audit_decision({"event": "systematic_cost_gate", "decision": "block", "symbol": self._canonical_from_resolved(sym), "market": market, "reason": reason, "cost": cost})
            return False, reason
        self._trace_step(sig, "COST_GATE_APPLIED", "PASS", reason, cost=cost, details=details)
        self._audit_decision({"event": "systematic_cost_gate", "decision": "allow", "symbol": self._canonical_from_resolved(sym), "market": market, "reason": reason, "cost": cost})
        return True, "OK"

    def _systematic_portfolio_allows_entry(self, sym: str, market: str, sig: dict) -> tuple[bool, str]:
        if self.systematic is None:
            return True, "OK"
        canonical = self._canonical_from_resolved(sym).upper()
        try:
            open_positions = list(self.gateway.positions())
        except Exception as exc:
            return False, f"cannot confirm portfolio exposure: {str(exc)[:120]}"
        closed_trades = self._today_closed_trades()
        try:
            realized_today = float(self._broker_day_realized_pnl())
        except Exception:
            realized_today = 0.0
        allowed, reason, details = self.systematic.portfolio.can_trade(
            canonical, str(sig.get("engine") or ""), str(sig.get("direction") or ""),
            daily_loss_usd=realized_today,
            trades_today=self._daily_trade_count(),
            open_positions=open_positions,
            closed_trades=closed_trades,
            asset_class=str(sig.get("asset_class") or market or ""),
        )
        sig.setdefault("_mt5_thresholds", {})["systematic_portfolio"] = details
        self._audit_decision({"event": "systematic_portfolio_gate", "decision": "allow" if allowed else "block", "symbol": canonical, "market": market, "reason": reason, "details": details})
        if not allowed:
            self._mark_blocked(sig, "PORTFOLIO_RISK_GATE_APPLIED", reason)
            return False, reason
        self._trace_step(sig, "PORTFOLIO_RISK_GATE_APPLIED", "PASS", reason, risk_gate_function="mt5_systematic_engine.PortfolioRisk.can_trade", details=details)
        multiplier = float(details.get("risk_multiplier") or 1.0)
        if multiplier < 1.0:
            sig.setdefault("conditions", {})["systematic risk multiplier"] = multiplier
        return True, "OK"

    def _systematic_execution_quality_allows_entry(self, sym: str, market: str, sig: dict,
                                                   volume: float, sl: float, tp: float) -> tuple[bool, str]:
        if self.systematic is None:
            return True, "OK"
        canonical = self._canonical_from_resolved(sym).upper()
        engine = str(sig.get("engine") or "")
        price = float(sig.get("price") or 0.0)
        atr = float(sig.get("atr") or 0.0)
        tick_ok = False
        tick_reason = "tick execution module unavailable"
        tick_info = {}
        if _mt5_strategy_engines is not None:
            try:
                _resolved, tick = self.gateway.symbol_tick(sym)
                bid = float(getattr(tick, "bid", 0.0) or 0.0)
                ask = float(getattr(tick, "ask", 0.0) or 0.0)
                tick_ok, tick_reason, tick_info = _mt5_strategy_engines.tick_execution_check(
                    canonical, engine, bid, ask, price, atr, float(sig.get("score") or 0.0)
                )
            except Exception as exc:
                tick_ok, tick_reason, tick_info = False, f"tick check failed before final order: {str(exc)[:120]}", {}
        duplicate_ok, duplicate_reason = self._duplicate_order_allows_entry(sym, str(sig.get("direction") or ""))
        market_open = self._entry_window_open(market)
        allowed, reason, details = self.systematic.execution_quality.pre_order_check(
            canonical, str(sig.get("direction") or ""), price, sl, tp, volume,
            market_open=market_open, duplicate_ok=duplicate_ok, tick_ok=bool(tick_ok),
            lot_ok=volume > 0, stop_ok=sl > 0 and tp > 0,
        )
        details["tick_reason"] = tick_reason
        details["duplicate_reason"] = duplicate_reason
        details["tick_info"] = tick_info
        sig["_systematic_execution_quality"] = details
        self._audit_decision({"event": "systematic_execution_quality_gate", "decision": "allow" if allowed else "block", "symbol": canonical, "market": market, "reason": reason if allowed else (reason + "; " + tick_reason if not tick_ok else reason), "details": details})
        if not allowed:
            if not tick_ok:
                self._mark_blocked(sig, "ORDER_EXECUTION_CHECK", tick_reason)
                return False, tick_reason
            if not duplicate_ok:
                self._mark_blocked(sig, "ORDER_EXECUTION_CHECK", duplicate_reason)
                return False, duplicate_reason
            self._mark_blocked(sig, "ORDER_EXECUTION_CHECK", reason)
            return False, reason
        self._trace_step(sig, "ORDER_EXECUTION_CHECK", "PASS", reason, details=details)
        return True, "OK"

    def _log_forex_pre_pending_decision(self, sym: str, sig: dict | None, status: str = "", reason: str = "", pending_created: bool = False) -> None:
        if not isinstance(sig, dict):
            return
        canonical = canonical_symbol(self._canonical_from_resolved(sym)).upper()
        engine = str(sig.get("engine") or get_engine_for_symbol(canonical) or "")
        if engine != FOREX_ENGINE:
            return
        cfg = get_symbol_config(canonical) or {}
        try:
            min_score = float(sig.get("min_score") or cfg.get("min_score") or 0.0)
        except Exception:
            min_score = 0.0
        try:
            pending_floor = float(sig.get("pending_floor") or self._effective_pending_floor(canonical, min_score, engine))
        except Exception:
            pending_floor = max(0.0, min_score - 15.0)
        sig["pending_floor"] = pending_floor
        state = str(status or sig.get("final_status") or sig.get("state") or "").upper()
        try:
            score = float(sig.get("score") or sig.get("score_after") or 0.0)
        except Exception:
            score = 0.0
        low_vol_warning = bool(sig.get("low_vol_warning") or sig.get("low_vol_stage") == "scan_warning")
        pending_soft = bool(state == PENDING_SOFT or sig.get("soft_allow_used"))
        require_stronger = bool(sig.get("require_stronger_1m_confirmation"))
        cost_value = sig.get("cost_to_target_pct")
        cost_text = ""
        try:
            if cost_value is not None:
                cost_text = f"{float(cost_value):.6f}"
        except Exception:
            cost_text = str(cost_value or "")
        pre_pending_decision = state or (PENDING_SOFT if pending_soft else PENDING if pending_created else BLOCKED)
        sig["pre_pending_decision"] = pre_pending_decision
        sig["pending_soft"] = pending_soft
        sig["low_vol_warning"] = low_vol_warning
        final_reason = str(reason or sig.get("pending_reason") or sig.get("block_reason") or "OK").replace("\n", " ").replace("\r", " ")[:240]
        print(
            f"[MT5_FOREX_GATE] ruleset_version={RULESET_VERSION} engine={engine} symbol={canonical} "
            f"score={score:.1f} min_score={min_score:.1f} pending_floor={pending_floor:.1f} "
            f"cost_to_target={cost_text} low_vol_warning={str(low_vol_warning).lower()} "
            f"pre_pending_decision={pre_pending_decision} pending_soft={str(pending_soft).lower()} "
            f"require_stronger_1m_confirmation={str(require_stronger).lower()} status={state} "
            f"pending_created={1 if pending_created else 0} reason={final_reason}",
            flush=True,
        )

    def _engine_signal_base(self, canonical: str, market: str, group: str, cfg: dict, trigger: dict, score_result: dict, m5: pd.DataFrame, source_loop_name: str) -> dict:
        side = str(trigger.get("direction") or "").upper()
        p = _pack(m5)
        engine = str(cfg.get("engine") or get_engine_for_symbol(canonical))
        strategy = str(trigger.get("strategy_after") or cfg.get("strategy") or "")
        score_after = float(score_result.get("score_after") or 0.0)
        asset_label = "metals" if cfg.get("asset_class") == "metal" else str(cfg.get("asset_class") or "")
        sig = {
            "symbol": canonical, "market": market, "asset_class": str(cfg.get("asset_class") or ""), "engine": engine,
            "strategy": strategy, "mode": strategy, "strategy_before": str(trigger.get("strategy_before") or strategy), "strategy_after": strategy,
            "direction": side if side in {"BUY", "SELL"} else None,
            "regime": str(trigger.get("regime") or ""), "trigger_name": str(trigger.get("trigger_name") or ""), "trigger_reason": str(trigger.get("reason") or ""),
            "price": float(p.get("close") or 0.0), "atr": float(p.get("atr") or 0.0), "adx": float(p.get("adx") or 0.0), "rsi": float(p.get("rsi") or 50.0),
            "vwap": float(p.get("vwap") or 0.0), "vwap_z": float(p.get("vwap_z") or 0.0), "macd_h": 0.0, "vsurge": 0.0, "stoch_k": 50.0, "stoch_d": 50.0,
            "score": score_after, "score_before": float(score_result.get("score_before") or score_after), "score_after": score_after, "min_score": float(score_result.get("min_score") or cfg.get("min_score") or 0.0),
            "score_breakdown": dict(score_result.get("breakdown") or {}), "score_penalties": dict(score_result.get("penalties") or {}),
            "score_function": f"calculate_{asset_label}_score", "score_function_source": "mt5_bot.py",
            "adx_function": "apply_forex_adx_logic" if engine == FOREX_ENGINE else "apply_index_regime_logic" if engine == INDEX_ENGINE else "apply_metals_adx_logic",
            "spread_function": "forex_spread_gate" if engine == FOREX_ENGINE else "index_spread_gate" if engine == INDEX_ENGINE else "metals_spread_gate",
            "conditions": {"group": group, "engine selected per symbol": engine},
            "_mt5_thresholds": {"symbol_config": cfg, "engine_trigger": trigger, "engine_score": score_result},
            "decision_trace_id": self._new_decision_trace_id(canonical), "_decision_trace": [],
            "generated_at": datetime.utcnow().isoformat(), "source_loop_name": source_loop_name,
            "direction_source": f"{engine}_M5_TRIGGER", "evaluate_probability_master_gate": False,
            "ruleset_version": RULESET_VERSION, "build_id": BUILD_ID, "scanner_process_started_at": self.scanner_process_started_at,
            "row_source": "current", "historical": False, "timeframe_setup": "M5",
            "final_status": CANDIDATE if side in {"BUY", "SELL"} else NO_SETUP, "state": CANDIDATE if side in {"BUY", "SELL"} else NO_SETUP,
            "block_reason": "", "pending_reason": "",
        }
        self._trace_step(sig, "RAW_SCAN", "PASS", "raw scan started", symbol=canonical, engine=engine, source_loop_name=source_loop_name)
        self._trace_step(sig, "ENGINE_SELECTED", "PASS", "engine selected per symbol", engine=engine, asset_class=cfg.get("asset_class"), direction_source=sig["direction_source"], evaluate_probability_master_gate=False)
        self._trace_step(sig, "SCORE_CALCULATED", "PASS", "engine score calculated", score_function=sig.get("score_function"), score_before=sig.get("score_before"), score_after=sig.get("score_after"), min_score=sig.get("min_score"))
        return sig

    def _replacement_data_freshness(self, canonical: str, frames: dict) -> tuple[bool, str, str, dict]:
        now = datetime.utcnow()
        timeframe_seconds = {"H4": 14400, "H1": 3600, "M15": 900, "M5": 300, "M1": 60}
        max_age_seconds = {"H4": 18000, "H1": 5400, "M15": 1800, "M5": 720, "M1": 180}
        minimum_bars = {"H4": 60, "H1": 80, "M15": 60, "M5": 40, "M1": 3}
        evidence = {}
        problems = []
        for label, frame in frames.items():
            bars = _bars(frame)
            meta = dict(getattr(frame, "attrs", {}).get("bridge_rates") or {}) if frame is not None else {}
            row = {
                "bars": int(len(bars)) if bars is not None else 0,
                "minimum_bars": minimum_bars[label],
                "bridge_file": meta.get("bridge_file"),
                "bridge_mtime": meta.get("bridge_mtime"),
                "bridge_file_age_minutes": meta.get("bridge_file_age_minutes"),
                "broker_time_offset_seconds": meta.get("broker_time_offset_seconds"),
                "broker_time_offset_source": meta.get("broker_time_offset_source"),
                "raw_time": meta.get("last_bar_time_raw"),
                "normalized_time": meta.get("last_bar_time_normalized"),
                "receipt_time_utc": meta.get("receipt_time_utc"),
            }
            if bars is None or len(bars) < minimum_bars[label]:
                row["state"] = "DATA_MISSING"
                problems.append(f"{label} bars {row['bars']} < {minimum_bars[label]}")
                evidence[label] = row
                continue
            latest_open = _latest_bar_dt(bars)
            if latest_open is None:
                row["state"] = "DATA_MISSING"
                problems.append(f"{label} latest timestamp missing")
                evidence[label] = row
                continue
            latest_close = latest_open + timedelta(seconds=timeframe_seconds[label])
            age_seconds = (now - latest_close).total_seconds()
            row.update({
                "latest_completed_open_utc": latest_open.isoformat(),
                "latest_completed_close_utc": latest_close.isoformat(),
                "age_seconds": round(age_seconds, 3),
                "max_age_seconds": max_age_seconds[label],
            })
            if age_seconds < -120:
                row["state"] = "DATA_CLOCK_INVALID"
                problems.append(f"{label} completed candle is {-age_seconds:.0f}s in the future")
            elif age_seconds > max_age_seconds[label]:
                row["state"] = "DATA_STALE"
                problems.append(f"{label} age {age_seconds:.0f}s > {max_age_seconds[label]}s")
            else:
                row["state"] = "FRESH"
            evidence[label] = row
        try:
            _resolved, tick = self.gateway.symbol_tick(canonical)
            tick_epoch = int(getattr(tick, "time_utc", getattr(tick, "time", 0)) or 0)
            tick_age = time.time() - tick_epoch if tick_epoch > 0 else float("inf")
            tick_state = "FRESH" if -5.0 <= tick_age <= 30.0 else "DATA_CLOCK_INVALID" if tick_age < -5.0 else "DATA_STALE"
            evidence["TICK"] = {
                "state": tick_state,
                "time_broker": getattr(tick, "time_broker", None),
                "time_utc": tick_epoch,
                "broker_utc_offset_seconds": getattr(tick, "broker_utc_offset_seconds", None),
                "receipt_time_utc": str(getattr(tick, "receipt_time_utc", "")),
                "age_seconds": round(tick_age, 3),
                "max_age_seconds": 30.0,
            }
            if tick_state != "FRESH":
                problems.append(f"TICK {tick_state} age={tick_age:.1f}s")
        except Exception as exc:
            evidence["TICK"] = {"state": "DATA_MISSING", "error": str(exc)[:180]}
            problems.append(f"TICK unavailable: {str(exc)[:120]}")
        if problems:
            state = "DATA_MISSING" if any("missing" in item.lower() or "unavailable" in item.lower() for item in problems) else "DATA_STALE"
            return False, state, "; ".join(problems), evidence
        return True, "FRESH", "all H4/H1/M15/M5/M1 and tick inputs are fresh", evidence

    def _strategy_v1_frame(self, frame, label: str, completed_only: bool = False):
        if frame is None:
            return None
        if completed_only and len(frame) > 3:
            frame = frame.iloc[:-1]
        rows = []
        for idx, row in frame.iterrows():
            timestamp = idx.isoformat() if hasattr(idx, "isoformat") else str(idx)
            rows.append(_strategy_v1.Candle(
                timestamp,
                float(row.get("Open", 0.0) or 0.0),
                float(row.get("High", 0.0) or 0.0),
                float(row.get("Low", 0.0) or 0.0),
                float(row.get("Close", 0.0) or 0.0),
                float(row.get("Volume", 0.0) or 0.0),
            ))
        return _strategy_v1.Frame(label, tuple(rows))

    def _scan_symbol_replacement_v1(self, canonical: str, group: str, market: str, strategy_equity: float, cfg: dict, h4, h1, m15, m5, m1) -> bool:
        if _strategy_v1 is None:
            reason = "strategy_architecture_v1 unavailable; legacy engines disabled by live takeover"
            sig = {"symbol": canonical, "market": market, "engine": "STRATEGY_V1", "strategy": "STRATEGY_V1", "direction": None, "generated_at": datetime.utcnow().isoformat(), "final_status": NOT_QUALIFIED, "state": NOT_QUALIFIED, "not_qualified": True, "block_reason": reason, "decision_trace_id": self._new_decision_trace_id(canonical), "_decision_trace": []}
            self._upsert_signal(canonical, market, sig, 0.0, 0.0, reason, False, _score_details_from_signal(sig, market), group)
            self._audit_decision({"event": "STRATEGY_V1_UNAVAILABLE", "decision": "not_qualified", "symbol": canonical, "reason": reason})
            return True
        asset = str(cfg.get("asset_class") or "").lower()
        if asset not in {"forex", "index", "metal"}:
            return False
        raw_frames = {"H4": h4, "H1": h1, "M15": m15, "M5": m5, "M1": m1}
        freshness_ok, data_state, freshness_reason, freshness = self._replacement_data_freshness(canonical, raw_frames)
        if not freshness_ok:
            engine_name = {
                "forex": "FOREX_TREND_PULLBACK",
                "index": "INDEX_SESSION_BREAKOUT",
                "metal": "METAL_VOLATILITY_TREND",
            }[asset]
            sig = {
                "symbol": canonical,
                "market": market,
                "asset_class": asset,
                "engine": engine_name,
                "strategy": engine_name,
                "direction": None,
                "generated_at": datetime.utcnow().isoformat(),
                "final_status": NOT_QUALIFIED,
                "state": NOT_QUALIFIED,
                "not_qualified": True,
                "block_reason": f"{data_state} {freshness_reason}",
                "data_state": data_state,
                "timeframe_freshness": freshness,
                "replacement_strategy_v1": True,
                "decision_trace_id": self._new_decision_trace_id(canonical),
                "_decision_trace": [],
            }
            self._upsert_signal(canonical, market, sig, 0.0, 0.0, sig["block_reason"], False, _score_details_from_signal(sig, market), group)
            self._audit_decision({
                "event": data_state,
                "decision": "not_qualified",
                "symbol": canonical,
                "engine": engine_name,
                "reason": freshness_reason,
                "freshness": freshness,
            })
            print(f"[MT5_DATA_FRESHNESS] symbol={canonical} state={data_state} reason={freshness_reason}", flush=True)
            return True
        m5_closed_at, m5_trigger_id = _completed_candle_identity(m5, "M5", 300)
        if not m5_trigger_id:
            raise RuntimeError(f"fresh M5 data has no immutable candle identity for {canonical}")
        self.state.setdefault("last_scanned_m5_candles", {})[canonical] = m5_trigger_id
        self._save_state()
        _ss.set_status(f"last_m5_scan_{canonical}", f"NEW_COMPLETED_M5 {m5_trigger_id}")
        frames = {
            "H4": self._strategy_v1_frame(h4, "H4", completed_only=True),
            "H1": self._strategy_v1_frame(h1, "H1", completed_only=True),
            "M15": self._strategy_v1_frame(m15, "M15", completed_only=True),
            "M5": self._strategy_v1_frame(m5, "M5", completed_only=True),
            "M1": self._strategy_v1_frame(m1, "M1", completed_only=True),
        }
        engine_profiles = {
            "forex": {"stop_atr": 1.25, "target_r": 1.60, "spread_limit_atr": 0.35, "risk_pct": _env_float("MT5_RISK_PCT_FOREX", 2.0, lo=0.01, hi=10.0), "engine": "FOREX_TREND_PULLBACK"},
            "index": {"stop_atr": 1.50, "target_r": 1.80, "spread_limit_atr": 0.25, "risk_pct": _env_float("MT5_RISK_PCT_INDICES", 3.0, lo=0.01, hi=10.0), "engine": "INDEX_SESSION_BREAKOUT"},
            "metal": {"stop_atr": 1.60, "target_r": 1.80, "spread_limit_atr": 0.45, "risk_pct": _env_float("MT5_RISK_PCT_METALS", 1.0, lo=0.01, hi=10.0), "engine": "METAL_VOLATILITY_TREND"},
        }
        engine_profile = engine_profiles[asset]
        engine_profile["risk_pct"] = self._risk_pct_for_symbol(canonical, market)
        profile = _strategy_v1.Profile(
            canonical,
            asset,
            engine_profile["stop_atr"],
            engine_profile["target_r"],
            engine_profile["spread_limit_atr"],
            engine_profile["risk_pct"] / 100.0,
            "all",
        )
        decision = _strategy_v1.evaluate(canonical, profile, frames)
        m5_frame = frames["M5"]
        price = float(m5_frame.candles[-1].close) if m5_frame and m5_frame.candles else 0.0
        atr_value = float(decision.stop_distance / max(profile.stop_atr, 1e-12)) if decision.stop_distance > 0 else 0.0
        htf = (decision.evidence.get("higher_timeframes") or {}) if isinstance(decision.evidence, dict) else {}
        sig = {
            "symbol": canonical,
            "market": market,
            "asset_class": asset,
            "engine": decision.engine or engine_profile["engine"],
            "strategy": decision.engine or engine_profile["engine"],
            "strategy_before": decision.setup_type or decision.engine or engine_profile["engine"],
            "strategy_after": decision.engine or engine_profile["engine"],
            "mode": decision.setup_type or "STRATEGY_V1",
            "direction": decision.side,
            "setup_type": decision.setup_type,
            "regime": str((htf.get("M15") or {}).get("regime") or ""),
            "trigger_name": decision.setup_type,
            "trigger_reason": decision.reason,
            "price": price,
            "atr": atr_value,
            "score": float(decision.score or 0.0),
            "score_before": float(decision.score or 0.0),
            "score_after": float(decision.score or 0.0),
            "min_score": 60.0,
            "pending_floor": 55.0,
            "h4_bias": (htf.get("H4") or {}).get("side"),
            "h4_score": (htf.get("H4") or {}).get("score"),
            "h1_bias": (htf.get("H1") or {}).get("side"),
            "h1_score": (htf.get("H1") or {}).get("score"),
            "m15_bias": (htf.get("M15") or {}).get("side"),
            "m15_score": (htf.get("M15") or {}).get("score"),
            "setup_15m_score": (htf.get("M15") or {}).get("score"),
            "trigger_5m_score": float(decision.score or 0.0),
            "execution_1m_score": 20.0 if not decision.reasons else 0.0,
            "direction_source": "strategy_architecture_v1_M5_TRIGGER",
            "score_function": "strategy_architecture_v1.quality_score_v2",
            "score_function_source": "strategy_architecture_v1.py",
            "adx_function": "strategy_architecture_v1.adx_proxy",
            "spread_function": "strategy_architecture_v1.geometry",
            "replacement_strategy_v1": True,
            "timeframe_freshness": freshness,
            "replacement_decision": dict(decision.__dict__) if hasattr(decision, "__dict__") else {},
            "generated_at": datetime.utcnow().isoformat(),
            "source_loop_name": "strategy_v1_live",
            "decision_trace_id": self._new_decision_trace_id(canonical),
            "_decision_trace": [],
            "ruleset_version": "strategy_architecture_v1",
            "build_id": BUILD_ID,
            "scanner_process_started_at": self.scanner_process_started_at,
            "row_source": "current",
            "historical": False,
            "final_status": "NOT_QUALIFIED",
            "state": "NOT_QUALIFIED",
            "block_reason": "",
            "pending_reason": "",
        }
        trigger_candle = m5_frame.candles[-1] if m5_frame and m5_frame.candles else None
        if trigger_candle is not None:
            sig.update({
                "m5_time": trigger_candle.timestamp,
                "m5_open": trigger_candle.open,
                "m5_high": trigger_candle.high,
                "m5_low": trigger_candle.low,
                "m5_close": trigger_candle.close,
                "m5_trigger_candle_id": m5_trigger_id,
                "m5_candle_closed_at": m5_closed_at,
            })
        for frame_label in ("H4", "H1", "M15"):
            frame = frames.get(frame_label)
            candle = frame.candles[-1] if frame and frame.candles else None
            if candle is not None:
                prefix = frame_label.lower()
                sig.update({
                    f"{prefix}_time": candle.timestamp,
                    f"{prefix}_open": candle.open,
                    f"{prefix}_high": candle.high,
                    f"{prefix}_low": candle.low,
                    f"{prefix}_close": candle.close,
                })
        baseline_m1_closed_at, baseline_m1_candle_id = _completed_m1_candle_id(m1)
        sig["baseline_m1_closed_at"] = baseline_m1_closed_at
        sig["baseline_m1_candle_id"] = baseline_m1_candle_id
        self._audit_decision({
            "event": "STRATEGY_V1_DECISION",
            "decision": "pass" if decision.state == "PASS" else "not_qualified",
            "symbol": canonical,
            "asset_class": asset,
            "engine": decision.engine,
            "setup_type": decision.setup_type,
            "side": decision.side,
            "score": decision.score,
            "expected_edge_r": decision.expected_edge_r,
            "reasons": decision.reasons,
            "evidence": decision.evidence,
        })
        details = _score_details_from_signal(sig, market)
        if decision.state != "PASS":
            reason = "STRATEGY_V1_NOT_QUALIFIED " + (decision.reason or "no complete strategy pass")
            self._mark_not_qualified(sig, reason, "STRATEGY_V1")
            self._upsert_signal(canonical, market, sig, price, atr_value, reason, False, details, group)
            self.analytics_engine.record("NOT_QUALIFIED", canonical, sig["engine"], sig["strategy"], sig["score"], reason, sig)
            movement = decision.evidence.get("movement") if isinstance(decision.evidence, dict) else {}
            h4_meta = (htf.get("H4") or {}) if isinstance(htf, dict) else {}
            h1_meta = (htf.get("H1") or {}) if isinstance(htf, dict) else {}
            m15_meta = (htf.get("M15") or {}) if isinstance(htf, dict) else {}
            self._audit_decision({
                "event": "STRATEGY_V1_AUDIT",
                "decision": "NOT_QUALIFIED",
                "symbol": canonical,
                "engine": sig["engine"],
                "setup_type": decision.setup_type,
                "side": decision.side,
                "h4_bias": h4_meta.get("side"),
                "h4_score": h4_meta.get("score"),
                "h1_bias": h1_meta.get("side"),
                "h1_score": h1_meta.get("score"),
                "m15_bias": m15_meta.get("side"),
                "m15_score": m15_meta.get("score"),
                "m15_move_atr": (movement.get("M15") or {}).get("move_atr") if isinstance(movement, dict) else None,
                "m5_move_atr": (movement.get("M5") or {}).get("move_atr") if isinstance(movement, dict) else None,
                "reasons": decision.reasons,
            })
            print(
                f"[STRATEGY_V1_AUDIT] symbol={canonical} engine={sig['engine']} side={sig.get('direction') or ''} "
                f"H4={h4_meta.get('side')}:{h4_meta.get('score')} H1={h1_meta.get('side')}:{h1_meta.get('score')} "
                f"M15={m15_meta.get('side')}:{m15_meta.get('score')} "
                f"M15_move_atr={(movement.get('M15') or {}).get('move_atr') if isinstance(movement, dict) else ''} "
                f"M5_move_atr={(movement.get('M5') or {}).get('move_atr') if isinstance(movement, dict) else ''} "
                f"reason={reason}",
                flush=True,
            )
            return True
        profile_ok, profile_reason = self._symbol_profile_gate_allows_entry(canonical, market, sig)
        if not profile_ok:
            reason = f"STRATEGY_V1_NOT_QUALIFIED profile {profile_reason}"
            self._mark_not_qualified(sig, reason, "STRATEGY_V1_PRE_SETUP_QUALITY")
            self._upsert_signal(canonical, market, sig, price, atr_value, reason, False, details, group)
            self.analytics_engine.record("NOT_QUALIFIED", canonical, sig["engine"], sig["strategy"], sig["score"], reason, sig)
            return True

        spread_ok, spread_reason = self._spread_allows_entry(canonical, market, sig, price, atr_value)
        if not spread_ok:
            reason = f"STRATEGY_V1_NOT_QUALIFIED spread {spread_reason}"
            self._mark_not_qualified(sig, reason, "STRATEGY_V1_PRE_SETUP_SAFETY")
            self._upsert_signal(canonical, market, sig, price, atr_value, reason, False, details, group)
            self.analytics_engine.record("NOT_QUALIFIED", canonical, sig["engine"], sig["strategy"], sig["score"], reason, sig)
            return True
        pre_sl = price - decision.stop_distance if decision.side == "BUY" else price + decision.stop_distance
        pre_tp = price + decision.target_distance if decision.side == "BUY" else price - decision.target_distance
        cost_ok, cost_reason, adjusted_score, cost_details = self.cost_model.check(
            canonical, market, sig, 1.0, pre_sl, pre_tp, {}, stage="scan"
        )
        sig["score"] = float(adjusted_score)
        sig["score_after"] = float(adjusted_score)
        if not cost_ok:
            reason = f"STRATEGY_V1_NOT_QUALIFIED {cost_reason}"
            self._mark_not_qualified(sig, reason, "STRATEGY_V1_PRE_SETUP_SAFETY")
            self._upsert_signal(canonical, market, sig, price, atr_value, reason, False, details, group)
            self.analytics_engine.record("NOT_QUALIFIED", canonical, sig["engine"], sig["strategy"], sig["score"], reason, sig)
            self._audit_decision({
                "event": "EXTREME_COST_NOT_QUALIFIED", "decision": "NOT_QUALIFIED",
                "symbol": canonical, "side": decision.side, "reason": cost_reason,
                "stage": "PRE_SETUP", "details": cost_details,
            })
            return True

        if asset == "index":
            exhaustion_ok, exhaustion_reason, exhaustion_meta = self._index_exhaustion_allows_entry(canonical, market, sig)
            sig.setdefault("_mt5_thresholds", {})["index_exhaustion_pre_setup"] = exhaustion_meta
            if not exhaustion_ok:
                reason = f"STRATEGY_V1_NOT_QUALIFIED index exhaustion {exhaustion_reason}"
                self._mark_not_qualified(sig, reason, "STRATEGY_V1_PRE_SETUP_QUALITY")
                self._upsert_signal(canonical, market, sig, price, atr_value, reason, False, details, group)
                self.analytics_engine.record("NOT_QUALIFIED", canonical, sig["engine"], sig["strategy"], sig["score"], reason, sig)
                self._audit_decision({
                    "event": "INDEX_EXHAUSTION_NOT_QUALIFIED", "decision": "NOT_QUALIFIED",
                    "symbol": canonical, "side": decision.side, "reason": exhaustion_reason,
                    "stage": "PRE_SETUP", "details": exhaustion_meta,
                })
                return True

        sig["final_status"] = PENDING
        sig["state"] = PENDING
        created, reason = self.create_pending_setup(canonical, str(decision.side), sig["engine"], sig["strategy"], decision.score, sig)
        self._upsert_signal(canonical, market, sig, price, atr_value, reason, created, details, group)
        print(f"[STRATEGY_V1] symbol={canonical} engine={sig['engine']} state=PENDING side={decision.side} score={decision.score:.2f} pending_created={int(created)} reason={reason}", flush=True)
        return True

    def _scan_symbol(self, sym: str, group: str, market: str, strategy_equity: float) -> None:
        canonical = canonical_symbol(self._canonical_from_resolved(sym)).upper()
        cfg = get_symbol_config(canonical)
        source_loop_name = "full_scan"
        if not cfg:
            trace_stub = {"decision_trace_id": self._new_decision_trace_id(canonical), "_decision_trace": [], "generated_at": datetime.utcnow().isoformat(), "source_loop_name": source_loop_name, "ruleset_version": RULESET_VERSION, "build_id": BUILD_ID, "scanner_process_started_at": self.scanner_process_started_at, "state": NO_SETUP, "final_status": NO_SETUP, "direction_source": "NO_ENGINE", "evaluate_probability_master_gate": False, "row_source": "current", "conditions": {"group": group}}
            _ss.upsert_signal(canonical, market, "no_setup", None, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, NO_SETUP, "symbol not in SYMBOL_CONFIG", False, trace_stub)
            self._record_scan_gate(canonical, market, "symbol not in SYMBOL_CONFIG", False, False, NO_SETUP)
            self.analytics_engine.record(NO_SETUP, canonical, "", "", 0.0, "symbol not in SYMBOL_CONFIG", trace_stub)
            return
        engine = get_engine_for_symbol(canonical)
        if cfg.get("asset_class") == "forex":
            market = "forex"
        elif cfg.get("asset_class") == "index":
            market = market if market in {"index_cfd", "index_cfd_eu"} else self._market_for_symbol(canonical)
        elif cfg.get("asset_class") == "metal":
            market = "metal"
        try:
            m5_probe = self.gateway.rates(canonical, "M5", 3)
            _m5_closed_at, m5_probe_id = _completed_candle_identity(m5_probe, "M5", 300)
            last_m5_id = str(self.state.setdefault("last_scanned_m5_candles", {}).get(canonical) or "")
            if m5_probe_id and m5_probe_id == last_m5_id:
                _ss.set_status(f"last_m5_scan_{canonical}", f"NO_NEW_M5_CANDLE {m5_probe_id}")
                return
        except Exception:
            # The full load below records a precise DATA_MISSING result.
            pass
        try:
            h4 = self.gateway.rates(canonical, "H4", max(80, self.runtime.history_bars // 4))
            h1 = self.gateway.rates(canonical, "H1", max(120, self.runtime.history_bars // 2))
            m15 = self.gateway.rates(canonical, "M15", max(160, self.runtime.history_bars))
            m5 = self.gateway.rates(canonical, "M5", max(180, self.runtime.history_bars))
            m1 = self.gateway.rates(canonical, "M1", 61)
        except Exception as exc:
            trace_stub = {"decision_trace_id": self._new_decision_trace_id(canonical), "_decision_trace": [], "generated_at": datetime.utcnow().isoformat(), "source_loop_name": source_loop_name, "ruleset_version": RULESET_VERSION, "build_id": BUILD_ID, "scanner_process_started_at": self.scanner_process_started_at, "state": BLOCKED, "final_status": BLOCKED, "direction_source": f"{engine}_M5_TRIGGER" if engine else "NO_ENGINE", "evaluate_probability_master_gate": False, "row_source": "current", "conditions": {"group": group, "error": str(exc)[:240]}}
            _ss.upsert_signal(canonical, market, "no_data", None, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, BLOCKED, str(exc), False, trace_stub)
            self._record_scan_gate(canonical, market, str(exc), False, False, BLOCKED)
            self.analytics_engine.record(BLOCKED, canonical, engine, str(cfg.get("strategy") or ""), 0.0, str(exc)[:180], trace_stub)
            self._log_forex_pre_pending_decision(canonical, trace_stub, BLOCKED, str(exc), False)
            return
        try:
            _ss.upsert_candles(canonical, m5.tail(120))
        except Exception:
            pass
        if self._scan_symbol_replacement_v1(canonical, group, market, strategy_equity, cfg, h4, h1, m15, m5, m1):
            return
        raise RuntimeError(
            f"replacement strategy has no route for {canonical}/{cfg.get('asset_class')}; legacy fallback is retired"
        )
        if m5 is None or len(m5) == 0:
            reason = "no M5 bars"
            trace_stub = {"decision_trace_id": self._new_decision_trace_id(canonical), "_decision_trace": [], "generated_at": datetime.utcnow().isoformat(), "source_loop_name": source_loop_name, "ruleset_version": RULESET_VERSION, "build_id": BUILD_ID, "scanner_process_started_at": self.scanner_process_started_at, "state": BLOCKED, "final_status": BLOCKED, "direction_source": f"{engine}_M5_TRIGGER" if engine else "NO_ENGINE", "evaluate_probability_master_gate": False, "row_source": "current", "conditions": {"group": group, "hard_pre_pending_blocker": "no bars"}}
            _ss.upsert_signal(canonical, market, "no_data", None, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, BLOCKED, reason, False, trace_stub)
            self._record_scan_gate(canonical, market, reason, False, False, BLOCKED)
            self.analytics_engine.record(BLOCKED, canonical, engine, str(cfg.get("strategy") or ""), 0.0, reason, trace_stub)
            self._log_forex_pre_pending_decision(canonical, trace_stub, BLOCKED, reason, False)
            print(f"[MT5_ENGINE_SCAN] ruleset_version={RULESET_VERSION} symbol={canonical} engine={engine} state={BLOCKED} reason={reason} direction_source={trace_stub.get('direction_source')} evaluate_probability_master_gate=false pending_created=0", flush=True)
            return
        latest_bar = _latest_bar_dt(m5)
        max_stale_min = _env_float("MT5_SCAN_STALE_M5_MINUTES", 30.0, lo=1.0, hi=1440.0)
        bridge_meta = dict(getattr(m5, "attrs", {}).get("bridge_rates") or {})
        vps_now = datetime.utcnow()
        if latest_bar is not None:
            age_min = (vps_now - latest_bar).total_seconds() / 60.0
            bridge_file = str(bridge_meta.get("bridge_file") or "")
            bridge_mtime = str(bridge_meta.get("bridge_mtime") or "")
            bridge_file_age_min = bridge_meta.get("bridge_file_age_minutes")
            raw_bar_time = str(bridge_meta.get("last_bar_time_raw") or "")
            normalized_bar_time = str(bridge_meta.get("last_bar_time_normalized") or latest_bar.isoformat())
            broker_offset_seconds = int(bridge_meta.get("broker_time_offset_seconds") or 0)
            if not bridge_file:
                try:
                    if str(getattr(self.gateway, "mode", "")).lower() == "bridge":
                        resolved_for_m5 = self.gateway.ensure_symbol(canonical)
                        bridge_path = Path(self.gateway.config.bridge_dir) / f"rates_{resolved_for_m5}_M5.csv"
                        bridge_file = str(bridge_path)
                        if bridge_path.exists():
                            bridge_stat = bridge_path.stat()
                            bridge_mtime_dt = datetime.utcfromtimestamp(float(bridge_stat.st_mtime))
                            bridge_mtime = bridge_mtime_dt.isoformat()
                            bridge_file_age_min = (vps_now - bridge_mtime_dt).total_seconds() / 60.0
                        else:
                            bridge_mtime = "missing"
                except Exception as bridge_exc:
                    bridge_file = bridge_file or "bridge_debug_unavailable"
                    bridge_mtime = f"error:{str(bridge_exc)[:120]}"
            file_age_text = "" if bridge_file_age_min is None else f"{float(bridge_file_age_min):.1f}m"
            freshness_status = (
                f"file={bridge_file} mtime={bridge_mtime} file_age={file_age_text} "
                f"raw_bar={raw_bar_time} normalized_bar={normalized_bar_time} "
                f"vps_time={vps_now.isoformat()} age_minutes={age_min:.1f} "
                f"offset_seconds={broker_offset_seconds} threshold={max_stale_min:.1f}"
            )
            _ss.set_status(f"bridge_m5_freshness_{canonical}", freshness_status[:500])
            if age_min > max_stale_min:
                bridge_text = (
                    f" bridge_file={bridge_file} bridge_mtime={bridge_mtime} bridge_file_age={file_age_text}"
                    f" raw_bar_time={raw_bar_time} normalized_bar_time={normalized_bar_time}"
                    f" vps_time={vps_now.isoformat()} broker_offset_seconds={broker_offset_seconds}"
                )
                reason = f"stale M5 bars ({age_min:.1f}m > {max_stale_min:.1f}m);{bridge_text}"
                trace_stub = {"decision_trace_id": self._new_decision_trace_id(canonical), "_decision_trace": [], "generated_at": datetime.utcnow().isoformat(), "source_loop_name": source_loop_name, "ruleset_version": RULESET_VERSION, "build_id": BUILD_ID, "scanner_process_started_at": self.scanner_process_started_at, "state": BLOCKED, "final_status": BLOCKED, "direction_source": f"{engine}_M5_TRIGGER" if engine else "NO_ENGINE", "evaluate_probability_master_gate": False, "row_source": "current", "conditions": {"group": group, "latest_m5_bar": latest_bar.isoformat(), "latest_m5_bar_raw": raw_bar_time, "latest_m5_bar_normalized": normalized_bar_time, "vps_time": vps_now.isoformat(), "stale_age_minutes": age_min, "stale_threshold_minutes": max_stale_min, "bridge_file": bridge_file, "bridge_mtime": bridge_mtime, "bridge_file_age_minutes": bridge_file_age_min, "broker_time_offset_seconds": broker_offset_seconds, "hard_pre_pending_blocker": "stale bars"}}
                _ss.upsert_signal(canonical, market, "stale_bars", None, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, BLOCKED, reason, False, trace_stub)
                self._record_scan_gate(canonical, market, reason, False, False, BLOCKED)
                self.analytics_engine.record(BLOCKED, canonical, engine, str(cfg.get("strategy") or ""), 0.0, reason, trace_stub)
                self._log_forex_pre_pending_decision(canonical, trace_stub, BLOCKED, reason, False)
                print(f"[MT5_ENGINE_SCAN] ruleset_version={RULESET_VERSION} symbol={canonical} engine={engine} state={BLOCKED} reason={reason} direction_source={trace_stub.get('direction_source')} evaluate_probability_master_gate=false pending_created=0", flush=True)
                return
        if engine == FOREX_ENGINE:
            trigger = detect_forex_m5_trigger(canonical, m5, m15, h1)
            score_result = calculate_forex_score(canonical, str(trigger.get("direction") or ""), trigger, m5, m15, h1)
        elif engine == INDEX_ENGINE:
            trigger = detect_index_m5_trigger(canonical, m5, m15, h1)
            score_result = calculate_index_score(canonical, str(trigger.get("direction") or ""), trigger, m5, m15, h1)
        else:
            trigger = detect_metals_m5_trigger(canonical, m5, m15, h1)
            score_result = calculate_metals_score(canonical, str(trigger.get("direction") or ""), trigger, m5, m15, h1)
        sig = self._engine_signal_base(canonical, market, group, cfg, trigger, score_result, m5, source_loop_name)
        sig["m1_rows_loaded"] = int(len(m1) if m1 is not None else 0)
        self.analytics_engine.record(RAW_SCAN, canonical, engine, str(sig.get("strategy") or ""), float(sig.get("score") or 0.0), "raw scan", sig)
        price = float(sig.get("price") or 0.0)
        atr = float(sig.get("atr") or 0.0)
        details = _score_details_from_signal(sig, market)
        direction = sig.get("direction")
        if not direction:
            reason = str(trigger.get("reason") or "no setup")
            no_trigger_quality = "m5 trigger" in reason.lower()
            if no_trigger_quality:
                sig["not_qualified"] = True
                sig["final_status"] = "NOT_QUALIFIED"; sig["state"] = "NOT_QUALIFIED"; sig["block_reason"] = reason
                self._audit_decision({
                    "event": "NOT_QUALIFIED",
                    "decision": "not_qualified",
                    "scope": "candidate",
                    "symbol": canonical,
                    "engine": engine,
                    "reason": reason,
                })
                self._trace_step(sig, "NOT_QUALIFIED", "NOT_QUALIFIED", reason, quality_filter_step="M5_TRIGGER")
                status = "NOT_QUALIFIED"
            else:
                sig["final_status"] = NO_SETUP; sig["state"] = NO_SETUP; sig["block_reason"] = reason
                self._trace_step(sig, "NO_SETUP", NO_SETUP, reason)
                status = NO_SETUP
            self._upsert_signal(canonical, market, sig, price, atr, reason, False, details, group)
            self.analytics_engine.record(status, canonical, engine, str(sig.get("strategy") or ""), float(sig.get("score") or 0.0), reason, sig)
            print(f"[MT5_ENGINE_SCAN] ruleset_version={RULESET_VERSION} symbol={canonical} engine={engine} state={status} direction_source={sig.get('direction_source')} evaluate_probability_master_gate=false pending_created=0", flush=True)
            return
        htf_ok, htf_reason, htf_meta = self._shared_htf_setup_permission(canonical, market, cfg, sig, details, h4, h1, m15, m5)
        details = _score_details_from_signal(sig, market)
        if not htf_ok:
            self._mark_blocked(sig, "HTF_ALIGNMENT", htf_reason)
            self._upsert_signal(canonical, market, sig, price, atr, htf_reason, False, details, group)
            status = "NOT_QUALIFIED" if sig.get("not_qualified") else BLOCKED
            self.analytics_engine.record(status, canonical, engine, str(sig.get("strategy") or ""), float(sig.get("score") or 0.0), htf_reason, sig)
            print(f"[MT5_ENGINE_SCAN] ruleset_version={RULESET_VERSION} symbol={canonical} engine={engine} state={status} reason={htf_reason} direction_source={sig.get('direction_source')} evaluate_probability_master_gate=false pending_created=0", flush=True)
            return
        self.analytics_engine.record(CANDIDATE, canonical, engine, str(sig.get("strategy") or ""), float(sig.get("score") or 0.0), str(trigger.get("reason") or "candidate"), sig)
        score = float(sig.get("score") or 0.0)
        p = _pack(m5)
        h1_bias_aligned = str((trigger.get("metrics") or {}).get("h1_bias") or "") == str(direction)
        m15_structure_aligned = str((trigger.get("metrics") or {}).get("m15_bias") or "") == str(direction)
        if engine == FOREX_ENGINE:
            gate_ok, score, gate_reason, gate_meta = apply_forex_adx_logic(canonical, str(sig.get("strategy_after") or ""), float(p.get("adx") or 0.0), score, h1_bias_aligned, m15_structure_aligned)
            z_ok, score, z_reason, z_meta = mean_reversion_zscore_gate(canonical, float(p.get("vwap_z") or 0.0), 1.2, score)
            gate_ok = gate_ok and z_ok
            gate_meta["zscore_gate"] = z_meta
            if z_reason != "OK": gate_reason = z_reason
        elif engine == INDEX_ENGINE:
            gate_ok, score, gate_reason, gate_meta = apply_index_regime_logic(canonical, str(sig.get("regime") or ""), float(p.get("adx") or 0.0), str(sig.get("strategy_after") or ""), score, float(sig.get("score_before") or score))
            sig["soft_allow_used"] = bool(gate_meta.get("soft_allow_used")); sig["index_soft_allow"] = bool(gate_meta.get("soft_allow_used"))
            if gate_meta.get("require_stronger_1m_confirmation"):
                sig["require_stronger_1m_confirmation"] = True
        else:
            gate_ok, score, gate_reason, gate_meta = apply_metals_adx_logic(canonical, str(sig.get("strategy_after") or ""), float(p.get("adx") or 0.0), score, h1_bias_aligned, m15_structure_aligned)
        sig["score"] = float(score); sig["score_after"] = float(score); sig["adx_reason"] = gate_reason
        sig.setdefault("_mt5_thresholds", {})["engine_gate"] = gate_meta
        self._trace_step(sig, "ADX_LOGIC_APPLIED", "PASS" if gate_ok else "BLOCKED", gate_reason, adx_function=sig.get("adx_function"), gate=gate_meta)
        if engine == FOREX_ENGINE and (
            not gate_ok
            or str(gate_reason or "OK") != "OK"
            or str((gate_meta or {}).get("decision") or "").lower() in {"soft_penalty", "scan_warning"}
            or str(((gate_meta or {}).get("zscore_gate") or {}).get("decision") or "").lower() == "soft_penalty"
        ):
            sig["forex_adx_warning"] = True
            sig["require_stronger_1m_confirmation"] = True
        if not gate_ok:
            sig["engine_gate_stage"] = "scan_warning"
            self._trace_step(sig, "ADX_LOGIC_APPLIED", "WARN", gate_reason, engine_gate_stage="scan_warning")
        try:
            _resolved, tick = self.gateway.symbol_tick(canonical)
            bid = float(getattr(tick, "bid", 0.0) or 0.0); ask = float(getattr(tick, "ask", 0.0) or 0.0)
            score_before_spread = float(score)
            if engine == FOREX_ENGINE: spread_ok, spread_score, spread_reason, spread_meta = forex_spread_gate(canonical, bid, ask, atr, score)
            elif engine == INDEX_ENGINE: spread_ok, spread_score, spread_reason, spread_meta = index_spread_gate(canonical, bid, ask, atr, score)
            else: spread_ok, spread_score, spread_reason, spread_meta = metals_spread_gate(canonical, bid, ask, atr, score)
            if spread_ok and str(spread_meta.get("decision") or "").lower() == "soft_penalty":
                spread_meta["spread_stage"] = "scan_warning"
                spread_meta["score_before_scan_warning"] = score_before_spread
                spread_meta["score_after_scan_warning"] = score_before_spread
                if engine == FOREX_ENGINE:
                    spread_meta["forex_spread_warning"] = True
                    sig["forex_spread_warning"] = True
                    sig["require_stronger_1m_confirmation"] = True
                score = score_before_spread
            else:
                score = float(spread_score)
            sig["score"] = float(score); sig["score_after"] = float(score)
            sig["spread_signal"] = float(spread_meta.get("spread") or 0.0); sig["spread_signal_atr"] = float(spread_meta.get("spread_to_atr") or 0.0)
            sig["_mt5_spread_signal"] = spread_meta; sig.setdefault("_mt5_thresholds", {})["spread"] = spread_meta
            self._trace_step(sig, "SPREAD_GATE_APPLIED", "PASS" if spread_ok else "BLOCKED", spread_reason, spread_function=sig.get("spread_function"), spread=spread_meta)
        except Exception as exc:
            spread_ok, spread_reason = False, f"tick unavailable: {str(exc)[:120]}"
        if not spread_ok:
            self._mark_blocked(sig, "SPREAD_GATE_APPLIED", spread_reason)
            self._upsert_signal(canonical, market, sig, price, atr, spread_reason, False, details, group)
            self.analytics_engine.record(BLOCKED, canonical, engine, str(sig.get("strategy") or ""), float(sig.get("score") or 0.0), spread_reason, sig)
            print(f"[MT5_ENGINE_SCAN] ruleset_version={RULESET_VERSION} symbol={canonical} engine={engine} state={BLOCKED} reason={spread_reason} direction_source={sig.get('direction_source')} evaluate_probability_master_gate=false pending_created=0", flush=True)
            return
        scan_exec_ok, scan_exec_reason = execution_ok(price, atr, market=market)
        if not scan_exec_ok:
            reason_text = str(scan_exec_reason or "").lower()
            if price <= 0 or atr <= 0 or "invalid" in reason_text or "dead" in reason_text:
                reason = f"market dead/untradeable at scan: {scan_exec_reason}"
                self._mark_blocked(sig, "COST_GATE_APPLIED", reason)
                self._upsert_signal(canonical, market, sig, price, atr, reason, False, details, group)
                self.analytics_engine.record(BLOCKED, canonical, engine, str(sig.get("strategy") or ""), float(sig.get("score") or 0.0), reason, sig)
                print(f"[MT5_ENGINE_SCAN] ruleset_version={RULESET_VERSION} symbol={canonical} engine={engine} state={BLOCKED} reason={reason} direction_source={sig.get('direction_source')} evaluate_probability_master_gate=false pending_created=0", flush=True)
                return
            if engine == FOREX_ENGINE:
                penalty = _env_float("MT5_FOREX_SCAN_LOW_VOL_SCORE_PENALTY", 1.0, lo=0.0, hi=5.0)
            else:
                penalty = _env_float("MT5_SCAN_LOW_VOL_SCORE_PENALTY", 3.0, lo=0.0, hi=15.0)
            before_low_vol = float(sig.get("score") or 0.0)
            after_low_vol = max(0.0, before_low_vol - penalty)
            if engine == FOREX_ENGINE:
                low_vol_min_score = float(sig.get("min_score") or cfg.get("min_score") or 0.0)
                low_vol_pending_floor = self._effective_pending_floor(canonical, low_vol_min_score, engine)
                if before_low_vol >= low_vol_pending_floor and after_low_vol < low_vol_pending_floor:
                    after_low_vol = low_vol_pending_floor
                sig["low_vol_warning"] = True
                sig["low_vol_warning_reason"] = str(scan_exec_reason or "low volatility warning")
                sig["require_stronger_1m_confirmation"] = True
            sig["score"] = after_low_vol; sig["score_after"] = after_low_vol
            sig["low_vol_stage"] = "scan_warning"
            sig.setdefault("_mt5_thresholds", {})["low_vol_scan"] = {"low_vol_stage": "scan_warning", "reason": scan_exec_reason, "penalty": penalty, "score_before": before_low_vol, "score_after": after_low_vol, "require_stronger_1m_confirmation": bool(sig.get("require_stronger_1m_confirmation"))}
            self._trace_step(sig, "LOW_VOL_SCAN_WARNING", "WARN", scan_exec_reason, low_vol_stage="scan_warning", penalty=penalty, require_stronger_1m_confirmation=bool(sig.get("require_stronger_1m_confirmation")))
        if not self._entry_window_open(market):
            reason = "market closed - waiting for configured MT5 open window"
            self._mark_blocked(sig, "CONFIG_LOADED", reason)
            self._upsert_signal(canonical, market, sig, price, atr, reason, False, details, group)
            print(f"[MT5_ENGINE_SCAN] ruleset_version={RULESET_VERSION} symbol={canonical} engine={engine} state={BLOCKED} reason={reason} direction_source={sig.get('direction_source')} evaluate_probability_master_gate=false pending_created=0", flush=True)
            return
        time_block_reason = self._entry_time_block_reason(canonical, market)
        if time_block_reason:
            self._mark_blocked(sig, "CONFIG_LOADED", time_block_reason)
            self._upsert_signal(canonical, market, sig, price, atr, time_block_reason, False, details, group)
            print(f"[MT5_ENGINE_SCAN] ruleset_version={RULESET_VERSION} symbol={canonical} engine={engine} state={BLOCKED} reason={time_block_reason} direction_source={sig.get('direction_source')} evaluate_probability_master_gate=false pending_created=0", flush=True)
            return
        profile = self._symbol_profile(canonical)
        if isinstance(profile, dict):
            enabled_raw = profile.get("enabled", True)
            profile_enabled = str(enabled_raw).strip().lower() not in {"0", "false", "no", "off"} if isinstance(enabled_raw, str) else bool(enabled_raw)
            if not profile_enabled:
                reason = f"symbol profile disabled for {canonical}"
                self._mark_blocked(sig, "SYMBOL_HEALTH_GATE_APPLIED", reason)
                self._upsert_signal(canonical, market, sig, price, atr, reason, False, details, group)
                self.analytics_engine.record(BLOCKED, canonical, engine, str(sig.get("strategy") or ""), float(sig.get("score") or 0.0), reason, sig)
                print(f"[MT5_ENGINE_SCAN] ruleset_version={RULESET_VERSION} symbol={canonical} engine={engine} state={BLOCKED} reason={reason} direction_source={sig.get('direction_source')} evaluate_probability_master_gate=false pending_created=0", flush=True)
                return
        systematic_health_ok, systematic_health_reason = self._systematic_health_allows_entry(canonical, market, sig)
        if not systematic_health_ok:
            self._mark_blocked(sig, "SYMBOL_HEALTH_GATE_APPLIED", systematic_health_reason)
            self._upsert_signal(canonical, market, sig, price, atr, systematic_health_reason, False, details, group)
            self.analytics_engine.record(BLOCKED, canonical, engine, str(sig.get("strategy") or ""), float(sig.get("score") or 0.0), systematic_health_reason, sig)
            print(f"[MT5_ENGINE_SCAN] ruleset_version={RULESET_VERSION} symbol={canonical} engine={engine} state={BLOCKED} reason={systematic_health_reason} direction_source={sig.get('direction_source')} evaluate_probability_master_gate=false pending_created=0", flush=True)
            return
        if not self._daily_risk_allows_entry():
            reason = "daily loss cap already reached"
            self._mark_blocked(sig, "PORTFOLIO_RISK_GATE_APPLIED", reason)
            self._upsert_signal(canonical, market, sig, price, atr, reason, False, details, group)
            self.analytics_engine.record(BLOCKED, canonical, engine, str(sig.get("strategy") or ""), float(sig.get("score") or 0.0), reason, sig)
            print(f"[MT5_ENGINE_SCAN] ruleset_version={RULESET_VERSION} symbol={canonical} engine={engine} state={BLOCKED} reason={reason} direction_source={sig.get('direction_source')} evaluate_probability_master_gate=false pending_created=0", flush=True)
            return
        if self._open_position_count() >= self._effective_max_open_trades():
            reason = f"max open trades hard-stop reached ({self._effective_max_open_trades()})"
            self._mark_blocked(sig, "PORTFOLIO_RISK_GATE_APPLIED", reason)
            self._upsert_signal(canonical, market, sig, price, atr, reason, False, details, group)
            self.analytics_engine.record(BLOCKED, canonical, engine, str(sig.get("strategy") or ""), float(sig.get("score") or 0.0), reason, sig)
            print(f"[MT5_ENGINE_SCAN] ruleset_version={RULESET_VERSION} symbol={canonical} engine={engine} state={BLOCKED} reason={reason} direction_source={sig.get('direction_source')} evaluate_probability_master_gate=false pending_created=0", flush=True)
            return
        health_ok, health_reason = self.symbol_health_manager.check(canonical, sig)
        if health_reason and health_reason != "OK":
            sig["symbol_health_stage"] = "scan_warning"
            self._trace_step(sig, "SYMBOL_HEALTH_GATE_APPLIED", "WARN", health_reason, symbol_health_stage="scan_warning", health_ok=bool(health_ok))
        stop_mult = float(cfg.get("atr_stop_mult") or 1.3)
        stop_d = max(float(atr or 0.0) * stop_mult, _compute_stop_distance(price, atr, market=market, vwap_stop_dist=0.0, is_crypto=False))
        stop_d = self._cap_stop_distance_for_symbol(canonical, market, stop_d, sig)
        rr = self._entry_rr_for_symbol(canonical, market, sig, configured_rr=cfg.get("tp_r"))
        sl = price - stop_d if direction == "BUY" else price + stop_d
        tp = price + (stop_d * rr) if direction == "BUY" else price - (stop_d * rr)
        scan_risk_meta = {"spec": {"contract_size": 1.0}, "stage": "scan"}
        scan_cost_ok, scan_cost_reason, _scan_adjusted_score, scan_cost_details = self.cost_model.check(canonical, market, sig, 1.0, sl, tp, scan_risk_meta, stage="scan")
        self._trace_step(sig, "COST_GATE_APPLIED", "PASS" if scan_cost_ok else "BLOCKED", scan_cost_reason, cost=scan_cost_details, cost_to_target_stage=scan_cost_details.get("cost_to_target_stage"))
        if engine == FOREX_ENGINE and scan_cost_ok:
            try:
                forex_cost_to_target = float(sig.get("cost_to_target_pct") or scan_cost_details.get("cost_to_target") or 0.0)
                forex_cost_warn_threshold = float(scan_cost_details.get("strict_threshold") or _env_float("MT5_COST_TO_TARGET_BLOCK", 0.30, lo=0.0, hi=2.0))
            except Exception:
                forex_cost_to_target = 0.0
                forex_cost_warn_threshold = 0.30
            if forex_cost_to_target >= forex_cost_warn_threshold:
                sig["cost_to_target_warning"] = True
                sig["cost_to_target_stage"] = "scan_warning"
                sig["require_stronger_1m_confirmation"] = True
                scan_cost_details["forex_scan_warning"] = True
                self._trace_step(sig, "COST_GATE_APPLIED", "WARN", scan_cost_reason, cost_to_target_stage="scan_warning", cost_to_target=forex_cost_to_target)
        if not scan_cost_ok:
            self._mark_blocked(sig, "COST_GATE_APPLIED", scan_cost_reason)
            self._upsert_signal(canonical, market, sig, price, atr, scan_cost_reason, False, details, group)
            status = "NOT_QUALIFIED" if sig.get("not_qualified") else BLOCKED
            self.analytics_engine.record(status, canonical, engine, str(sig.get("strategy") or ""), float(sig.get("score") or 0.0), scan_cost_reason, sig)
            print(f"[MT5_ENGINE_SCAN] ruleset_version={RULESET_VERSION} symbol={canonical} engine={engine} state={status} reason={scan_cost_reason} cost_to_target_stage={sig.get('cost_to_target_stage')} direction_source={sig.get('direction_source')} evaluate_probability_master_gate=false pending_created=0", flush=True)
            return
        min_score = float(sig.get("min_score") or cfg.get("min_score") or 0.0)
        score = float(sig.get("score") or 0.0)
        pending_floor = self._effective_pending_floor(canonical, min_score, engine)
        sig["pending_floor"] = pending_floor
        strict_pending_floor = pending_floor_for_symbol(canonical, min_score, engine)
        if self._demo_winrate_test_effective() and pending_floor < strict_pending_floor:
            sig["strict_mode_block_reason_shadow"] = f"strict pending_floor would be {strict_pending_floor:.1f}"
            sig.setdefault("_mt5_thresholds", {}).setdefault("demo_winrate", {})["strict_pending_floor"] = strict_pending_floor
            if score >= pending_floor and score < strict_pending_floor:
                sig["why_trade_allowed_in_demo"] = "demo win-rate mode loosened pending floor by 5 points"
        sig.setdefault("_mt5_thresholds", {})["score_gate"] = {"min_score": min_score, "pending_floor": pending_floor, "strict_pending_floor": strict_pending_floor, "score": score, "require_stronger_1m_confirmation": bool(sig.get("require_stronger_1m_confirmation"))}
        if score >= min_score and not (engine == FOREX_ENGINE and bool(sig.get("require_stronger_1m_confirmation"))):
            state = PENDING
        elif score >= pending_floor:
            state = PENDING_SOFT
        else:
            reason = f"score below pending_floor ({score:.0f} < {pending_floor:.0f}; min_score {min_score:.0f})"
            self._mark_blocked(sig, "SCORE_CALCULATED", reason)
            self._upsert_signal(canonical, market, sig, price, atr, reason, False, details, group)
            status = "NOT_QUALIFIED" if sig.get("not_qualified") else BLOCKED
            self.analytics_engine.record(status, canonical, engine, str(sig.get("strategy") or ""), score, reason, sig)
            print(f"[MT5_ENGINE_SCAN] ruleset_version={RULESET_VERSION} symbol={canonical} engine={engine} state={status} score={score:.1f} min_score={min_score:.1f} pending_floor={pending_floor:.1f} reason={reason} direction_source={sig.get('direction_source')} evaluate_probability_master_gate=false pending_created=0", flush=True)
            return
        if state == PENDING_SOFT:
            sig["require_stronger_1m_confirmation"] = True
        sig["soft_allow_used"] = state == PENDING_SOFT
        sig["index_soft_allow"] = bool(state == PENDING_SOFT and engine == INDEX_ENGINE)
        sig["final_status"] = state; sig["state"] = state
        created, pending_reason = self.create_pending_setup(canonical, str(direction), engine, str(sig.get("strategy_after") or cfg.get("strategy") or ""), score, sig)
        if self._active_scan_stats is not None and created:
            self._active_scan_stats["pending_created"] += 1
        self._upsert_signal(canonical, market, sig, price, atr, pending_reason, True, details, group)
        print(f"[MT5_ENGINE_SCAN] ruleset_version={RULESET_VERSION} symbol={canonical} engine={engine} state={state} score={score:.1f} min_score={min_score:.1f} pending_floor={pending_floor:.1f} cost_to_target_stage={sig.get('cost_to_target_stage', '')} low_vol_stage={sig.get('low_vol_stage', '')} direction_source={sig.get('direction_source')} evaluate_probability_master_gate=false pending_created={1 if created else 0}", flush=True)
        return

    def _upsert_signal(self, sym: str, market: str, sig: dict, price: float, atr: float,
                       gate: str, gate_ok: bool, details: dict, group: str) -> None:
        self._assert_signal_engine_contract(sym, sig)
        quality = bool(sig.get("not_qualified")) or self._is_quality_filter_reason(gate or sig.get("block_reason") or "", sig)
        if not gate_ok and not quality:
            self._log_scan_block_reason(sym, str(sig.get("direction") or ""), str(sig.get("setup_id") or ""), str(sig.get("block_reason") or gate or ""), sig=sig)
        if gate_ok:
            sig.setdefault("final_status", "ACTIONABLE")
            sig.setdefault("block_reason", "")
            sig.setdefault("blocked_at_step", "")
        elif not sig.get("final_status"):
            if not sig.get("direction"):
                sig["final_status"] = "NO_SETUP"
                sig["block_reason"] = str(gate or "")
                sig["blocked_at_step"] = ""
            else:
                self._mark_blocked(sig, self._infer_block_step(gate), gate)
        self._log_forex_pre_pending_decision(sym, sig, str(sig.get("final_status") or ""), str(gate or ""), bool(sig.get("pending_setup_created")))
        conditions = dict(sig.get("conditions") or {})
        conditions.setdefault("group", group)
        conditions["decision_trace_id"] = sig.get("decision_trace_id")
        conditions["decision_trace"] = self._audit_json_safe(sig.get("_decision_trace") or [])
        conditions["final_status"] = sig.get("final_status") or ("ACTIONABLE" if gate_ok else "BLOCKED")
        conditions["block_reason"] = sig.get("block_reason") or ("" if gate_ok else str(gate or ""))
        conditions["blocked_at_step"] = sig.get("blocked_at_step") or ""
        conditions["pending_setup_created"] = bool(sig.get("pending_setup_created"))
        conditions["build_id"] = sig.get("build_id") or BUILD_ID
        conditions["ruleset_version"] = sig.get("ruleset_version") or RULESET_VERSION
        conditions["scanner_process_started_at"] = sig.get("scanner_process_started_at") or getattr(self, "scanner_process_started_at", "")
        conditions["generated_at"] = sig.get("generated_at") or datetime.utcnow().isoformat()
        conditions["source_loop_name"] = sig.get("source_loop_name") or "full_scan"
        conditions["engine"] = sig.get("engine")
        conditions["state"] = sig.get("state") or sig.get("final_status")
        conditions["direction_source"] = sig.get("direction_source")
        conditions["evaluate_probability_master_gate"] = bool(sig.get("evaluate_probability_master_gate", False))
        conditions["score_before"] = sig.get("score_before")
        conditions["score_after"] = sig.get("score_after")
        conditions["min_score"] = sig.get("min_score")
        conditions["pending_floor"] = sig.get("pending_floor")
        conditions["cost_to_target"] = sig.get("cost_to_target_pct")
        conditions["cost_to_target_stage"] = sig.get("cost_to_target_stage")
        conditions["cost_to_target_warning"] = bool(sig.get("cost_to_target_warning", False))
        conditions["low_vol_warning"] = bool(sig.get("low_vol_warning", False))
        conditions["low_vol_stage"] = sig.get("low_vol_stage")
        conditions["pre_pending_decision"] = sig.get("pre_pending_decision")
        conditions["pending_soft"] = bool(sig.get("pending_soft", False))
        conditions["require_stronger_1m_confirmation"] = bool(sig.get("require_stronger_1m_confirmation", False))
        conditions["block_reason"] = sig.get("block_reason") or ("" if gate_ok else str(gate or ""))
        conditions["pending_reason"] = sig.get("pending_reason") or (str(gate or "") if sig.get("pending_setup_created") else "")
        conditions["row_source"] = sig.get("row_source") or "current"
        conditions["historical"] = bool(sig.get("historical", False))
        conditions["score_function"] = sig.get("score_function")
        conditions["score_function_source"] = sig.get("score_function_source")
        conditions["adx_function"] = sig.get("adx_function")
        conditions["spread_function"] = sig.get("spread_function")
        conditions["engine_contract_verified"] = True
        sig["conditions"] = conditions
        if self.systematic is not None and sig.get("direction"):
            try:
                sig["systematic_reason"] = self.systematic.explainer.explain(sig, "ACTIONABLE" if gate_ok else ("NOT_QUALIFIED" if quality else "BLOCKED"), gate)
                conditions["systematic_reason"] = sig["systematic_reason"]
            except Exception:
                pass
        audit_fields = self._trade_audit_fields(sym, market, sig, details)
        _ss.upsert_signal(
            sym, market, str(sig.get("mode") or sig.get("strategy") or "scanning"), sig.get("direction"),
            price, atr, (atr / price) if price else 0.0, float(sig.get("rsi") or 0.0),
            float(sig.get("macd_h") or 0.0), float(sig.get("vsurge") or 0.0), float(sig.get("vwap") or 0.0),
            str(sig.get("regime") or "WAIT"), gate, gate_ok, conditions,
            vwap_z=float(sig.get("vwap_z") or 0.0),
            stoch_k=float(sig.get("stoch_k") or 0.0),
            stoch_d=float(sig.get("stoch_d") or 0.0),
            score=float(sig.get("score") or 0.0),
            strategy=str(sig.get("strategy") or ""),
            score_breakdown=audit_fields["score_breakdown"],
            bias_1h_score=audit_fields.get("bias_1h_score"),
            setup_15m_score=audit_fields.get("setup_15m_score"),
            trigger_5m_score=audit_fields.get("trigger_5m_score"),
            execution_1m_score=audit_fields.get("execution_1m_score"),
            smc_score=audit_fields.get("smc_score"),
            risk_reward_score=audit_fields.get("risk_reward_score"),
            session_score=audit_fields.get("session_score"),
            execution_1m_result=audit_fields.get("execution_1m_result"),
            smc_confirmation=audit_fields.get("smc_confirmation"),
            spread_pct=float(sig.get("spread_pct") or 0.0),
        )
        self._record_scan_gate(sym, market, gate, gate_ok, bool(sig.get("direction")), str(sig.get("final_status") or ""))
        self._audit_decision({
            "event": "signal_gate",
            "decision": "allow" if gate_ok else ("not_qualified" if quality else "block"),
            "symbol": self._canonical_from_resolved(sym),
            "market": market,
            "direction": sig.get("direction"),
            "price": price,
            "atr": atr,
            "gate": gate,
            "gate_ok": bool(gate_ok),
            "strategy": str(sig.get("strategy") or ""),
            "score": float(sig.get("score") or 0.0),
            "group": group,
            "execution_gate_id": sig.get("_execution_gate_id"),
            "explainability": sig.get("systematic_reason"),
            "trade_audit": audit_fields,
        })
        if self.systematic is not None and sig.get("direction"):
            try:
                row = self._systematic_csv_row(sym, market, sig, "ACTIONABLE" if gate_ok else ("NOT_QUALIFIED" if quality else "BLOCKED"), gate)
                if sig.get("systematic_reason"):
                    row["block_reason"] = sig.get("systematic_reason")
                self.systematic.analytics.record_signal(**row)
                if not gate_ok and not quality:
                    self.systematic.analytics.record_blocked_signal(**row)
            except Exception as exc:
                _ss.set_status("systematic_signal_csv_error", str(exc)[:180])
        if sig.get("direction"):
            scan_minute = datetime.now().strftime("%Y%m%d%H%M")
            try:
                _ss.record_setup_event(
                    f"mt5_scan_{sym}_{scan_minute}",
                    sym,
                    market,
                    str(sig.get("direction") or ""),
                    str(sig.get("strategy") or ""),
                    str(sig.get("mode") or sig.get("strategy") or "scanning"),
                    str(sig.get("timeframe_setup") or self._setup_timeframe()),
                    float(sig.get("score") or 0.0),
                    score_breakdown=audit_fields["score_breakdown"],
                    bias_1h_score=audit_fields.get("bias_1h_score"),
                    setup_15m_score=audit_fields.get("setup_15m_score"),
                    trigger_5m_score=audit_fields.get("trigger_5m_score"),
                    execution_1m_score=audit_fields.get("execution_1m_score"),
                    smc_score=audit_fields.get("smc_score"),
                    risk_reward_score=audit_fields.get("risk_reward_score"),
                    session_score=audit_fields.get("session_score"),
                    execution_1m_result=audit_fields.get("execution_1m_result"),
                    smc_confirmation=audit_fields.get("smc_confirmation"),
                    spread_signal=audit_fields.get("spread_signal"),
                    entry_price=price,
                    exit_reason=gate,
                    status="actionable" if gate_ok else ("not_qualified" if quality else "blocked"),
                )
            except Exception:
                pass

    def _begin_scan_stats(self, kind: str) -> None:
        self._active_scan_stats = {
            "kind": kind,
            "started_at": datetime.now().isoformat(),
            "rows": 0,
            "waiting": 0,
            "candidates": 0,
            "actionable": 0,
            "blocked": 0,
            "raw_scans": 0,
            "no_setup": 0,
            "pending_created": 0,
            "pending_active": 0,
            "confirmed": 0,
            "executed": 0,
            "cancelled": 0,
            "gates": Counter(),
            "markets": Counter(),
        }

    def _gate_bucket(self, gate: str, gate_ok: bool, has_direction: bool) -> str:
        if gate_ok:
            return "ok"
        if not has_direction:
            return "waiting"
        text = str(gate or "").lower()
        for needle, bucket in (
            ("spread", "spread"),
            ("quality", "quality_score"),
            ("z-score", "mean_rev_zscore"),
            ("adx", "mean_rev_adx"),
            ("stochastic", "mean_rev_stoch"),
            ("low vol", "low_vol_execution"),
            ("market closed", "market_window"),
            ("time window", "entry_time_window"),
            ("size block", "size"),
            ("daily", "daily_limit"),
            ("symbol", "symbol_risk"),
            ("order rejected", "order_rejected"),
        ):
            if needle in text:
                return bucket
        return "other_block"

    def _record_scan_gate(self, sym: str, market: str, gate: str, gate_ok: bool, has_direction: bool, final_status: str = "") -> None:
        stats = self._active_scan_stats
        if not stats:
            return
        stats["rows"] += 1
        stats["markets"][market or "unknown"] += 1
        status = str(final_status or "").upper()
        pending_state = status in {"PENDING", "PENDING_SOFT"}
        if has_direction:
            stats["candidates"] += 1
            if gate_ok and not pending_state:
                stats["actionable"] += 1
            elif not pending_state:
                stats["blocked"] += 1
        else:
            stats["waiting"] += 1
        stats["raw_scans"] += 1
        if status == "NO_SETUP" or not has_direction:
            stats["no_setup"] += 1
        if pending_state:
            stats["pending_active"] += 1
        if status in {"ACTIONABLE", "CONFIRMED"}:
            stats["confirmed"] += 1
        if status == "EXECUTED":
            stats["executed"] += 1
        if status == "CANCELLED":
            stats["cancelled"] += 1
        stats["gates"]["pending" if pending_state else self._gate_bucket(gate, gate_ok, has_direction)] += 1

    def _audit_json_safe(self, value):
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, dict):
            return {str(key): self._audit_json_safe(val) for key, val in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [self._audit_json_safe(item) for item in value]
        try:
            return float(value)
        except Exception:
            return str(value)

    def _spread_snapshot_now(self, sym: str, market: str, atr: float) -> dict:
        try:
            resolved, tick = self.gateway.symbol_tick(sym)
            bid = float(getattr(tick, "bid", 0.0) or 0.0)
            ask = float(getattr(tick, "ask", 0.0) or 0.0)
        except Exception as exc:
            return {"error": str(exc)[:160], "market": market}
        if bid <= 0 or ask <= 0 or ask < bid:
            return {"error": f"invalid tick bid={bid} ask={ask}", "market": market}
        mid = (bid + ask) / 2.0
        spread = ask - bid
        return {
            "resolved_symbol": resolved,
            "market": market,
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "spread": spread,
            "spread_pct": spread / mid if mid > 0 else 0.0,
            "spread_atr": spread / atr if atr > 0 else 0.0,
            "atr": atr,
        }

    def _signed_entry_slippage(self, direction: str, signal_price: float, fill_price: float) -> float:
        if signal_price <= 0 or fill_price <= 0:
            return 0.0
        if str(direction).upper() == "BUY":
            return fill_price - signal_price
        return signal_price - fill_price

    def _infer_exit_source_from_prices(self, meta: dict, close_price: float) -> str:
        explicit = str(meta.get("exit_source") or "").strip().upper()
        if explicit:
            return explicit
        try:
            sl = float(meta.get("sl") or 0.0)
            tp = float(meta.get("tp") or 0.0)
            atr = abs(float(meta.get("atr") or 0.0))
            entry = abs(float(meta.get("entry") or 0.0))
            close = float(close_price or 0.0)
        except Exception:
            return "BROKER"
        tol = max(entry * 0.00002, atr * 0.10, 0.00001)
        if sl > 0 and abs(close - sl) <= tol:
            return "SL"
        if tp > 0 and abs(close - tp) <= tol:
            return "TP"
        return "BROKER"

    def _trade_population(self, sig: dict | None = None, status: str = "") -> str:
        sig = sig or {}
        mode = str(sig.get("mode") or sig.get("sig_mode") or "").lower()
        strategy = str(sig.get("strategy") or "").lower()
        if "adopt" in mode or "manual" in mode or "import" in mode:
            return "adopted_imported"
        if "pyramid" in mode or "pyramid" in strategy or int(sig.get("pyramid_level") or 1) > 1:
            return "profit_pyramid"
        if "recovery" in mode or "management" in mode or "recovery" in strategy or "management" in strategy:
            return "recovery_management"
        return "direct_strategy"

    def _normalized_session_name(self, value: str | None) -> str:
        text = str(value or "").strip().lower().replace("-", "_")
        if "overlap" in text:
            return "overlap"
        if "asia" in text:
            return "Asia"
        if "london" in text:
            return "London"
        if text in {"ny", "new_york", "new york", "newyork"} or text.startswith("ny_"):
            return "New York"
        return "unknown"

    def _trade_audit_fields(
        self,
        sym: str,
        market: str,
        sig: dict,
        details: dict | None = None,
        *,
        fill_price: float | None = None,
        spread_execution: float | None = None,
        slippage: float | None = None,
    ) -> dict:
        details = dict(details or _score_details_from_signal(sig, market))
        execution_result = str(sig.get("execution_1m_result") or "mt5_not_checked")
        smc_confirmation = bool(sig.get("smc_confirmation") or False)
        spread_signal = float(sig.get("spread_signal") or 0.0)
        if spread_execution is None:
            spread_execution = spread_signal
        if slippage is None:
            signal_price = float(sig.get("price") or 0.0)
            fill = float(fill_price if fill_price is not None else signal_price)
            slippage = self._signed_entry_slippage(str(sig.get("direction") or ""), signal_price, fill)
        score_breakdown = dict(details.get("score_breakdown") or {})
        score_breakdown.update({
            "audit_version": "mt5_trade_audit_v1",
            "market": market,
            "session_name": self._normalized_session_name(sig.get("session") or details.get("session_name")),
            "timeframe_setup": details.get("timeframe_setup") or self._setup_timeframe(),
            "comparison_bucket": details.get("comparison_bucket") or "5m_direct",
            "execution_1m_result": execution_result,
            "smc_confirmation": smc_confirmation,
            "spread_signal": spread_signal,
            "spread_signal_pct": float(sig.get("spread_signal_pct") or sig.get("spread_pct") or 0.0),
            "spread_signal_atr": float(sig.get("spread_signal_atr") or 0.0),
            "spread_execution": float(spread_execution or 0.0),
            "slippage": float(slippage or 0.0),
            "thresholds": self._audit_json_safe(sig.get("_mt5_thresholds") or {}),
            "spread_snapshot_signal": self._audit_json_safe(sig.get("_mt5_spread_signal") or {}),
            "conditions": self._audit_json_safe(sig.get("conditions") or {}),
            "asset_class": sig.get("asset_class"),
            "engine": sig.get("engine"),
            "strategy_before": sig.get("strategy_before"),
            "strategy_after": sig.get("strategy_after"),
            "score_before": sig.get("score_before"),
            "score_after": sig.get("score_after"),
            "min_score": sig.get("min_score"),
            "pending_floor": sig.get("pending_floor"),
            "aggressive_target_profit_usd": sig.get("aggressive_target_profit_usd"),
            "target_profit_shortfall": sig.get("target_profit_shortfall"),
            "strict_mode_block_reason_shadow": sig.get("strict_mode_block_reason_shadow"),
            "why_trade_allowed_in_demo": sig.get("why_trade_allowed_in_demo"),
            "parent_trade_id": sig.get("parent_trade_id"),
            "pyramid_level": sig.get("pyramid_level"),
            "basket_unrealized_pnl": sig.get("basket_unrealized_pnl"),
            "basket_risk": sig.get("basket_risk"),
            "basket_target_profit": sig.get("basket_target_profit"),
            "basket_expected_profit": sig.get("basket_expected_profit"),
            "adx_reason": sig.get("adx_reason"),
            "engine_trace": self._audit_json_safe(sig.get("engine_trace") or {}),
            "index_soft_allow": bool(sig.get("index_soft_allow")),
            "soft_allow_reason": sig.get("soft_allow_reason"),
            "decision_trace_id": sig.get("decision_trace_id"),
            "decision_trace": self._audit_json_safe(sig.get("_decision_trace") or []),
            "score_function": sig.get("score_function"),
            "score_function_source": sig.get("score_function_source"),
            "adx_function": sig.get("adx_function"),
            "spread_function": sig.get("spread_function"),
            "risk_gate_function": "mt5_systematic_engine.PortfolioRisk.can_trade",
            "final_status": sig.get("final_status"),
            "block_reason": sig.get("block_reason"),
            "blocked_at_step": sig.get("blocked_at_step"),
            "pending_setup_created": bool(sig.get("pending_setup_created")),
            "one_min_confirmation": sig.get("one_min_confirmation"),
            "build_id": sig.get("build_id") or BUILD_ID,
            "ruleset_version": sig.get("ruleset_version") or RULESET_VERSION,
            "scanner_process_started_at": sig.get("scanner_process_started_at") or getattr(self, "scanner_process_started_at", ""),
            "generated_at": sig.get("generated_at"),
            "source_loop_name": sig.get("source_loop_name"),
            "state": sig.get("state") or sig.get("final_status"),
            "direction_source": sig.get("direction_source"),
            "evaluate_probability_master_gate": bool(sig.get("evaluate_probability_master_gate", False)),
            "pending_reason": sig.get("pending_reason"),
            "row_source": sig.get("row_source") or "current",
        })
        return {
            "strategy": str(sig.get("strategy") or ""),
            "score": float(sig.get("score") or 0.0),
            "score_breakdown": self._audit_json_safe(score_breakdown),
            "bias_1h_score": details.get("bias_1h_score"),
            "setup_15m_score": details.get("setup_15m_score"),
            "trigger_5m_score": details.get("trigger_5m_score"),
            "execution_1m_score": details.get("execution_1m_score", 0),
            "smc_score": details.get("smc_score", 0),
            "risk_reward_score": details.get("risk_reward_score"),
            "session_score": details.get("session_score"),
            "timeframe_setup": details.get("timeframe_setup") or "M15",
            "execution_1m_result": execution_result,
            "smc_confirmation": smc_confirmation,
            "spread_signal": spread_signal,
            "spread_execution": float(spread_execution or 0.0),
            "slippage": float(slippage or 0.0),
            "comparison_bucket": details.get("comparison_bucket"),
            "forward_phase": details.get("forward_phase"),
            "session": self._normalized_session_name(sig.get("session") or details.get("session_name")),
            "population": self._trade_population(sig),
            "setup_created_at": sig.get("setup_created_at") or sig.get("_pending_setup_created_at"),
            "m1_candle_closed_at": sig.get("m1_candle_closed_at"),
            "confirmation_detected_at": sig.get("confirmation_detected_at"),
            "order_send_at": sig.get("order_send_at"),
            "mt5_fill_at": sig.get("mt5_fill_at"),
            "confirmation_latency_ms": sig.get("confirmation_latency_ms", sig.get("confirmation_detection_latency_ms")),
            "send_latency_ms": sig.get("send_latency_ms", sig.get("order_send_latency_ms")),
            "total_latency_ms": sig.get("total_latency_ms", sig.get("total_setup_to_order_ms")),
        }

    def _finish_scan_stats(self, kind: str, **extra) -> str:
        stats = self._active_scan_stats or {}
        self._active_scan_stats = None
        gates_counter = stats.get("gates", Counter())
        markets_counter = stats.get("markets", Counter())
        gates = ",".join(f"{key}:{value}" for key, value in gates_counter.most_common(8))
        markets = ",".join(f"{key}:{value}" for key, value in markets_counter.most_common())
        try:
            stats["pending_active"] = max(int(stats.get("pending_active", 0) or 0), len(self.pending_setups))
        except Exception:
            pass
        tradeable_today = (
            int(stats.get("pending_active", 0) or 0)
            + int(stats.get("confirmed", 0) or 0)
            + int(stats.get("executed", 0) or 0)
        )
        parts = [
            f"ruleset_version={RULESET_VERSION}",
            f"kind={kind}",
            f"rows={int(stats.get('rows', 0) or 0)}",
            f"candidates={int(stats.get('candidates', 0) or 0)}",
            f"actionable={int(stats.get('actionable', 0) or 0)}",
            f"blocked={int(stats.get('blocked', 0) or 0)}",
            f"waiting={int(stats.get('waiting', 0) or 0)}",
            f"raw_scans={int(stats.get('raw_scans', 0) or 0)}",
            f"no_setup={int(stats.get('no_setup', 0) or 0)}",
            f"pending_created={int(stats.get('pending_created', 0) or 0)}",
            f"pending_active={int(stats.get('pending_active', 0) or 0)}",
            f"confirmed={int(stats.get('confirmed', 0) or 0)}",
            f"executed={int(stats.get('executed', 0) or 0)}",
            f"cancelled={int(stats.get('cancelled', 0) or 0)}",
            f"tradeable_today={tradeable_today}",
        ]
        _ss.set_status("ruleset_version", RULESET_VERSION)
        _ss.set_status("tradeable_today", tradeable_today)
        for status_key in ("raw_scans", "no_setup", "candidates", "blocked", "pending_created", "pending_active", "confirmed", "executed", "cancelled"):
            _ss.set_status(f"scan_counter_{status_key}", int(stats.get(status_key, 0) or 0))
        for key, value in extra.items():
            parts.append(f"{key}={value}")
        if gates:
            parts.append(f"gates={gates}")
        if markets:
            parts.append(f"markets={markets}")
        summary = " ".join(parts)
        _ss.set_status(f"last_{kind}_scan_summary", summary)
        print(f"[MT5_SCAN] {summary}", flush=True)
        return summary

    def _mt5_volume_for_trade(self, sym: str, market: str, price: float, stop_d: float,
                              strategy_equity: float, sig: dict) -> tuple[float, str, dict]:
        spec = self.gateway.symbol_info(sym)
        canonical = self._canonical_from_resolved(sym)
        risk_pct = self._risk_pct_for_symbol(canonical, market)
        risk_budget = strategy_equity * (risk_pct / 100.0)
        score = float(sig.get("score") or 0.0)
        if bool((sig or {}).get("replacement_strategy_v1")):
            # V1 score is a diagnostic edge metric, not a calibrated probability.
            prob_mult = 1.0
            sig.setdefault("_mt5_thresholds", {}).setdefault("risk", {})["score_sizing"] = "disabled_for_strategy_v1"
        elif score >= 85:
            prob_mult = 1.0
        elif score >= 75:
            prob_mult = 0.75
        else:
            prob_mult = 0.5
        risk_budget *= prob_mult
        two_engine_risk_usd = self._two_engine_test_risk_usd(canonical)
        if two_engine_risk_usd is not None and two_engine_risk_usd > 0:
            risk_budget = min(risk_budget, float(two_engine_risk_usd))
            sig.setdefault("_mt5_thresholds", {}).setdefault("risk", {})["two_engine_risk_usd"] = round(float(two_engine_risk_usd), 2)
        direction = str(sig.get("direction") or "").upper()
        configured_rr = (get_symbol_config(canonical) or {}).get("tp_r")
        rr = self._entry_rr_for_symbol(canonical, market, sig, configured_rr=configured_rr)
        stop_price = price - stop_d if direction == "BUY" else price + stop_d
        target_price = price + stop_d * rr if direction == "BUY" else price - stop_d * rr
        try:
            per_lot_geometry = self.gateway.order_calc_trade_geometry(
                spec.symbol, direction, 1.0, price, stop_price, target_price
            )
            loss_per_lot = float(per_lot_geometry.get("sl_loss_usd") or 0.0)
            profit_per_lot = float(per_lot_geometry.get("tp_profit_usd") or 0.0)
            margin_per_lot = float(per_lot_geometry.get("margin_usd") or 0.0)
            acct = self.gateway.account_info()
            free_margin = float(getattr(acct, "free_margin", 0.0) or 0.0)
        except Exception as exc:
            return 0.0, f"SIZE BLOCK: broker geometry unavailable: {str(exc)[:140]}", {"spec": spec.__dict__}
        if loss_per_lot <= 0 or profit_per_lot <= 0 or margin_per_lot <= 0:
            return 0.0, f"SIZE BLOCK: invalid broker geometry {per_lot_geometry}", {"spec": spec.__dict__}
        sig["_broker_geometry_per_lot"] = {
            **per_lot_geometry, "entry": price, "sl": stop_price, "tp": target_price, "rr": rr,
        }
        volume_by_risk = risk_budget / loss_per_lot
        margin_fraction = _env_float("MT5_MAX_MARGIN_FRACTION", 0.80, lo=0.10, hi=0.95)
        volume_by_margin = (free_margin * margin_fraction / margin_per_lot) if free_margin > 0 and margin_per_lot > 0 else 0.0
        volume_by_cap = volume_by_margin
        raw_volume = min(volume_by_risk, volume_by_margin, float(spec.volume_max or volume_by_risk))
        max_lot = self._max_lot_for_symbol(sym, market)
        if max_lot > 0:
            raw_volume = min(raw_volume, max_lot)
        volume = self.gateway.normalize_volume(spec, raw_volume)
        if volume <= 0:
            minimum_volume = self.gateway.normalize_volume(spec, float(spec.volume_min or 0.0))
            minimum_risk = loss_per_lot * minimum_volume if minimum_volume > 0 else float("inf")
            max_trade_risk = self._max_trade_risk_limit_for_symbol(canonical, market)
            if minimum_volume > 0 and max_trade_risk > 0 and minimum_risk <= max_trade_risk:
                volume = minimum_volume
                sig.setdefault("_mt5_thresholds", {}).setdefault("risk", {}).update({
                    "minimum_lot_fallback": True,
                    "minimum_lot": minimum_volume,
                    "minimum_lot_risk_usd": round(minimum_risk, 4),
                    "max_trade_risk_usd": round(max_trade_risk, 4),
                })
            else:
                return (
                    0.0,
                    f"SIZE BLOCK: computed {raw_volume:.8f} lots below broker minimum {spec.volume_min}; minimum risk USD {minimum_risk:.2f} exceeds cap USD {max_trade_risk:.2f}",
                    {
                        "risk_budget": risk_budget,
                        "risk_pct": risk_pct,
                        "loss_per_lot": loss_per_lot,
                        "volume_by_risk": volume_by_risk,
                        "margin_fraction": margin_fraction,
                        "volume_by_cap": volume_by_cap,
                        "minimum_lot_risk": minimum_risk,
                        "max_trade_risk": max_trade_risk,
                        "spec": spec.__dict__,
                    },
                )
        aggressive_details = {}
        if self._aggressive_scalp_effective():
            sl = stop_price
            tp = target_price
            volume, aggressive_details = self.aggressive_sizing_engine.plan(canonical, market, sig, spec, price, sl, tp, strategy_equity, volume, {
                "risk_budget": risk_budget,
                "risk_pct": risk_pct,
                "loss_per_lot": loss_per_lot,
                "profit_per_lot": profit_per_lot,
                "margin_per_lot": margin_per_lot,
                "volume_by_risk": volume_by_risk,
                "margin_fraction": margin_fraction,
                "volume_by_cap": volume_by_cap,
                "max_lot": max_lot,
                "spec": spec.__dict__,
            })
            if volume <= 0:
                return 0.0, "SIZE BLOCK: aggressive sizing could not find a safe broker-valid lot", {"aggressive_sizing": aggressive_details, "spec": spec.__dict__}
        if bool((sig or {}).get("replacement_strategy_v1")):
            asset = str(sig.get("asset_class") or "").lower()
            max_trade_risk = {
                "forex": _env_float("MT5_MAX_TRADE_RISK_FOREX_USD", 100.0, lo=0.0),
                "index": _env_float("MT5_MAX_TRADE_RISK_INDICES_USD", 100.0, lo=0.0),
                "metal": _env_float("MT5_MAX_TRADE_RISK_METALS_USD", 100.0, lo=0.0),
            }.get(asset, 100.0)
        else:
            max_trade_risk = self._max_trade_risk_limit_for_symbol(canonical, market)
        planned_risk = loss_per_lot * volume
        if max_trade_risk > 0 and planned_risk > max_trade_risk:
            return (
                0.0,
                f"SIZE BLOCK: planned risk ${planned_risk:.2f} exceeds max ${max_trade_risk:.2f}",
                {
                    "risk_budget": risk_budget,
                    "risk_pct": risk_pct,
                    "loss_per_lot": loss_per_lot,
                    "volume_by_risk": volume_by_risk,
                    "margin_fraction": margin_fraction,
                    "volume_by_cap": volume_by_cap,
                    "max_lot": max_lot,
                    "planned_risk": planned_risk,
                    "max_trade_risk": max_trade_risk,
                    "aggressive_sizing": aggressive_details,
                    "target_profit_shortfall": sig.get("target_profit_shortfall"),
                    "spec": spec.__dict__,
                },
            )
        return (
            volume,
            "OK",
            {
                "risk_budget": risk_budget,
                "risk_pct": risk_pct,
                "loss_per_lot": loss_per_lot,
                "volume_by_risk": volume_by_risk,
                "profit_per_lot": profit_per_lot,
                "margin_fraction": margin_fraction,
                "volume_by_cap": volume_by_cap,
                "max_lot": max_lot,
                "planned_risk": planned_risk,
                "max_trade_risk": max_trade_risk,
                "aggressive_sizing": aggressive_details,
                "target_profit_shortfall": sig.get("target_profit_shortfall"),
                "margin_per_lot": margin_per_lot,
                "usable_margin": free_margin * margin_fraction,
                "spec": spec.__dict__,
            },
        )

    def _loss_per_lot(self, spec: MT5SymbolSpec, price: float, stop_d: float, direction: str = "BUY") -> float:
        side = str(direction or "").upper()
        if side not in {"BUY", "SELL"} or price <= 0 or stop_d <= 0:
            return 0.0
        stop_price = price - stop_d if side == "BUY" else price + stop_d
        try:
            projected = self.gateway.order_calc_profit(spec.symbol, side, 1.0, price, stop_price)
        except Exception as exc:
            self._audit_decision({
                "event": "BROKER_CALC_UNAVAILABLE",
                "decision": "block",
                "symbol": spec.symbol,
                "calculation": "OrderCalcProfit_SL",
                "reason": str(exc)[:180],
            })
            return 0.0
        return abs(float(projected))

    def _trade_result_r(self, realized: float, entry: float, sl: float, qty: float, market: str, risk_meta: dict | None = None) -> float | None:
        planned_risk = float((risk_meta or {}).get("planned_risk") or 0.0) if isinstance(risk_meta, dict) else 0.0
        if planned_risk <= 0 and entry > 0 and sl > 0 and qty > 0:
            try:
                spec = self.gateway.symbol_info(self._canonical_from_resolved(str((risk_meta or {}).get("symbol") or ""))) if isinstance(risk_meta, dict) and risk_meta.get("symbol") else None
                if spec is not None:
                    planned_risk = self._loss_per_lot(spec, entry, abs(entry - sl), str((risk_meta or {}).get("direction") or "BUY")) * qty
            except Exception:
                planned_risk = 0.0
        if planned_risk <= 0:
            planned_risk = abs(entry - sl) * max(qty, 1.0)
        return round(float(realized) / planned_risk, 6) if planned_risk > 0 else None

    def _estimated_pnl_from_meta(self, meta: dict, close_price: float) -> float:
        entry = float(meta.get("entry") or 0.0)
        qty = float(meta.get("qty") or 0.0)
        direction = str(meta.get("direction") or "BUY")
        risk_meta = meta.get("risk_meta") or {}
        spec = (risk_meta.get("spec") or {}) if isinstance(risk_meta, dict) else {}
        contract_size = float(spec.get("contract_size") or 0.0)
        if contract_size > 0:
            gross = ((close_price - entry) if direction == "BUY" else (entry - close_price)) * qty * contract_size
            if str(spec.get("symbol", "")).upper().endswith("JPY") and close_price > 0:
                gross = gross / close_price
            return gross
        return _gross_pnl_usd(entry, close_price, qty, direction, str(meta.get("market") or "forex"))

    def _record_rejected_trade(self, sym: str, market: str, sig: dict, qty: float,
                               price: float, sl: float, tp: float, atr: float, reason: str) -> None:
        trade_id = f"mt5_rejected_{sym}_{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
        details = _score_details_from_signal(sig, market)
        audit_fields = self._trade_audit_fields(sym, market, sig, details, fill_price=price)
        _ss.insert_trade(
            trade_id, sym, market, str(sig.get("direction") or ""), qty, price, sl, tp, atr,
            str(sig.get("mode") or "mt5"), datetime.now().isoformat(),
            status="rejected",
            rejected_reason=reason[:500],
            **audit_fields,
        )
        self._audit_decision({
            "event": "order_rejected_recorded",
            "decision": "rejected",
            "trade_id": trade_id,
            "symbol": self._canonical_from_resolved(sym),
            "market": market,
            "direction": sig.get("direction"),
            "qty": qty,
            "price": price,
            "sl": sl,
            "tp": tp,
            "rejected_reason": reason[:500],
            "trade_audit": audit_fields,
        })

    def _place_trade(self, sym: str, market: str, sig: dict, qty: float, sl: float, tp: float, risk_meta: dict | None = None):
        trade_id = f"mt5_{sym}_{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
        price = float(sig["price"])
        atr = float(sig["atr"])
        risk_meta = dict(risk_meta or {})
        gate_id = str(sig.get("_execution_gate_id") or "")
        if gate_id:
            risk_meta["execution_gate_id"] = gate_id
            risk_meta["execution_gate_approved_at"] = sig.get("_execution_gate_approved_at")
        elif _env_bool("MT5_KILL_ON_UNEXPECTED_TRADE_PATH", True):
            reason = f"unexpected MT5 trade path blocked for {sym}: missing execution gate approval"
            _ss.set_status("halt", reason)
            self._audit_decision({
                "event": "unexpected_trade_path",
                "decision": "block",
                "symbol": self._canonical_from_resolved(sym),
                "market": market,
                "direction": sig.get("direction"),
                "qty": qty,
                "price": price,
                "sl": sl,
                "tp": tp,
                "strategy": str(sig.get("strategy") or ""),
                "score": float(sig.get("score") or 0.0),
                "reason": reason,
            })
            raise RuntimeError(reason)
        direction = str(sig.get("direction") or "").upper()
        pending = sig.get("pending_setup") if isinstance(sig.get("pending_setup"), dict) else {}
        raw_signal_side = str(sig.get("raw_signal_side") or sig.get("raw_direction") or direction).upper()
        setup_side = str(sig.get("setup_side") or pending.get("setup_side") or direction).upper()
        pending_setup_side = str(sig.get("pending_setup_side") or pending.get("side") or pending.get("direction") or setup_side).upper()
        confirmation_side = str(sig.get("confirmation_side") or (direction if sig.get("one_min_confirmation", True) else "")).upper()
        final_order_side = str(sig.get("final_order_side") or direction).upper()
        mt5_order_type = "ORDER_TYPE_BUY" if final_order_side == "BUY" else "ORDER_TYPE_SELL" if final_order_side == "SELL" else ""
        side_fields = {
            "raw_signal_side": raw_signal_side, "setup_side": setup_side,
            "pending_setup_side": pending_setup_side, "confirmation_side": confirmation_side,
            "final_order_side": final_order_side, "mt5_order_type": mt5_order_type,
        }
        side_mismatch = (
            final_order_side not in {"BUY", "SELL"}
            or direction != final_order_side
            or mt5_order_type != ("ORDER_TYPE_BUY" if direction == "BUY" else "ORDER_TYPE_SELL")
            or setup_side != confirmation_side
            or confirmation_side != final_order_side
            or pending_setup_side != confirmation_side
        )
        self._audit_decision({"event": "side_consistency_assertion", "decision": "block" if side_mismatch else "pass", "symbol": self._canonical_from_resolved(sym), "scope": "order", **side_fields})
        if side_mismatch:
            reason = f"side consistency assertion failed: {side_fields}"
            _ss.set_status("halt", reason)
            raise RuntimeError(reason)
        sig.update(side_fields)
        pyramid_context = self._pyramid_context_for_trade(sym, direction)
        pyramid_context["parent_trade_id"] = pyramid_context.get("parent_trade_id") or trade_id
        if isinstance(risk_meta.get("aggressive_sizing"), dict) and risk_meta.get("aggressive_sizing", {}).get("target_profit_usd"):
            pyramid_context["basket_target_profit"] = float(risk_meta.get("aggressive_sizing", {}).get("target_profit_usd") or 0.0)
        risk_meta.update({
            "parent_trade_id": pyramid_context.get("parent_trade_id"),
            "pyramid_level": pyramid_context.get("pyramid_level"),
            "basket_unrealized_pnl": pyramid_context.get("basket_unrealized_pnl"),
            "basket_risk": pyramid_context.get("basket_risk"),
            "basket_target_profit": pyramid_context.get("basket_target_profit"),
            "basket_expected_profit": pyramid_context.get("basket_expected_profit"),
        })
        sig.update({
            "parent_trade_id": pyramid_context.get("parent_trade_id"),
            "pyramid_level": pyramid_context.get("pyramid_level"),
            "basket_unrealized_pnl": pyramid_context.get("basket_unrealized_pnl"),
            "basket_risk": pyramid_context.get("basket_risk"),
            "basket_target_profit": pyramid_context.get("basket_target_profit"),
            "basket_expected_profit": pyramid_context.get("basket_expected_profit"),
        })
        if self.runtime.dry_run:
            now_iso = datetime.utcnow().isoformat()
            sig.setdefault("order_send_at", now_iso)
            sig.setdefault("mt5_fill_at", now_iso)
            confirmation_dt = _parse_dt(sig.get("confirmation_detected_at"))
            setup_dt = _parse_dt(sig.get("setup_created_at") or sig.get("_pending_setup_created_at"))
            if confirmation_dt:
                sig["send_latency_ms"] = round(max(0.0, (datetime.utcnow() - confirmation_dt).total_seconds() * 1000.0), 3)
            if setup_dt:
                sig["total_latency_ms"] = round(max(0.0, (datetime.utcnow() - setup_dt).total_seconds() * 1000.0), 3)
            sig["order_send_latency_ms"] = sig.get("send_latency_ms")
            sig["total_setup_to_order_ms"] = sig.get("total_latency_ms")
            ticket = f"dry_{trade_id}"
            details = _score_details_from_signal(sig, market)
            audit_fields = self._trade_audit_fields(sym, market, sig, details, fill_price=price)
            self.open_trades[ticket] = {
                "trade_id": trade_id,
                "sym": sym,
                "market": market,
                "direction": sig["direction"],
                "qty": qty,
                "entry": price,
                "sl": sl,
                "tp": tp,
                "atr": atr,
                "sig_mode": sig.get("mode"),
                "opened_at": datetime.now().isoformat(),
                "trade_date": datetime.now().date().isoformat(),
                "estimated_pnl": 0.0,
                "risk_meta": risk_meta,
                "trade_audit": audit_fields,
                "parent_trade_id": pyramid_context.get("parent_trade_id"),
                "pyramid_level": pyramid_context.get("pyramid_level"),
                "initial_lot": qty if int(pyramid_context.get("pyramid_level") or 1) == 1 else float(pyramid_context.get("initial_lot") or qty),
                "basket_unrealized_pnl": pyramid_context.get("basket_unrealized_pnl"),
                "basket_risk": pyramid_context.get("basket_risk"),
                "basket_target_profit": pyramid_context.get("basket_target_profit"),
                "basket_expected_profit": pyramid_context.get("basket_expected_profit"),
            }
            _ss.insert_trade(
                trade_id, sym, market, sig["direction"], qty, price, sl, tp, atr,
                str(sig.get("mode") or "mt5"), datetime.now().isoformat(),
                status="dry_run",
                **audit_fields,
            )
            if self.systematic is not None:
                try:
                    self.systematic.analytics.record_execution(**self._systematic_csv_row(sym, market, sig, "DRY_RUN", "dry run order recorded", cost=float((sig.get("_mt5_thresholds") or {}).get("systematic_cost", {}).get("cost", {}).get("total_cost_usd") or 0.0), slippage=0.0))
                except Exception as exc:
                    _ss.set_status("systematic_execution_csv_error", str(exc)[:180])
            _ss.set_status("daily_trade_count", self._daily_trade_count())
            self._audit_decision({
                "event": "order_recorded",
                "decision": "dry_run",
                "gate_id": gate_id,
                "trade_id": trade_id,
                "symbol": self._canonical_from_resolved(sym),
                "direction": sig.get("direction"),
                "qty": qty,
                "price": price,
                "sl": sl,
                "tp": tp,
                "trade_audit": audit_fields,
            })
            self._save_state()
            return {"retcode": 10009, "retcode_text": "DRY_RUN", "order": ticket, "deal": 0, "price": price, "accepted": True}
        duplicate_ok, duplicate_reason = self._duplicate_order_allows_entry(sym, str(sig["direction"]))
        if not duplicate_ok:
            raise RuntimeError(f"pre-send duplicate prevention blocked {sym}: {duplicate_reason}")
        before_tickets = {
            str(pos.ticket)
            for pos in self.gateway.positions()
            if self._canonical_from_resolved(pos.symbol) == self._canonical_from_resolved(sym)
        }
        order_send_dt = datetime.utcnow()
        sig["order_send_at"] = order_send_dt.isoformat()
        confirmation_dt = _parse_dt(sig.get("confirmation_detected_at"))
        setup_dt = _parse_dt(sig.get("setup_created_at") or sig.get("_pending_setup_created_at"))
        sig["order_send_latency_ms"] = round(max(0.0, (order_send_dt - confirmation_dt).total_seconds() * 1000.0), 3) if confirmation_dt else None
        sig["send_latency_ms"] = sig.get("order_send_latency_ms")
        sig["total_setup_to_order_ms"] = round(max(0.0, (order_send_dt - setup_dt).total_seconds() * 1000.0), 3) if setup_dt else None
        sig["total_latency_ms"] = sig.get("total_setup_to_order_ms")
        resolved, fill_price, result = self.gateway.place_market_order(sym, sig["direction"], qty, sl, tp, f"cipherfx-{sym}")
        mt5_fill_dt = datetime.utcnow()
        sig["mt5_fill_at"] = mt5_fill_dt.isoformat()
        self._audit_decision({
            "event": "order_timing", "decision": "sent", "symbol": self._canonical_from_resolved(sym),
            "side": str(sig.get("direction") or "").upper(), "setup_id": sig.get("setup_id"),
            "setup_created_at": sig.get("setup_created_at") or sig.get("_pending_setup_created_at"),
            "m1_candle_closed_at": sig.get("m1_candle_closed_at"),
            "confirmation_detected_at": sig.get("confirmation_detected_at"),
            "order_send_at": sig.get("order_send_at"), "mt5_fill_at": sig.get("mt5_fill_at"),
            "confirmation_detection_latency_ms": sig.get("confirmation_detection_latency_ms"),
            "confirmation_latency_ms": sig.get("confirmation_latency_ms", sig.get("confirmation_detection_latency_ms")),
            "order_send_latency_ms": sig.get("order_send_latency_ms"),
            "send_latency_ms": sig.get("send_latency_ms", sig.get("order_send_latency_ms")),
            "total_setup_to_order_ms": sig.get("total_setup_to_order_ms"),
            "total_latency_ms": sig.get("total_latency_ms", sig.get("total_setup_to_order_ms")),
        })
        retcode = int(getattr(result, "retcode", 0) or 0)
        if retcode not in TRADE_RETCODE_DONE:
            sig["retcode"] = retcode
            raise RuntimeError(f"MT5 order rejected for {resolved}: retcode={retcode}")
        preferred_tickets = {
            str(int(value))
            for value in (getattr(result, "order", 0), getattr(result, "deal", 0))
            if int(value or 0) > 0
        }
        live_position = self._find_live_position(sym, exclude_tickets=before_tickets, preferred_tickets=preferred_tickets)
        if live_position is not None:
            ticket = str(live_position.ticket)
            fill_price = float(live_position.price_open or fill_price)
            qty = float(live_position.volume or qty)
            sl = float(live_position.sl or sl)
            tp = float(live_position.tp or tp)
        else:
            ticket = str(int(getattr(result, "order", 0) or getattr(result, "deal", 0) or 0))
        if not ticket or ticket == "0":
            ticket = f"pending_{trade_id}"
        execution_spread_snapshot = self._spread_snapshot_now(sym, market, atr)
        spread_execution = float(execution_spread_snapshot.get("spread") or sig.get("spread_signal") or 0.0)
        slippage = self._signed_entry_slippage(str(sig["direction"]), price, float(fill_price))
        sig["_mt5_spread_execution"] = execution_spread_snapshot
        sig["spread_execution"] = spread_execution
        sig["slippage"] = slippage
        sig["retcode"] = retcode
        sig["fill_price"] = float(fill_price)
        details = _score_details_from_signal(sig, market)
        audit_fields = self._trade_audit_fields(
            sym,
            market,
            sig,
            details,
            fill_price=float(fill_price),
            spread_execution=spread_execution,
            slippage=slippage,
        )
        risk_meta["trade_audit"] = audit_fields
        risk_meta["spread_execution_snapshot"] = execution_spread_snapshot
        self.open_trades[ticket] = {
            "trade_id": trade_id,
            "sym": sym,
            "market": market,
            "direction": sig["direction"],
            "qty": qty,
            "entry": fill_price,
            "sl": sl,
            "tp": tp,
            "atr": atr,
            "sig_mode": sig.get("mode"),
            "opened_at": datetime.now().isoformat(),
            "trade_date": datetime.now().date().isoformat(),
            "resolved_symbol": resolved,
            "risk_meta": risk_meta,
            "trade_audit": audit_fields,
            "parent_trade_id": pyramid_context.get("parent_trade_id"),
            "pyramid_level": pyramid_context.get("pyramid_level"),
            "initial_lot": qty if int(pyramid_context.get("pyramid_level") or 1) == 1 else float(pyramid_context.get("initial_lot") or qty),
            "basket_unrealized_pnl": pyramid_context.get("basket_unrealized_pnl"),
            "basket_risk": pyramid_context.get("basket_risk"),
            "basket_target_profit": pyramid_context.get("basket_target_profit"),
            "basket_expected_profit": pyramid_context.get("basket_expected_profit"),
        }
        _ss.insert_trade(
            trade_id, sym, market, sig["direction"], qty, fill_price, sl, tp, atr,
            str(sig.get("mode") or "mt5"), datetime.now().isoformat(),
            status="live",
            **audit_fields,
        )
        if self.systematic is not None:
            try:
                self.systematic.analytics.record_execution(**self._systematic_csv_row(sym, market, sig, "LIVE", "order confirmed", cost=float((sig.get("_mt5_thresholds") or {}).get("systematic_cost", {}).get("cost", {}).get("total_cost_usd") or 0.0), slippage=slippage))
            except Exception as exc:
                _ss.set_status("systematic_execution_csv_error", str(exc)[:180])
        _ss.set_status("daily_trade_count", self._daily_trade_count())
        self._audit_decision({
            "event": "order_confirmed",
            "decision": "live",
            "gate_id": gate_id,
            "trade_id": trade_id,
            "symbol": self._canonical_from_resolved(sym),
            "resolved_symbol": resolved,
            "direction": sig.get("direction"),
            "qty": qty,
            "entry": fill_price,
            "sl": sl,
            "tp": tp,
            "retcode": retcode,
            "ticket": ticket,
            "spread_execution_snapshot": execution_spread_snapshot,
            "slippage": slippage,
            "parent_trade_id": pyramid_context.get("parent_trade_id"),
            "pyramid_level": pyramid_context.get("pyramid_level"),
            "basket_unrealized_pnl": pyramid_context.get("basket_unrealized_pnl"),
            "basket_risk": pyramid_context.get("basket_risk"),
            "basket_target_profit": pyramid_context.get("basket_target_profit"),
            "trade_audit": audit_fields,
        })
        self._save_state()
        return result

    def _find_live_position(
        self,
        sym: str,
        exclude_tickets: set[str] | None = None,
        preferred_tickets: set[str] | None = None,
    ) -> MT5PositionView | None:
        canonical = self._canonical_from_resolved(sym)
        exclude_tickets = exclude_tickets or set()
        preferred_tickets = preferred_tickets or set()
        for _ in range(5):
            candidates = [
                pos
                for pos in self.gateway.positions()
                if self._canonical_from_resolved(pos.symbol) == canonical
            ]
            for pos in candidates:
                if str(pos.ticket) in preferred_tickets:
                    return pos
            new_candidates = [pos for pos in candidates if str(pos.ticket) not in exclude_tickets]
            if new_candidates:
                return max(new_candidates, key=lambda pos: pos.time)
            if candidates:
                return max(candidates, key=lambda pos: pos.time)
            time.sleep(1)
        return None

    def _canonical_from_resolved(self, resolved_symbol: str) -> str:
        return self.runtime.canonical_symbol(resolved_symbol)


def cli():
    parser = argparse.ArgumentParser(description="Cipher FX MT5/XM bot")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    args = cli()
    runtime = MT5RuntimeConfig(dry_run=args.dry_run or MT5RuntimeConfig().dry_run)
    XM_MT5_Bot(runtime).run()
