from __future__ import annotations
import os
from math import isfinite
from pathlib import Path
from typing import Any
from ..contracts import Candle, MarketSnapshot
from ..market_regime import classify_market

FRAME_ORDER = ("H4", "M15", "M5")
ENGINE = "FOREX_ENGINE"
STRATEGY = "FOREX_CHART_LIQUIDITY_SWEEP_STRUCTURE_BREAK"
_MEMORY = None
_MEMORY_MTIME = None

def _candles(frame) -> list[Candle]:
    return list(getattr(frame, "candles", ())) if frame is not None else []

def _side(c: Candle | None) -> str:
    return "BUY" if c and c.close > c.open else "SELL" if c and c.close < c.open else "NONE"

def _live_bar_age_seconds(candles, tick) -> float | None:
    if not candles or tick is None:
        return None
    current_start = (tick.timestamp.timestamp() // 300.0) * 300.0
    expected_last = current_start - 300.0
    last_start = candles[-1].timestamp.timestamp()
    if abs(last_start - expected_last) > 5.0:
        return None
    return max(0.0, tick.timestamp.timestamp() - current_start)

def _atr(frame, period=14) -> float:
    c = _candles(frame)
    if len(c) < 2: return 0.0
    tr = [max(x.high-x.low, abs(x.high-(c[i-1].close if i else x.open)), abs(x.low-(c[i-1].close if i else x.open))) for i,x in enumerate(c)]
    return sum(tr[-period:]) / min(period, len(tr))

def _direction(frame) -> str:
    c = _candles(frame)
    if len(c) < 8: return "NONE"
    if c[-1].high > c[-8].high and c[-1].low > c[-8].low: return "BUY"
    if c[-1].high < c[-8].high and c[-1].low < c[-8].low: return "SELL"
    return "MIXED"

def _chart_setup(frame, tick=None, expected_side: str | None = None) -> dict[str, Any]:
    c = _candles(frame)
    if len(c) < 20: return {"valid": False, "side": "NONE", "type": "NONE", "sweep": False, "break": False}
    cur, ref, recent = c[-1], c[-15:-5], c[-5:-1]
    hi, lo = max(x.high for x in ref), min(x.low for x in ref)
    live_ask = float(getattr(tick, "ask", 0.0) or 0.0) if tick is not None else 0.0
    live_bid = float(getattr(tick, "bid", 0.0) or 0.0) if tick is not None else 0.0
    live_age = _live_bar_age_seconds(c, tick)
    if live_age is not None:
        if live_age <= 120.0 and live_ask > cur.close and expected_side in (None, "BUY"):
            return {"valid": True, "side": "BUY", "type": "LIVE_STRUCTURE_BREAK", "sweep": False, "break": True, "level": cur.close, "reference_level": hi, "candle_color": "GREEN", "trigger_source": "LIVE_TICK", "trigger_price": live_ask, "trigger_age_seconds": live_age, "forming_open": cur.close, "forming_direction": "BUY"}
        if live_age <= 120.0 and live_bid < cur.close and expected_side in (None, "SELL"):
            return {"valid": True, "side": "SELL", "type": "LIVE_STRUCTURE_BREAK", "sweep": False, "break": True, "level": cur.close, "reference_level": lo, "candle_color": "RED", "trigger_source": "LIVE_TICK", "trigger_price": live_bid, "trigger_age_seconds": live_age, "forming_open": cur.close, "forming_direction": "SELL"}
        return {"valid": False, "side": "NONE", "type": "NONE", "sweep": False, "break": False, "trigger_source": "LIVE_WINDOW_CLOSED", "trigger_age_seconds": live_age}
    sl, sh = any(x.low < lo for x in recent), any(x.high > hi for x in recent)
    br_buy, br_sell = _side(cur) == "BUY" and cur.close > hi, _side(cur) == "SELL" and cur.close < lo
    rc_buy, rc_sell = sl and _side(cur) == "BUY" and cur.close > lo, sh and _side(cur) == "SELL" and cur.close < hi
    if br_buy and rc_buy: typ, side = "LIQUIDITY_SWEEP_BOS", "BUY"
    elif br_sell and rc_sell: typ, side = "LIQUIDITY_SWEEP_BOS", "SELL"
    elif br_buy: typ, side = "STRUCTURE_BREAK", "BUY"
    elif br_sell: typ, side = "STRUCTURE_BREAK", "SELL"
    elif rc_buy: typ, side = "LIQUIDITY_RECLAIM", "BUY"
    elif rc_sell: typ, side = "LIQUIDITY_RECLAIM", "SELL"
    else: return {"valid": False, "side": "NONE", "type": "NONE", "sweep": sl or sh, "break": False}
    if expected_side in {"BUY", "SELL"} and side != expected_side:
        return {"valid": False, "side": "NONE", "type": typ, "trigger_source": "SETUP_DIRECTION_MISMATCH", "setup_direction": expected_side, "detected_direction": side}
    return {"valid": True, "side": side, "type": typ, "sweep": sl or sh, "break": br_buy or br_sell, "level": hi if side == "BUY" else lo, "trigger_source": "COMPLETED_CANDLE"}

def _memory(frame, side: str) -> dict[str, Any]:
    global _MEMORY, _MEMORY_MTIME
    out = {"role": "SETUP_RECOGNITION", "recognized": False, "shape": None, "neighbours": 0}
    try:
        from ..pattern_memory import PatternMemory, encode_shape
        path = Path(os.getenv("MT5_PATTERN_MEMORY_PATH", "/opt/cipherfx_mt5/config/pattern_memory.npz"))
        stamp = path.stat().st_mtime_ns if path.exists() else None
        if _MEMORY is None or stamp != _MEMORY_MTIME: _MEMORY, _MEMORY_MTIME = PatternMemory.load(str(path)), stamp
        shape = encode_shape([x.close for x in _candles(frame)])
        out["shape"] = shape
        result = _MEMORY.query(shape) if _MEMORY is not None else None
        if result:
            taken = result["buy"] if side == "BUY" else result["sell"]
            other = result["sell"] if side == "BUY" else result["buy"]
            out.update({"recognized": True, "expected_r": round(taken, 4), "opposite_r": round(other, 4), "neighbours": result["n"]})
    except Exception: pass
    return out

def _context(frame) -> dict[str, Any]:
    c = _candles(frame)
    return {"available": bool(c), "direction": _direction(frame), "bar_time": c[-1].timestamp.isoformat() if c else ""}



def build_setup(snapshot: MarketSnapshot) -> dict[str, Any]:
    regime = classify_market(snapshot.frames)
    h4 = _context(snapshot.frames.get("H4"))
    m15 = _context(snapshot.frames.get("M15"))
    regime_h4 = ((regime.get("timeframes") or {}).get("H4") or {}).get("direction")
    if regime_h4 in {"BUY", "SELL"}:
        h4 = {**h4, "raw_direction": h4["direction"], "direction": regime_h4, "direction_source": "H4_REGIME"}
    setup_direction = h4["direction"] if h4["direction"] in {"BUY", "SELL"} else m15["direction"] if m15["direction"] in {"BUY", "SELL"} else None
    chart = _chart_setup(snapshot.frames.get("M5"), snapshot.tick, setup_direction)
    memory = _memory(snapshot.frames.get("M5"), setup_direction or chart["side"])
    m5 = _candles(snapshot.frames.get("M5"))
    frames = {"H4": h4, "M15": m15, "M5": {**chart, "bar_time": m5[-1].timestamp.isoformat() if m5 else ""}, "setup_direction": setup_direction or chart["side"]}
    reason = "" if chart["valid"] else chart.get("trigger_source") == "LIVE_WINDOW_CLOSED" and "M5_LIVE_TRIGGER_WINDOW_CLOSED" or "M5_CHART_SETUP_NOT_FOUND"
    if reason:
        return {"valid": False, "side": "NO_TRADE", "engine": ENGINE, "strategy_name": STRATEGY, "setup_type": STRATEGY, "frames": frames, "chart_setup": chart, "memory": memory, "context": {"h4": frames["H4"], "m15": frames["M15"], "market_regime": regime}, "decision_role": "CHART_SETUP_WITH_MEMORY", "rejection_reason": reason}
    side, atr = chart["side"], _atr(snapshot.frames.get("M5"))
    entry = float(snapshot.tick.ask if side == "BUY" else snapshot.tick.bid)
    stop=min(x.low for x in m5[-6:])-atr*.1 if side=="BUY" else max(x.high for x in m5[-6:])+atr*.1
    structural_distance=abs(entry-stop)
    distance=max(structural_distance, atr*1.0)
    stop_ok = bool(atr > 0 and distance >= atr*.2 and isfinite(distance))
    target = entry + distance*2 if side == "BUY" else entry - distance*2
    return {"valid": stop_ok, "side": side if stop_ok else "NO_TRADE", "engine": ENGINE, "strategy_name": STRATEGY, "setup_type": STRATEGY, "entry_model": "LIVE_M5_TICK_BREAK" if chart.get("trigger_source") == "LIVE_TICK" else "LIVE_CHART_IMMEDIATE", "frames": frames, "chart_setup": chart, "context": {"h4": frames["H4"], "m15": frames["M15"], "market_regime": regime}, "memory": memory, "entry": entry, "stop_loss": stop, "take_profit": target, "stop_distance": distance, "stop_valid": stop_ok, "minimum_rr": 2.0, "decision_role": "CHART_SETUP_WITH_MEMORY", "reasons": [f"CHART_SETUP_{chart['type']}", f"CHART_DIRECTION_{side}", "LIVE_TICK_TRIGGER" if chart.get("trigger_source") == "LIVE_TICK" else "COMPLETED_CANDLE_TRIGGER", "MEMORY_RECOGNIZED" if memory.get("recognized") else "MEMORY_OBSERVATION_ONLY", "STOP_DISTANCE_PASS" if stop_ok else "STOP_DISTANCE_INVALID"], "rejection_reason": "" if stop_ok else "STOP_DISTANCE_INVALID","setup_direction":setup_direction or side}
