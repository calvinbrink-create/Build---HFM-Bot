"""Deterministic market-regime context for setup construction.

This module describes the current market; it never creates a signal, scores a
trade, or vetoes execution. Engines can use the returned context to explain
whether a setup is occurring in trend, range, pullback, transition, or a
volatility state.
"""
from __future__ import annotations

from typing import Any


def _candles(frame) -> list[Any]:
    return list(getattr(frame, "candles", ())) if frame is not None else []


def _true_range(candles: list[Any], index: int) -> float:
    bar = candles[index]
    previous = candles[index - 1].close if index else bar.open
    return max(
        float(bar.high) - float(bar.low),
        abs(float(bar.high) - float(previous)),
        abs(float(bar.low) - float(previous)),
    )


def _frame_state(frame, *, lookback: int = 20) -> dict[str, Any]:
    candles = _candles(frame)
    if len(candles) < lookback + 1:
        return {
            "available": bool(candles),
            "state": "INSUFFICIENT",
            "direction": "NONE",
            "volatility": "UNKNOWN",
            "slope_atr": 0.0,
            "efficiency": 0.0,
            "bar_time": candles[-1].timestamp.isoformat() if candles else "",
        }

    lookback = min(lookback, len(candles) - 1)
    recent_tr = [_true_range(candles, i) for i in range(max(1, len(candles) - 14), len(candles))]
    long_start = max(1, len(candles) - 50)
    long_tr = [_true_range(candles, i) for i in range(long_start, len(candles))]
    recent_atr = sum(recent_tr) / len(recent_tr) if recent_tr else 0.0
    long_atr = sum(long_tr) / len(long_tr) if long_tr else recent_atr
    start = len(candles) - lookback - 1
    move = float(candles[-1].close) - float(candles[start].close)
    slope_atr = move / recent_atr if recent_atr > 0 else 0.0
    path = sum(abs(float(candles[i].close) - float(candles[i - 1].close)) for i in range(start + 1, len(candles)))
    efficiency = abs(move) / path if path > 0 else 0.0

    if slope_atr >= 1.2 and efficiency >= 0.35:
        state, direction = "TREND", "BUY"
    elif slope_atr <= -1.2 and efficiency >= 0.35:
        state, direction = "TREND", "SELL"
    elif abs(slope_atr) <= 0.8 or efficiency < 0.25:
        state, direction = "RANGE", "NONE"
    else:
        state, direction = "TRANSITION", "NONE"

    volatility_ratio = recent_atr / long_atr if long_atr > 0 else 1.0
    if volatility_ratio >= 1.30:
        volatility = "HIGH"
    elif volatility_ratio <= 0.70:
        volatility = "LOW"
    else:
        volatility = "NORMAL"

    return {
        "available": True,
        "state": state,
        "direction": direction,
        "volatility": volatility,
        "volatility_ratio": round(volatility_ratio, 4),
        "slope_atr": round(slope_atr, 4),
        "efficiency": round(efficiency, 4),
        "bar_time": candles[-1].timestamp.isoformat(),
    }


def classify_market(frames: dict[str, Any]) -> dict[str, Any]:
    """Return descriptive regime context without changing trade eligibility."""
    states = {}
    for name, lookback in (("D1", 20), ("H4", 20), ("H1", 24), ("M15", 32), ("M5", 48)):
        states[name] = _frame_state(frames.get(name), lookback=lookback)

    primary_name = next(
        (name for name in ("D1", "H4", "H1", "M15", "M5") if states[name]["state"] != "INSUFFICIENT"),
        "M5",
    )
    primary = states[primary_name]
    lower_name = next(
        (name for name in ("M15", "M5") if states[name]["state"] != "INSUFFICIENT"),
        primary_name,
    )
    lower = states[lower_name]

    if primary["state"] == "TREND" and lower["state"] == "TREND":
        if primary["direction"] == lower["direction"] == "BUY":
            label = "TREND_UP"
        elif primary["direction"] == lower["direction"] == "SELL":
            label = "TREND_DOWN"
        elif primary["direction"] == "BUY":
            label = "PULLBACK_IN_UPTREND"
        elif primary["direction"] == "SELL":
            label = "PULLBACK_IN_DOWNTREND"
        else:
            label = "MIXED_TRANSITION"
    elif primary["state"] == "TREND" and lower["state"] == "RANGE":
        label = "PULLBACK_IN_UPTREND" if primary["direction"] == "BUY" else "PULLBACK_IN_DOWNTREND" if primary["direction"] == "SELL" else "RANGE"
    elif primary["state"] == "RANGE" and lower["state"] == "RANGE":
        label = "RANGE"
    elif primary["state"] == "TRANSITION" or lower["state"] == "TRANSITION":
        label = "BREAKOUT_TRANSITION"
    else:
        label = "MIXED_TRANSITION"

    available = [value for value in states.values() if value["state"] != "INSUFFICIENT"]
    volatility = "HIGH" if any(value["volatility"] == "HIGH" for value in available) else "LOW" if available and all(value["volatility"] == "LOW" for value in available) else "NORMAL"
    return {
        "role": "MARKET_CONTEXT_OBSERVATION_ONLY",
        "label": label,
        "direction": primary["direction"] if primary["direction"] in {"BUY", "SELL"} else lower["direction"],
        "volatility": volatility,
        "primary_timeframe": primary_name,
        "lower_timeframe": lower_name,
        "timeframes": states,
    }
