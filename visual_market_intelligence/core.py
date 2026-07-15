"""Causal, deterministic visual observer. It cannot place or alter trades."""
from __future__ import annotations
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import statistics
import time
from typing import Any, Iterable, Mapping, Sequence
from .storage import insert_prediction

MODULE_VERSION = "visual_market_intelligence_v1"
MODEL_VERSION = "shadow_heuristic_v1"
FEATURE_VERSION = "causal_features_v1"
MODE = "SHADOW_ONLY"

@dataclass(frozen=True)
class Candle:
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    spread: float | None = None

@dataclass(frozen=True)
class VisualPrediction:
    prediction_id: str
    created_at: str
    symbol: str
    session: str
    setup_id: str | None
    trade_id: str | None
    mode: str
    module_version: str
    model_version: str
    feature_version: str
    h1_features: Mapping[str, Any]
    m15_features: Mapping[str, Any]
    m5_features: Mapping[str, Any]
    m1_features: Mapping[str, Any]
    numeric_features: Mapping[str, Any]
    cost_context: Mapping[str, Any]
    engine_context: Mapping[str, Any]
    probabilities: Mapping[str, float]
    confidence: float
    uncertainty: float
    out_of_distribution: float
    abstain: bool
    reasons: Sequence[str]
    input_hash: str
    latency_ms: float

def _num(value: Any, default: float = 0.0) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default

def _get(row: Any, name: str, default: Any = None) -> Any:
    return row.get(name, default) if isinstance(row, Mapping) else getattr(row, name, default)

def to_candles(rows: Iterable[Any]) -> list[Candle]:
    out = []
    for row in rows or []:
        timestamp = _get(row, "timestamp", _get(row, "time", _get(row, "open_time", "")))
        if hasattr(timestamp, "isoformat"):
            timestamp = timestamp.isoformat()
        o, h, l, c = (_num(_get(row, key)) for key in ("open", "high", "low", "close"))
        if h < l or h <= 0 or l <= 0:
            continue
        spread = _get(row, "spread")
        out.append(Candle(str(timestamp), o, h, l, c,
                          _num(_get(row, "volume", _get(row, "tick_volume", 0))),
                          _num(spread) if spread is not None else None))
    return out

def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))

def _mean(values: Sequence[float], default: float = 0.0) -> float:
    return statistics.fmean(values) if values else default

def _ema(values: Sequence[float], period: int) -> float:
    if not values:
        return 0.0
    alpha = 2.0 / (period + 1)
    value = float(values[0])
    for item in values[1:]:
        value = alpha * float(item) + (1 - alpha) * value
    return value

def _atr(candles: Sequence[Candle], period: int = 14) -> float:
    if not candles:
        return 0.0
    tr, previous = [], None
    for candle in candles[-max(period * 3, period):]:
        prior = candle.close if previous is None else previous
        tr.append(max(candle.high - candle.low, abs(candle.high - prior), abs(candle.low - prior)))
        previous = candle.close
    return _mean(tr[-period:])

def _std(values: Sequence[float]) -> float:
    return statistics.pstdev(values) if len(values) > 1 else 0.0

def _session(timestamp: str) -> str:
    try:
        hour = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).hour
    except (TypeError, ValueError):
        return "unknown"
    if hour < 8: return "Asia"
    if hour < 13: return "London"
    if hour < 17: return "overlap"
    if hour < 22: return "New York"
    return "unknown"

def causal_frame_features(frame: Iterable[Any], timeframe: str = "") -> dict[str, Any]:
    """Features use only the supplied completed bars; no future rows are consulted."""
    candles = to_candles(frame)
    if not candles:
        return {"timeframe": timeframe, "available": False, "bars": 0,
                "direction": "UNKNOWN", "regime_probs": {"disorder": 1.0}}
    closes = [c.close for c in candles]
    current = candles[-1]
    atr = _atr(candles)
    ema20, ema50 = _ema(closes, 20), _ema(closes, 50)
    typical = [(c.high + c.low + c.close) / 3 for c in candles]
    volume = sum(c.volume for c in candles)
    vwap = sum(p * c.volume for p, c in zip(typical, candles)) / volume if volume else _mean(typical)
    rng = max(current.high - current.low, 1e-12)
    body = abs(current.close - current.open)
    body_fraction = body / rng
    close_location = (current.close - current.low) / rng
    ranges = [c.high - c.low for c in candles[-20:]]
    bodies = [abs(c.close - c.open) for c in candles[-20:]]
    average_range, average_body = _mean(ranges[:-1], rng), _mean(bodies[:-1], body)
    start = max(0, len(candles) - 20)
    denominator = sum(abs(candles[i].close - candles[i-1].close) for i in range(max(1, start), len(candles)))
    efficiency = abs(current.close - candles[start].close) / max(denominator, 1e-12)
    bandwidth = 2 * _std(closes[-20:]) / max(abs(_mean(closes[-20:])), 1e-12)
    prior = candles[-min(21, len(candles)):-1]
    prior_high = max((c.high for c in prior), default=current.high)
    prior_low = min((c.low for c in prior), default=current.low)
    direction = "BULLISH" if current.close > current.open else "BEARISH" if current.close < current.open else "FLAT"
    move = (current.close - candles[max(0, len(candles)-6)].close) / max(atr, 1e-12)
    strength = _clamp(abs(ema20 - ema50) / max(atr, 1e-12) / 2)
    bull = strength * (1 if ema20 >= ema50 else .35) * (.6 + .4 * _clamp(.5 + move / 4))
    bear = strength * (1 if ema20 <= ema50 else .35) * (.6 + .4 * _clamp(.5 - move / 4))
    expansion = _clamp(rng / max(average_range, 1e-12) - 1)
    compression = _clamp(1 - bandwidth / .02)
    disorder = _clamp((1 - efficiency) * .65 + (1 - strength) * .35)
    range_prob = _clamp((1 - strength) * .65 + (1 - efficiency) * .35)
    transition = _clamp((1 - abs(bull - bear)) * .5 + expansion * .5)
    return {
        "timeframe": timeframe, "available": len(candles) >= 2, "bars": len(candles),
        "last_completed_at": current.timestamp, "open": current.open, "high": current.high,
        "low": current.low, "close": current.close, "direction": direction, "atr": atr,
        "ema20": ema20, "ema50": ema50, "vwap": vwap, "body": body,
        "body_fraction": body_fraction, "close_location": close_location,
        "upper_wick": current.high - max(current.open, current.close),
        "lower_wick": min(current.open, current.close) - current.low,
        "average_range": average_range, "average_body": average_body,
        "atr_move_5": move, "efficiency_ratio": efficiency, "bandwidth": bandwidth,
        "expansion_ratio": rng / max(average_range, 1e-12),
        "distance_vwap_atr": (current.close - vwap) / max(atr, 1e-12),
        "distance_ema20_atr": (current.close - ema20) / max(atr, 1e-12),
        "prior_high": prior_high, "prior_low": prior_low,
        "break_of_structure_up": current.close > prior_high,
        "break_of_structure_down": current.close < prior_low,
        "pullback_bullish": current.low <= ema20 and current.close > ema20,
        "pullback_bearish": current.high >= ema20 and current.close < ema20,
        "failed_break_up": current.high > prior_high and current.close < prior_high,
        "failed_break_down": current.low < prior_low and current.close > prior_low,
        "regime_probs": {
            "bullish_trend": _clamp(bull), "bearish_trend": _clamp(bear),
            "range": range_prob, "compression": compression,
            "volatility_expansion": expansion, "disorder": disorder,
            "transition": transition,
        },
        "return_volatility": _std([
            (candles[i].close - candles[i-1].close) / max(abs(candles[i-1].close), 1e-12)
            for i in range(max(1, len(candles)-20), len(candles))
        ]),
    }

def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return 0.0
    return value

class VisualMarketIntelligence:
    """Shadow observer. It returns evidence only and has no execution interface."""
    def __init__(self, db_path: str):
        self.db_path = db_path
        from .storage import ensure_visual_tables
        ensure_visual_tables(db_path)

    def record_outcome(self, *, prediction_id: str, outcome_label: str,
                       realized_r: float | None = None, costs_paid: float | None = None,
                       horizon_seconds: int = 0, direction: str = "",
                       future_window_end: str | None = None,
                       evaluated_at: str | None = None) -> dict[str, Any]:
        from .storage import insert_outcome
        insert_outcome(
            self.db_path,
            prediction_id=prediction_id,
            outcome_label=outcome_label,
            realized_r=realized_r,
            costs_paid=costs_paid,
            horizon_seconds=horizon_seconds,
            direction=direction,
            future_window_end=future_window_end,
            evaluated_at=evaluated_at,
        )
        return {
            "status": "RECORDED",
            "mode": MODE,
            "prediction_id": str(prediction_id),
            "outcome_label": str(outcome_label),
        }

    def record_drift(self, *, symbol: str, metric: str, value: float | None,
                     threshold: float | None, status: str,
                     details: Mapping[str, Any] | None = None) -> dict[str, Any]:
        from .storage import insert_drift
        insert_drift(
            self.db_path,
            symbol=symbol,
            metric=metric,
            value=value,
            threshold=threshold,
            status=status,
            details=details,
        )
        return {
            "status": "RECORDED",
            "mode": MODE,
            "symbol": str(symbol),
            "metric": str(metric),
            "drift_status": str(status),
        }

    def evaluate(self, *, symbol: str, timestamp: str,
                 h1_context: Iterable[Any], m15_context: Iterable[Any],
                 m5_context: Iterable[Any], m1_context: Iterable[Any],
                 numeric_features: Mapping[str, Any] | None = None,
                 cost_context: Mapping[str, Any] | None = None,
                 engine_context: Mapping[str, Any] | None = None,
                 setup_id: str | None = None, trade_id: str | None = None) -> dict[str, Any]:
        started = time.perf_counter()
        frames = {
            "H1": causal_frame_features(h1_context, "H1"),
            "M15": causal_frame_features(m15_context, "M15"),
            "M5": causal_frame_features(m5_context, "M5"),
            "M1": causal_frame_features(m1_context, "M1"),
        }
        required = tuple(frames)
        missing = [name for name in required if not frames[name]["available"]]
        reasons = ["DATA_MISSING_" + "_".join(missing) + "_CONTEXT"] if missing else []
        long_votes = [float(frames[name]["direction"] == "BULLISH") for name in required]
        short_votes = [float(frames[name]["direction"] == "BEARISH") for name in required]
        long_score, short_score = _mean(long_votes), _mean(short_votes)
        direction = "LONG" if long_score > short_score else "SHORT" if short_score > long_score else "NEUTRAL"
        confidence = _clamp(abs(long_score - short_score) + max(long_score, short_score) * .35)
        uncertainty = _clamp(1 - confidence)
        regime_keys = ("bullish_trend", "bearish_trend", "range", "compression",
                       "volatility_expansion", "disorder", "transition")
        regime = {key: _mean([frames[n]["regime_probs"].get(key, 0) for n in required])
                  for key in regime_keys}
        max_expansion = max((frames[n].get("expansion_ratio", 0) for n in required), default=0)
        ood = _clamp(.5 * bool(missing) + .25 * regime["disorder"] + .25 * (max_expansion > 3))
        if confidence < .45: reasons.append("LOW_DIRECTIONAL_CONFIDENCE")
        if ood >= .65: reasons.append("OUT_OF_DISTRIBUTION")
        abstain = bool(missing or confidence < .45 or ood >= .65)
        if abstain: reasons.append("SHADOW_ABSTAIN")
        payload = {"symbol": symbol, "timestamp": timestamp, "setup_id": setup_id,
                   "trade_id": trade_id, "frames": frames,
                   "numeric_features": dict(numeric_features or {}),
                   "cost_context": dict(cost_context or {}),
                   "engine_context": dict(engine_context or {})}
        input_hash = hashlib.sha256(json.dumps(_jsonable(payload), sort_keys=True,
                                               separators=(",", ":")).encode()).hexdigest()
        prediction = VisualPrediction(
            prediction_id="vmi_" + input_hash[:24],
            created_at=datetime.now(timezone.utc).isoformat(), symbol=symbol,
            session=_session(timestamp), setup_id=setup_id, trade_id=trade_id,
            mode=MODE, module_version=MODULE_VERSION, model_version=MODEL_VERSION,
            feature_version=FEATURE_VERSION, h1_features=frames["H1"],
            m15_features=frames["M15"], m5_features=frames["M5"], m1_features=frames["M1"],
            numeric_features=dict(numeric_features or {}), cost_context=dict(cost_context or {}),
            engine_context=dict(engine_context or {}),
            probabilities={
                "long": long_score, "short": short_score,
                "neutral": _clamp(1 - max(long_score, short_score)),
                **{key: regime[key] for key in regime_keys},
            },
            confidence=confidence, uncertainty=uncertainty, out_of_distribution=ood,
            abstain=abstain, reasons=reasons, input_hash=input_hash,
            latency_ms=(time.perf_counter() - started) * 1000,
        )
        insert_prediction(self.db_path, asdict(prediction))
        return {
            "prediction_id": prediction.prediction_id, "created_at": prediction.created_at,
            "symbol": symbol, "session": prediction.session, "mode": MODE,
            "model_version": MODEL_VERSION, "feature_version": FEATURE_VERSION,
            "direction": direction, "probabilities": dict(prediction.probabilities),
            "confidence": confidence, "uncertainty": uncertainty,
            "out_of_distribution": ood, "abstain": abstain, "reasons": list(reasons),
            "input_hash": input_hash, "latency_ms": prediction.latency_ms, "features": frames,
        }
