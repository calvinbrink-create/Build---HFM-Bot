"""CipherFX active strategy package.

This module is the single strategy authority used by the live bot. It keeps
one public decision contract while isolating the three asset engines:
FOREX, INDEX, and METALS. It never submits orders.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

SIDES = {"BUY", "SELL"}
ASSETS = {"forex", "index", "metal"}


class SetupState:
    NEW = "NEW"
    WAITING_M1 = "WAITING_M1"
    CONFIRMED = "CONFIRMED"
    EXECUTION_PENDING = "EXECUTION_PENDING"
    EXECUTED = "EXECUTED"
    EXPIRED = "EXPIRED"
    INVALIDATED = "INVALIDATED"
    REJECTED = "REJECTED"


TERMINAL_STATES = {
    SetupState.EXECUTED,
    SetupState.EXPIRED,
    SetupState.INVALIDATED,
    SetupState.REJECTED,
}


@dataclass(frozen=True)
class Candle:
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass(frozen=True)
class Frame:
    timeframe: str
    candles: Tuple[Candle, ...]


@dataclass(frozen=True)
class Profile:
    symbol: str
    asset_class: str
    stop_atr: float
    target_r: float
    spread_limit_atr: float
    risk_fraction: float
    session: str = "all"
    h4_min_score: float = 70.0
    h1_min_score: float = 70.0
    m15_min_score: float = 70.0
    m5_min_score: float = 70.0
    minimum_strategy_score: float = 70.0
    m1_body_fraction_min: float = 0.25
    m1_buy_close_location_min: float = 0.60
    m1_sell_close_location_max: float = 0.40
    strategy_family: str = ""


@dataclass(frozen=True)
class TimeframeDecision:
    timeframe: str
    side: str
    score: float
    bar_open_time: datetime
    bar_close_time: datetime
    reasons: List[str]
    metrics: Dict[str, object]


@dataclass(frozen=True)
class HTFContext:
    context_id: str
    symbol: str
    asset_class: str
    side: str
    h4_decision: TimeframeDecision
    h1_decision: TimeframeDecision
    m15_decision: TimeframeDecision
    created_at: datetime
    status: str = "ACTIVE"


@dataclass
class Decision:
    symbol: str
    asset_class: str
    engine: str = ""
    setup_type: str = ""
    side: Optional[str] = None
    state: str = SetupState.REJECTED
    reason: str = ""
    score: float = 0.0
    stop_distance: float = 0.0
    target_distance: float = 0.0
    expected_edge_r: float = 0.0
    risk_fraction: float = 0.0
    reasons: List[str] = field(default_factory=list)
    evidence: Dict[str, object] = field(default_factory=dict)

    def block(self, reason: str) -> None:
        self.reason = self.reason or reason
        if reason and reason not in self.reasons:
            self.reasons.append(reason)
        self.state = SetupState.REJECTED


def finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        text = str(value or "").strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            result = datetime.fromisoformat(text)
        except ValueError:
            result = datetime(1970, 1, 1, tzinfo=timezone.utc)
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)


def _times(frame: Optional[Frame], minutes: int) -> Tuple[datetime, datetime]:
    if not frame or not frame.candles:
        value = datetime(1970, 1, 1, tzinfo=timezone.utc)
        return value, value
    opened = _dt(frame.candles[-1].timestamp)
    return opened, opened + timedelta(minutes=minutes)


def _closes(frame: Frame) -> List[float]:
    return [float(c.close) for c in frame.candles if finite(c.close)]


def _ranges(frame: Frame) -> List[float]:
    return [max(float(c.high) - float(c.low), 1e-12) for c in frame.candles]


def _ema(frame: Frame, period: int) -> float:
    values = _closes(frame)
    if not values:
        return 0.0
    alpha = 2.0 / (period + 1.0)
    value = values[0]
    for item in values[1:]:
        value = alpha * item + (1.0 - alpha) * value
    return value


def _atr(frame: Optional[Frame], period: int = 14) -> float:
    if not frame or len(frame.candles) < 2:
        return 0.0
    values = []
    for idx, candle in enumerate(frame.candles):
        previous = frame.candles[idx - 1].close if idx else candle.open
        values.append(max(candle.high - candle.low, abs(candle.high - previous), abs(candle.low - previous)))
    values = values[-period:]
    return sum(values) / len(values) if values else 0.0


def _rmi(frame: Frame, period: int = 14, momentum: int = 5) -> float:
    values = _closes(frame)
    if len(values) <= momentum + 2:
        return 50.0
    gains, losses = [], []
    for idx in range(momentum, len(values)):
        delta = values[idx] - values[idx - momentum]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    gains, losses = gains[-period:], losses[-period:]
    gain = sum(gains) / len(gains)
    loss = sum(losses) / len(losses)
    if loss <= 1e-12:
        return 100.0 if gain > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + gain / loss)


def _adx(frame: Frame, period: int = 14) -> float:
    if len(frame.candles) < period + 2:
        return 0.0
    sample = frame.candles[-period:]
    ranges = [max(c.high - c.low, 1e-12) for c in sample]
    moves = [
        abs(frame.candles[i].close - frame.candles[i - 1].close)
        for i in range(len(frame.candles) - period, len(frame.candles))
    ]
    return min(100.0, 100.0 * (sum(moves) / len(moves)) / (sum(ranges) / len(ranges)))


def _body(candle: Candle) -> float:
    return abs(candle.close - candle.open) / max(candle.high - candle.low, 1e-12)


def _location(candle: Candle) -> float:
    return (candle.close - candle.low) / max(candle.high - candle.low, 1e-12)


def _empty(frame: Optional[Frame], label: str, reason: str) -> TimeframeDecision:
    opened, closed = _times(frame, {"H4": 240, "H1": 60, "M15": 15, "M5": 5}.get(label, 1))
    return TimeframeDecision(
        label,
        "NO_TRADE",
        0.0,
        opened,
        closed,
        [reason],
        {"data_status": "MISSING_OR_INSUFFICIENT", "reason_code": reason},
    )


def _pack(decision: TimeframeDecision) -> Dict[str, Any]:
    return {
        "timeframe": decision.timeframe,
        "side": decision.side,
        "score": float(decision.score),
        "bar_open_time": decision.bar_open_time.isoformat(),
        "bar_close_time": decision.bar_close_time.isoformat(),
        "reasons": list(decision.reasons),
        "metrics": dict(decision.metrics),
    }


class AssetEngine:
    asset_class = ""
    engine_name = ""
    m15_setup_name = ""
    m5_trigger_name = ""
    total_weights: Dict[str, float] = {}

    def htf(self, frame: Optional[Frame], label: str, expected: Optional[str], minimum: float) -> TimeframeDecision:
        if frame is None or len(frame.candles) < 40:
            return _empty(frame, label, "DATA_INSUFFICIENT")
        opened, closed = _times(frame, {"H4": 240, "H1": 60}[label])
        close = float(frame.candles[-1].close)
        ema20 = _ema(frame, 20)
        ema50 = _ema(frame, 50)
        prior = _ema(Frame(frame.timeframe, frame.candles[:-4]), 20)
        components = self.htf_components(frame, close, ema20, ema50, prior)
        buy = sum(weight for enabled, weight in components["BUY"].values() if enabled)
        sell = sum(weight for enabled, weight in components["SELL"].values() if enabled)
        if buy >= minimum and buy > sell:
            side, score = "BUY", buy
        elif sell >= minimum and sell > buy:
            side, score = "SELL", sell
        else:
            side, score = "NO_TRADE", max(buy, sell)
        reasons = ["HTF_DIRECTION_PASS"] if side in SIDES else ["MIXED_OR_NEUTRAL_STRUCTURE"]
        if expected in SIDES and side != expected:
            reasons.append(label + "_NOT_ALIGNED")
        return TimeframeDecision(
            label,
            side,
            float(score),
            opened,
            closed,
            reasons,
            {
                "close": close,
                "ema20": ema20,
                "ema50": ema50,
                "rmi": _rmi(frame),
                "adx": _adx(frame),
                "components_buy": components["BUY"],
                "components_sell": components["SELL"],
                "minimum_score": minimum,
                "data_status": "OK",
                "engine": self.engine_name,
            },
        )

    def htf_components(self, frame: Frame, close: float, ema20: float, ema50: float, prior: float) -> Dict[str, Dict[str, float]]:
        raise NotImplementedError

    def m15(self, frame: Optional[Frame], expected: str, current_price: Optional[float], minimum: float) -> TimeframeDecision:
        raise NotImplementedError

    def m5(self, frame: Optional[Frame], expected: Optional[str], minimum: float, family: str) -> TimeframeDecision:
        raise NotImplementedError

    def evaluate_h4(self, frame: Optional[Frame], minimum: float) -> TimeframeDecision:
        return self.htf(frame, "H4", None, minimum)

    def evaluate_h1(self, frame: Optional[Frame], expected: Optional[str], minimum: float) -> TimeframeDecision:
        return self.htf(frame, "H1", expected, minimum)


class ForexEngine(AssetEngine):
    asset_class = "forex"
    engine_name = "FOREX_TREND_PULLBACK"
    m15_setup_name = "FOREX_POI_CHOCH_BOS_RETRACEMENT"
    m5_trigger_name = "FX_PULLBACK_OR_LIQUIDITY_RECLAIM"
    total_weights = {"context": 30.0, "m15": 30.0, "m5": 30.0, "regime": 5.0, "data": 5.0}

    def htf_components(self, frame, close, ema20, ema50, prior):
        rmi, adx = _rmi(frame), _adx(frame)
        values = _closes(frame)
        mid = sum(values[-20:]) / max(len(values[-20:]), 1)
        return {
            "BUY": {
                "ema_stack": (close > ema20 and ema20 > ema50, 30.0),
                "rmi": (rmi >= 52, 25.0),
                "slope": (ema20 > prior, 20.0),
                "adx_regime": (adx >= 15, 15.0),
                "location": (close >= mid, 10.0),
            },
            "SELL": {
                "ema_stack": (close < ema20 and ema20 < ema50, 30.0),
                "rmi": (rmi <= 48, 25.0),
                "slope": (ema20 < prior, 20.0),
                "adx_regime": (adx >= 15, 15.0),
                "location": (close <= mid, 10.0),
            },
        }

    def m15(self, frame, expected, current_price, minimum):
        if frame is None or len(frame.candles) < 40:
            return _empty(frame, "M15", "DATA_INSUFFICIENT")
        opened, closed = _times(frame, 15)
        candles, last = list(frame.candles), frame.candles[-1]
        atr, ema20 = _atr(frame), _ema(frame, 20)
        price = float(current_price if finite(current_price) else last.close)
        tolerance = max(atr * 0.75, abs(last.close) * 0.0002, 1e-12)
        poi_types = []
        recent = candles[-16:-1]
        if recent and abs(price - min(c.low for c in recent)) <= tolerance:
            poi_types.append("LIQUIDITY_LOW")
        if recent and abs(price - max(c.high for c in recent)) <= tolerance:
            poi_types.append("LIQUIDITY_HIGH")
        if abs(price - ema20) <= tolerance:
            poi_types.append("EMA20_RETEST")
        prior = candles[-9:-1]
        if expected == "BUY":
            bos = last.close > max(c.high for c in prior)
            retracement = any(c.low <= ema20 + atr * 0.2 for c in candles[-5:-1]) and last.close >= ema20
            countertrend = min(c.close for c in candles[-6:-1]) < min(c.close for c in candles[-12:-6])
            choch = countertrend and last.close > max(c.high for c in candles[-5:-1])
        else:
            bos = last.close < min(c.low for c in prior)
            retracement = any(c.high >= ema20 - atr * 0.2 for c in candles[-5:-1]) and last.close <= ema20
            countertrend = max(c.close for c in candles[-6:-1]) > max(c.close for c in candles[-12:-6])
            choch = countertrend and last.close < min(c.low for c in candles[-5:-1])
        parts = {"poi": bool(poi_types), "choch": choch, "bos": bos, "retracement": retracement}
        weights = {"poi": 30.0, "choch": 25.0, "bos": 25.0, "retracement": 20.0}
        score = sum(weights[k] for k, v in parts.items() if v)
        passed = parts["poi"] and parts["retracement"] and (parts["choch"] or parts["bos"]) and score >= minimum
        reasons = [self.m15_setup_name + "_PASS"] if passed else [k.upper() + "_MISSING" for k, v in parts.items() if not v]
        invalidation = min(c.low for c in candles[-8:-1]) if expected == "BUY" else max(c.high for c in candles[-8:-1])
        return TimeframeDecision(
            "M15", expected if passed else "NO_TRADE", score, opened, closed, reasons,
            {**parts, "weights": weights, "poi_types": list(dict.fromkeys(poi_types)),
             "setup_type": self.m15_setup_name, "entry_zone_low": invalidation if expected == "BUY" else last.low,
             "entry_zone_high": last.high if expected == "BUY" else invalidation,
             "invalidation_price": invalidation, "atr": atr, "ema20": ema20, "close": last.close,
             "required_score": minimum, "engine": self.engine_name},
        )

    def m5(self, frame, expected, minimum, family):
        return _m5_asset(frame, expected, self.asset_class, minimum, family, self.m5_trigger_name, {"break": 45.0, "direction": 20.0, "body": 20.0, "range": 15.0}, 0.20, 0.55, 0.45, False, 0.0)


class IndexEngine(AssetEngine):
    asset_class = "index"
    engine_name = "INDEX_SESSION_BREAKOUT"
    m15_setup_name = "INDEX_SESSION_RANGE_BREAK_RETEST"
    m5_trigger_name = "INDEX_SESSION_BREAKOUT_OR_RETEST"
    total_weights = {"context": 25.0, "m15": 30.0, "m5": 35.0, "session": 5.0, "data": 5.0}

    def htf_components(self, frame, close, ema20, ema50, prior):
        ranges = _ranges(frame)
        values = _closes(frame)
        avg_range = sum(ranges[-21:-1]) / max(len(ranges[-21:-1]), 1)
        session_mean = sum(values[-13:-1]) / max(len(values[-13:-1]), 1)
        adx = _adx(frame)
        return {
            "BUY": {
                "trend_stack": (close > ema20 and ema20 > ema50, 25.0),
                "session_bias": (close >= session_mean, 25.0),
                "range_expansion": (ranges[-1] >= avg_range, 20.0),
                "adx_regime": (adx >= 18, 20.0),
                "location": (close > prior, 10.0),
            },
            "SELL": {
                "trend_stack": (close < ema20 and ema20 < ema50, 25.0),
                "session_bias": (close <= session_mean, 25.0),
                "range_expansion": (ranges[-1] >= avg_range, 20.0),
                "adx_regime": (adx >= 18, 20.0),
                "location": (close < prior, 10.0),
            },
        }

    def m15(self, frame, expected, current_price, minimum):
        if frame is None or len(frame.candles) < 40:
            return _empty(frame, "M15", "DATA_INSUFFICIENT")
        opened, closed = _times(frame, 15)
        candles, last = list(frame.candles), frame.candles[-1]
        atr = _atr(frame)
        recent = candles[-25:-1]
        session_high, session_low = max(c.high for c in recent), min(c.low for c in recent)
        price = float(current_price if finite(current_price) else last.close)
        poi = abs(price - session_high) <= max(atr * 0.75, 1e-12) or abs(price - session_low) <= max(atr * 0.75, 1e-12)
        if expected == "BUY":
            session_break = last.close > session_high
            range_retest = any(c.low <= session_high for c in candles[-5:-1]) and last.close > session_high
        else:
            session_break = last.close < session_low
            range_retest = any(c.high >= session_low for c in candles[-5:-1]) and last.close < session_low
        momentum = _body(last) >= 0.20
        parts = {"session_poi": poi, "session_break": session_break, "range_retest": range_retest, "momentum": momentum}
        weights = {"session_poi": 25.0, "session_break": 30.0, "range_retest": 25.0, "momentum": 20.0}
        score = sum(weights[k] for k, v in parts.items() if v)
        passed = parts["session_poi"] and (parts["session_break"] or parts["range_retest"]) and parts["momentum"] and score >= minimum
        reasons = [self.m15_setup_name + "_PASS"] if passed else [k.upper() + "_MISSING" for k, v in parts.items() if not v]
        invalidation = session_low if expected == "BUY" else session_high
        return TimeframeDecision(
            "M15", expected if passed else "NO_TRADE", score, opened, closed, reasons,
            {**parts, "weights": weights, "setup_type": self.m15_setup_name,
             "entry_zone_low": invalidation if expected == "BUY" else last.low,
             "entry_zone_high": last.high if expected == "BUY" else invalidation,
             "invalidation_price": invalidation, "atr": atr, "close": last.close,
             "required_score": minimum, "engine": self.engine_name},
        )

    def m5(self, frame, expected, minimum, family):
        return _m5_asset(frame, expected, self.asset_class, minimum, family, self.m5_trigger_name, {"break": 40.0, "direction": 20.0, "body": 20.0, "range": 20.0}, 0.25, 0.55, 0.45, False, 0.0)


class MetalsEngine(AssetEngine):
    asset_class = "metal"
    engine_name = "METAL_VOLATILITY_TREND"
    m15_setup_name = "METAL_VOLATILITY_PULLBACK_RECLAIM"
    m5_trigger_name = "METAL_VOLATILITY_EXPANSION_RECLAIM"
    total_weights = {"context": 25.0, "m15": 30.0, "m5": 30.0, "volatility": 10.0, "data": 5.0}

    def htf_components(self, frame, close, ema20, ema50, prior):
        ranges = _ranges(frame)
        base = sum(ranges[-21:-1]) / max(len(ranges[-21:-1]), 1)
        recent = sum(ranges[-5:]) / 5.0
        rmi = _rmi(frame)
        return {
            "BUY": {
                "ema_structure": (close > ema20 and ema20 > ema50, 20.0),
                "volatility_expansion": (recent >= base, 30.0),
                "momentum": (rmi >= 55, 25.0),
                "location": (close > prior, 15.0),
                "candle_pressure": (_body(frame.candles[-1]) >= 0.30, 10.0),
            },
            "SELL": {
                "ema_structure": (close < ema20 and ema20 < ema50, 20.0),
                "volatility_expansion": (recent >= base, 30.0),
                "momentum": (rmi <= 45, 25.0),
                "location": (close < prior, 15.0),
                "candle_pressure": (_body(frame.candles[-1]) >= 0.30, 10.0),
            },
        }

    def m15(self, frame, expected, current_price, minimum):
        if frame is None or len(frame.candles) < 40:
            return _empty(frame, "M15", "DATA_INSUFFICIENT")
        opened, closed = _times(frame, 15)
        candles, last = list(frame.candles), frame.candles[-1]
        atr, ema20 = _atr(frame), _ema(frame, 20)
        price = float(current_price if finite(current_price) else last.close)
        tolerance = max(atr * 0.75, abs(last.close) * 0.0003, 1e-12)
        recent = candles[-16:-1]
        poi = bool(recent) and (abs(price - min(c.low for c in recent)) <= tolerance or abs(price - max(c.high for c in recent)) <= tolerance or abs(price - ema20) <= tolerance)
        impulse = (last.high - last.low) >= max(atr * 1.10, 1e-12)
        retracement = any((c.low <= ema20 + atr * 0.2 if expected == "BUY" else c.high >= ema20 - atr * 0.2) for c in candles[-5:-1])
        reclaim = (last.close > ema20 and last.close > last.open) if expected == "BUY" else (last.close < ema20 and last.close < last.open)
        parts = {"volatility_poi": poi, "impulse": impulse, "retracement": retracement, "volatility_reclaim": reclaim}
        weights = {"volatility_poi": 25.0, "impulse": 30.0, "retracement": 20.0, "volatility_reclaim": 25.0}
        score = sum(weights[k] for k, v in parts.items() if v)
        passed = all(parts.values()) and score >= minimum
        reasons = [self.m15_setup_name + "_PASS"] if passed else [k.upper() + "_MISSING" for k, v in parts.items() if not v]
        invalidation = min(c.low for c in candles[-8:-1]) if expected == "BUY" else max(c.high for c in candles[-8:-1])
        return TimeframeDecision(
            "M15", expected if passed else "NO_TRADE", score, opened, closed, reasons,
            {**parts, "weights": weights, "setup_type": self.m15_setup_name,
             "entry_zone_low": invalidation if expected == "BUY" else last.low,
             "entry_zone_high": last.high if expected == "BUY" else invalidation,
             "invalidation_price": invalidation, "atr": atr, "ema20": ema20,
             "close": last.close, "required_score": minimum, "engine": self.engine_name},
        )

    def m5(self, frame, expected, minimum, family):
        return _m5_asset(frame, expected, self.asset_class, minimum, family, self.m5_trigger_name, {"break": 35.0, "direction": 25.0, "body": 20.0, "range": 20.0}, 0.30, 0.60, 0.40, True, 1.10)


def _m5_asset(frame, expected, asset, minimum, family, trigger_family, weights, body_min, buy_location, sell_location, require_range, range_atr):
    if frame is None or len(frame.candles) < 12:
        return _empty(frame, "M5", "M5_DATA_INSUFFICIENT")
    opened, closed = _times(frame, 5)
    candles, last = list(frame.candles), frame.candles[-1]
    previous = candles[-6:-1]
    high_level, low_level = max(c.high for c in previous), min(c.low for c in previous)
    body, location = _body(last), _location(last)
    buy_break = last.close > high_level and last.close > last.open
    sell_break = last.close < low_level and last.close < last.open
    buy_retest = last.low <= high_level and last.close > high_level and last.close > last.open
    sell_retest = last.high >= low_level and last.close < low_level and last.close < last.open
    directional = (location >= buy_location) if expected == "BUY" else (location <= sell_location) if expected == "SELL" else False
    base_trigger = (buy_break or buy_retest) if expected == "BUY" else (sell_break or sell_retest) if expected == "SELL" else False
    trigger = "BREAK" if (buy_break or sell_break) else "RETEST" if (buy_retest or sell_retest) else ""
    range_ok = (last.high - last.low) >= max(_atr(frame) * range_atr, 1e-12) if require_range else True
    parts = {"break": bool(base_trigger), "direction": bool(directional), "body": body >= body_min, "range": bool(range_ok)}
    score = sum(float(weights[k]) for k, v in parts.items() if v)
    triggered = all(parts.values())
    if triggered:
        side, score, reasons = expected, 100.0, ["M5_BREAK_OR_RETEST_PASS"]
    else:
        side, reasons = "NO_TRADE", ["M5_TRIGGER_NOT_READY"] + [k.upper() + "_MISSING" for k, v in parts.items() if not v]
    return TimeframeDecision(
        "M5", side, score, opened, closed, reasons,
        {**parts, "trigger_type": trigger, "break": bool(buy_break or sell_break),
         "retest": bool(buy_retest or sell_retest), "body_fraction": body,
         "close_location": location, "high_level": high_level, "low_level": low_level,
         "asset_class": asset, "strategy_family": family, "trigger_family": trigger_family,
         "body_minimum": body_min, "range_ok": range_ok, "minimum_score": minimum,
         "bar_open_time": opened.isoformat()},
    )


def _engine_for(asset: str) -> AssetEngine:
    return {"forex": ForexEngine(), "index": IndexEngine(), "metal": MetalsEngine()}.get(asset, ForexEngine())


def evaluate_h4(frame: Frame, minimum: float = 70.0) -> TimeframeDecision:
    return ForexEngine().evaluate_h4(frame, minimum)


def evaluate_h1(frame: Frame, expected_side: Optional[str], minimum: float = 70.0) -> TimeframeDecision:
    return ForexEngine().evaluate_h1(frame, expected_side, minimum)


def evaluate_m15(frame: Frame, expected_side: str, minimum: float = 70.0, current_price: Optional[float] = None, asset_class: str = "forex") -> TimeframeDecision:
    return _engine_for(asset_class).m15(frame, expected_side, current_price, minimum)


def evaluate_m5_trigger(frame: Frame, expected_side: str, asset_class: str = "forex", minimum: float = 70.0, strategy_family: str = "") -> TimeframeDecision:
    return _engine_for(asset_class).m5(frame, expected_side, minimum, strategy_family)


def evaluate(symbol: str, profile: Profile, frames: Dict[str, Frame], m5_is_forming: bool = False, current_price: Optional[float] = None) -> Decision:
    engine = _engine_for(profile.asset_class)
    h4 = engine.evaluate_h4(frames.get("H4"), profile.h4_min_score)
    if h4.side in SIDES:
        h1 = engine.evaluate_h1(frames.get("H1"), h4.side, profile.h1_min_score)
        m15 = engine.m15(frames.get("M15"), h4.side, current_price, profile.m15_min_score)
        m5 = engine.m5(frames.get("M5"), h4.side, profile.m5_min_score, profile.strategy_family)
    else:
        h1 = engine.evaluate_h1(frames.get("H1"), None, profile.h1_min_score)
        m15 = _empty(frames.get("M15"), "M15", "H4_NO_DIRECTION")
        m5 = _empty(frames.get("M5"), "M5", "H4_NO_DIRECTION")

    h4_ok = h4.side in SIDES and h4.score >= profile.h4_min_score
    h1_ok = h4_ok and h1.side == h4.side and h1.score >= profile.h1_min_score
    m15_ok = h1_ok and m15.side == h4.side and m15.score >= profile.m15_min_score
    m5_ok = m15_ok and m5.side == h4.side and m5.score >= profile.m5_min_score

    weights = dict(engine.total_weights)
    components = {
        "engine_context": weights["context"] if h4_ok and h1_ok else 0.0,
        "m15_engine_quality": weights["m15"] * min(m15.score, 100.0) / 100.0 if h1_ok else 0.0,
        "m5_engine_quality": weights["m5"] * min(m5.score, 100.0) / 100.0 if m15_ok else 0.0,
        "engine_regime": weights.get("regime", weights.get("session", weights.get("volatility", 0.0))) if h1_ok else 0.0,
        "data_quality": weights["data"] if all(frames.get(k) and len(frames[k].candles) >= 12 for k in ("H4", "H1", "M15", "M5")) else 0.0,
    }
    total = round(sum(components.values()), 2)

    if h4.side not in SIDES:
        reason = "H4_NO_DIRECTION"
    elif not h4_ok:
        reason = "H4_SCORE_BELOW_MINIMUM"
    elif h1.side != h4.side:
        reason = "H1_NOT_ALIGNED"
    elif h1.score < profile.h1_min_score:
        reason = "H1_SCORE_BELOW_MINIMUM"
    elif m15.side != h4.side:
        reason = "M15_NOT_ALIGNED"
    elif m15.score < profile.m15_min_score:
        reason = "M15_SCORE_BELOW_MINIMUM"
    elif m5.side != h4.side:
        reason = "M5_TRIGGER_NOT_READY"
    elif m5.score < profile.m5_min_score:
        reason = "M5_TRIGGER_BELOW_MINIMUM"
    elif total < profile.minimum_strategy_score:
        reason = "STRATEGY_SCORE_BELOW_MINIMUM"
    else:
        reason = ""

    evidence = {
        "ruleset_version": "asset_engines_v1",
        "engine": engine.engine_name,
        "higher_timeframes": {"H4": _pack(h4), "H1": _pack(h1), "M15": _pack(m15)},
        "m5_trigger": _pack(m5),
        "m1": {"status": "NOT_REQUIRED", "role": "M5 trigger is execution event"},
        "score_metric": {
            "name": "Cipher FX Scoring Metric",
            "engine": engine.engine_name,
            "total": total,
            "minimum": profile.minimum_strategy_score,
            "components": components,
            "weights": weights,
            "enforced": True,
        },
        "m5_is_forming": bool(m5_is_forming),
    }
    if reason:
        return Decision(
            symbol, profile.asset_class, engine.engine_name, "TOP_DOWN_NO_SETUP",
            h4.side if h4.side in SIDES else None, SetupState.REJECTED, reason, total,
            risk_fraction=profile.risk_fraction, reasons=[reason], evidence=evidence,
        )

    context_material = "|".join([
        symbol.upper(), h4.side, h4.bar_open_time.isoformat(),
        h1.bar_open_time.isoformat(), m15.bar_open_time.isoformat(),
    ])
    context_id = hashlib.sha256(context_material.encode()).hexdigest()[:20]
    setup_id = f"{symbol.upper()}:{h4.side}:{m5.bar_open_time.isoformat()}:{context_id}"
    stop_distance = max(_atr(frames["M5"]) * max(profile.stop_atr, 0.1), 1e-12)
    target_distance = stop_distance * max(profile.target_r, 1.0)
    context = {
        "context_id": context_id, "symbol": symbol, "asset_class": profile.asset_class,
        "side": h4.side, "created_at": h4.bar_close_time.isoformat(), "status": "ACTIVE",
        "h4_decision": _pack(h4), "h1_decision": _pack(h1), "m15_decision": _pack(m15),
    }
    evidence.update({
        "htf_context": context,
        "setup_id": setup_id,
        "execution_model": "M5_BREAK_OR_RETEST_IMMEDIATE",
    })
    return Decision(
        symbol, profile.asset_class, engine.engine_name,
        f"{profile.strategy_family or engine.engine_name}:{(m5.metrics or {}).get('trigger_type') or 'BREAK_OR_RETEST'}",
        h4.side, "PASS", "TOP_DOWN_CONTEXT_AND_M5_TRIGGER_PASS", total,
        stop_distance, target_distance, profile.target_r, profile.risk_fraction,
        ["H4_H1_M15_ALIGNED", f"{engine.m15_setup_name}_PASS", "M5_BREAK_OR_RETEST_PASS"],
        evidence,
    )


def evaluate_asset_h4(frame: Frame, asset_class: str = "forex", minimum: float = 70.0) -> TimeframeDecision:
    return _engine_for(asset_class).evaluate_h4(frame, minimum)


def evaluate_asset_h1(frame: Frame, expected_side: str, asset_class: str = "forex", minimum: float = 70.0) -> TimeframeDecision:
    return _engine_for(asset_class).evaluate_h1(frame, expected_side, minimum)


def evaluate_asset_m15(frame: Frame, expected_side: str, asset_class: str = "forex", minimum: float = 70.0) -> TimeframeDecision:
    return _engine_for(asset_class).m15(frame, expected_side, None, minimum)


def context(frame: Frame) -> Tuple[str, float]:
    result = evaluate_h4(frame)
    return result.side, result.score


def regime(frame: Frame) -> str:
    return "TREND" if _adx(frame) >= 18 else "RANGE"


def score_details_from_signal(signal: Optional[Dict[str, Any]], market: str = "") -> Dict[str, Any]:
    signal = signal if isinstance(signal, dict) else {}
    breakdown = signal.get("score_breakdown") if isinstance(signal.get("score_breakdown"), dict) else {}
    decision = signal.get("replacement_decision") if isinstance(signal.get("replacement_decision"), dict) else {}
    evidence = decision.get("evidence") if isinstance(decision.get("evidence"), dict) else {}
    metric = breakdown.get("score_metric") if isinstance(breakdown.get("score_metric"), dict) else evidence.get("score_metric")
    metric = metric if isinstance(metric, dict) else {
        "name": "Cipher FX Scoring Metric",
        "total": float(signal.get("score") or 0.0),
        "minimum": float(signal.get("min_score") or 0.0),
        "components": {},
        "enforced": True,
    }
    return {
        "name": "Cipher FX Scoring Metric",
        "engine": metric.get("engine") or signal.get("engine") or "",
        "total": float(metric.get("total", signal.get("score") or 0.0) or 0.0),
        "minimum": float(metric.get("minimum", signal.get("min_score") or 0.0) or 0.0),
        "components": metric.get("components") or {},
        "weights": metric.get("weights") or {},
        "enforced": bool(metric.get("enforced", True)),
        "timeframes": breakdown.get("timeframe_decisions") or evidence.get("higher_timeframes") or {},
        "m1": {"status": "NOT_REQUIRED", "role": "M5 trigger is execution event"},
        "reasons": signal.get("block_reason") or "",
        "market": market,
    }


def load_fixture(path: str) -> Dict[str, Any]:
    import json
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


__all__ = [
    "Candle", "Frame", "Profile", "TimeframeDecision", "HTFContext",
    "Decision", "SetupState", "TERMINAL_STATES", "evaluate", "evaluate_h4",
    "evaluate_h1", "evaluate_m15", "evaluate_m5_trigger", "evaluate_asset_h4",
    "evaluate_asset_h1", "evaluate_asset_m15", "score_details_from_signal",
    "context", "regime", "load_fixture",
]
