"""Production remediation primitives for the active CipherFX route.

This module is deliberately side-effect free. It provides structural shadow
evidence and a bounded worker for non-critical intelligence persistence. It
does not submit, reject, expire, or modify trades.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import math
import queue
import threading
from typing import Any, Callable, Iterable

UTC = timezone.utc

def utc_now() -> str:
    return datetime.now(UTC).isoformat()

def _value(candle: Any, name: str, default: float = 0.0) -> float:
    try:
        value = float(getattr(candle, name, default))
        return value if math.isfinite(value) else default
    except Exception:
        return default

def _timestamp(candle: Any) -> str:
    return str(getattr(candle, "timestamp", "") or "")

def _atr(candles: list[Any], period: int = 14) -> float:
    if len(candles) < 2:
        return 0.0
    values: list[float] = []
    previous = None
    for candle in candles[-max(period + 1, 2):]:
        high = _value(candle, "high")
        low = _value(candle, "low")
        close = _value(candle, "close")
        values.append(max(high - low, abs(high - previous), abs(low - previous)) if previous is not None else max(high - low, 0.0))
        previous = close
    return sum(values[-period:]) / max(1, min(period, len(values)))

def _swing_points(candles: list[Any], left: int = 2, right: int = 2) -> tuple[list[dict], list[dict]]:
    highs: list[dict] = []
    lows: list[dict] = []
    for index in range(left, max(left, len(candles) - right)):
        candle = candles[index]
        high = _value(candle, "high")
        low = _value(candle, "low")
        prior = candles[index - left:index]
        later = candles[index + 1:index + right + 1]
        if prior and later and all(high > _value(item, "high") for item in prior + later):
            highs.append({"price": high, "time": _timestamp(candle), "index": index})
        if prior and later and all(low < _value(item, "low") for item in prior + later):
            lows.append({"price": low, "time": _timestamp(candle), "index": index})
    return highs, lows

def evaluate_h1_structure(candles: Iterable[Any], expected_side: str = "") -> dict[str, Any]:
    """Return closed-candle H1 structure evidence without changing permission."""
    rows = list(candles or [])
    if len(rows) < 12:
        return {"state": "INSUFFICIENT_DATA", "side": "NO_TRADE", "confidence": 0.0, "reason": "H1 structure history incomplete", "diagnostic_only": True}
    sample = rows[-80:]
    atr = _atr(sample)
    highs, lows = _swing_points(sample)
    if len(highs) < 2 or len(lows) < 2 or atr <= 0:
        return {"state": "INSUFFICIENT_DATA", "side": "NO_TRADE", "confidence": 0.0, "atr": atr, "reason": "confirmed H1 swings unavailable", "diagnostic_only": True}
    h1, h2 = highs[-1], highs[-2]
    l1, l2 = lows[-1], lows[-2]
    higher_high = h1["price"] > h2["price"]
    higher_low = l1["price"] > l2["price"]
    lower_high = h1["price"] < h2["price"]
    lower_low = l1["price"] < l2["price"]
    current = _value(sample[-1], "close")
    range_high = max(_value(item, "high") for item in sample[-12:])
    range_low = min(_value(item, "low") for item in sample[-12:])
    range_width = max(0.0, range_high - range_low)
    last_bos = "BUY" if current > h2["price"] else "SELL" if current < l2["price"] else ""
    if higher_high and higher_low:
        state, side = "BULLISH_STRUCTURE", "BUY"
    elif lower_high and lower_low:
        state, side = "BEARISH_STRUCTURE", "SELL"
    elif range_width <= max(atr * 2.0, 1e-12):
        state, side = "RANGE", "NO_TRADE"
    else:
        state, side = "TRANSITION", "NO_TRADE"
    confidence = 90.0 if state in {"BULLISH_STRUCTURE", "BEARISH_STRUCTURE"} else 55.0 if state == "RANGE" else 35.0
    return {
        "state": state, "side": side, "expected_side": str(expected_side or "").upper(),
        "last_confirmed_swing_high": h1, "previous_confirmed_swing_high": h2,
        "last_confirmed_swing_low": l1, "previous_confirmed_swing_low": l2,
        "last_bos_direction": last_bos,
        "structure_sequence": {"higher_high": higher_high, "higher_low": higher_low, "lower_high": lower_high, "lower_low": lower_low},
        "range_high": range_high, "range_low": range_low, "range_width": range_width,
        "range_width_atr": round(range_width / atr, 4) if atr else None, "atr": atr,
        "structure_confidence": confidence,
        "structure_invalidation": l1["price"] if side == "BUY" else h1["price"] if side == "SELL" else None,
        "closed_candle_time": _timestamp(sample[-1]), "diagnostic_only": True,
    }

def build_m15_setup(candles: Iterable[Any], expected_side: str = "", asset_class: str = "") -> dict[str, Any]:
    """Build a closed-M15 setup-location candidate for shadow validation."""
    rows = list(candles or [])
    side = str(expected_side or "").upper()
    if len(rows) < 16 or side not in {"BUY", "SELL"}:
        return {"status": "DATA_MISSING", "setup_type": "NO_SETUP", "direction": side, "diagnostic_only": True}
    current = rows[-1]
    history = rows[-13:-1]
    atr = _atr(rows)
    prior_high = max(_value(item, "high") for item in history)
    prior_low = min(_value(item, "low") for item in history)
    body = abs(_value(current, "close") - _value(current, "open"))
    displacement = body / atr if atr else 0.0
    bullish = _value(current, "close") > _value(current, "open")
    if side == "BUY" and _value(current, "low") < prior_low and bullish:
        setup_type = "LIQUIDITY_SWEEP_RECLAIM"
    elif side == "SELL" and _value(current, "high") > prior_high and not bullish:
        setup_type = "LIQUIDITY_SWEEP_RECLAIM"
    elif (side == "BUY" and _value(current, "close") > prior_high) or (side == "SELL" and _value(current, "close") < prior_low):
        setup_type = "BREAKOUT_RETEST_CANDIDATE"
    else:
        setup_type = "TREND_PULLBACK_VALUE"
    zone_half = max(atr * 0.35, body * 0.5, 1e-12)
    origin = _value(current, "close")
    zone_low, zone_high = origin - zone_half, origin + zone_half
    invalidation = zone_low - atr * 0.50 if side == "BUY" else zone_high + atr * 0.50
    target_1 = prior_high if side == "BUY" else prior_low
    target_2 = origin + atr * 1.5 if side == "BUY" else origin - atr * 1.5
    quality = min(100.0, 40.0 + min(30.0, displacement * 20.0) + (20.0 if bullish == (side == "BUY") else 0.0) + (10.0 if atr > 0 else 0.0))
    setup_id = "m15_" + hashlib.sha256("|".join((str(_timestamp(current)), side, setup_type, str(asset_class or ""))).encode()).hexdigest()[:20]
    return {
        "status": "READY", "setup_id": setup_id, "setup_type": setup_type, "direction": side,
        "source_candle_timestamp": _timestamp(current), "created_at": utc_now(), "valid_from": _timestamp(current),
        "valid_until": _timestamp(current), "entry_zone_low": zone_low, "entry_zone_high": zone_high,
        "invalidation_price": invalidation, "target_1": target_1, "target_2": target_2,
        "setup_origin_price": origin, "liquidity_reference": {"high": prior_high, "low": prior_low},
        "structure_reference": {"asset_class": asset_class, "source": "closed_M15_candles"},
        "atr_at_creation": atr, "setup_quality_score": round(quality, 2), "rejection_reasons": [],
        "diagnostic_only": True,
    }

@dataclass(frozen=True)
class CanonicalDecision:
    decision_id: str
    scan_id: str
    canonical_symbol: str
    broker_symbol: str
    engine: str
    strategy_name: str
    strategy_version: str
    decision_created_at: str
    h4_direction: str
    h4_score: float
    h1_direction: str
    h1_structure_state: str
    h1_score: float
    m15_setup_type: str
    m15_setup_direction: str
    m15_entry_zone_low: float | None
    m15_entry_zone_high: float | None
    m15_invalidation_price: float | None
    m15_setup_quality: float | None
    m5_trigger_type: str
    m5_trigger_direction: str
    m5_trigger_score: float
    final_eligibility: str
    rejection_reason: str
    planned_direction: str
    plan_id: str
    m1_role: str = "diagnostic_only"
    learning_mode: str = "SHADOW_ONLY"
    def payload(self) -> dict[str, Any]:
        return asdict(self)

class BoundedShadowWorker:
    """Run non-essential intelligence/storage work off the scan thread."""
    def __init__(self, handler: Callable[[dict[str, Any]], None], maxsize: int = 512):
        self._handler = handler; self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=maxsize); self._stop = threading.Event(); self._thread: threading.Thread | None = None; self.dropped = 0
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive(): return
        self._stop.clear(); self._thread = threading.Thread(target=self._run, name="cipherfx-shadow-worker", daemon=True); self._thread.start()
    def submit(self, item: dict[str, Any]) -> bool:
        try: self._queue.put_nowait(dict(item)); return True
        except queue.Full: self.dropped += 1; return False
    def _run(self) -> None:
        while not self._stop.is_set() or not self._queue.empty():
            try: item = self._queue.get(timeout=0.25)
            except queue.Empty: continue
            try: self._handler(item)
            except Exception: pass
            finally: self._queue.task_done()
    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread is not None and self._thread.is_alive(): self._thread.join(timeout=timeout)
        self._thread = None
    @property
    def pending(self) -> int: return self._queue.qsize()
