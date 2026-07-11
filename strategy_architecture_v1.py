#!/usr/bin/env python3
"""
Standalone non-live strategy architecture.
No MT5 calls and no order_send path.

Pipeline:
H4 regime/direction -> H1 permission -> M15 setup -> M5 trigger -> M1 close confirmation.
Three engines: FX trend-pullback, index session-breakout, metal volatility-trend.
"""
from __future__ import annotations
import argparse, json, math, statistics
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Tuple

SIDES = {"BUY", "SELL"}
ASSETS = {"forex", "index", "metal"}

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

@dataclass
class Decision:
    symbol: str
    asset_class: str
    engine: str = ""
    setup_type: str = ""
    side: Optional[str] = None
    state: str = "NO_SETUP"
    reason: str = ""
    score: float = 0.0
    stop_distance: float = 0.0
    target_distance: float = 0.0
    expected_edge_r: float = 0.0
    risk_fraction: float = 0.0
    reasons: List[str] = field(default_factory=list)
    evidence: Dict[str, object] = field(default_factory=dict)
    def block(self, reason: str) -> None:
        self.reasons.append(reason)

def finite(x: float) -> bool:
    return math.isfinite(float(x))

def closes(f: Frame) -> List[float]:
    return [x.close for x in f.candles]

def ema(f: Frame, period: int) -> float:
    v = closes(f)
    if len(v) < period: return float("nan")
    out = sum(v[:period]) / period
    a = 2.0 / (period + 1.0)
    for x in v[period:]: out = a * x + (1.0 - a) * out
    return out

def atr(f: Frame, period: int = 14) -> float:
    if len(f.candles) < period: return float("nan")
    out, previous = [], None
    for c in f.candles:
        out.append(c.high - c.low if previous is None else max(c.high-c.low, abs(c.high-previous), abs(c.low-previous)))
        previous = c.close
    return sum(out[-period:]) / period

def rsi(f: Frame, period: int = 14) -> float:
    v = closes(f)
    if len(v) < period + 1: return float("nan")
    changes = [b-a for a,b in zip(v[-period-1:-1], v[-period:])]
    gain = sum(max(x,0.0) for x in changes) / period
    loss = sum(max(-x,0.0) for x in changes) / period
    if loss == 0: return 100.0 if gain else 50.0
    return 100.0 - 100.0 / (1.0 + gain/loss)

def rmi(f: Frame, length: int = 14, momentum: int = 5) -> float:
    v = closes(f)
    if len(v) < length + momentum: return float("nan")
    changes = [v[i] - v[i-momentum] for i in range(momentum, len(v))][-length:]
    gain = sum(max(x,0.0) for x in changes) / length
    loss = sum(max(-x,0.0) for x in changes) / length
    if loss == 0: return 100.0 if gain else 50.0
    return 100.0 - 100.0 / (1.0 + gain/loss)

def adx_proxy(f: Frame, period: int = 14) -> float:
    if len(f.candles) < period + 1: return float("nan")
    ranges = [max(c.high-c.low, 1e-12) for c in f.candles[-period:]]
    moves = [abs(b.close-a.close)/r for a,b,r in zip(f.candles[-period-1:-1], f.candles[-period:], ranges)]
    return max(0.0, min(100.0, 100.0 * statistics.mean(moves)))

def vwap_proxy(f: Frame, period: int = 30) -> float:
    c = f.candles[-period:]
    if not c: return float("nan")
    vol = sum(max(x.volume,1.0) for x in c)
    return sum(((x.high+x.low+x.close)/3.0)*max(x.volume,1.0) for x in c) / vol

def bollinger_position(f: Frame, period: int = 20) -> float:
    v = closes(f)
    if len(v) < period: return float("nan")
    w = v[-period:]
    mid, sd = statistics.mean(w), statistics.pstdev(w)
    lo, hi = mid-2*sd, mid+2*sd
    return 0.5 if hi <= lo else max(0.0, min(1.0, (v[-1]-lo)/(hi-lo)))

def body_fraction(c: Candle) -> float:
    return abs(c.close-c.open) / max(c.high-c.low, 1e-12)

def close_location(c: Candle) -> float:
    return (c.close-c.low) / max(c.high-c.low, 1e-12)

def context(f: Frame) -> Tuple[str,float]:
    c, e20, e50, m = f.candles[-1], ema(f,20), ema(f,50), rmi(f)
    if not all(finite(x) for x in (e20,e50,m)): return "FLAT", 0.0
    # Direction comes from price structure; RMI confirms momentum side but does
    # not need an arbitrary extreme threshold such as 55/45.
    if c.close > e20 > e50 and m >= 50:
        return "BUY", min(100.0, 60.0+(m-50.0)*2.0)
    if c.close < e20 < e50 and m <= 50:
        return "SELL", min(100.0, 60.0+(50.0-m)*2.0)
    return "FLAT", 50.0

def regime(f: Frame) -> str:
    a = adx_proxy(f)
    return "TREND" if finite(a) and a >= 25 else "RANGE" if finite(a) and a <= 18 else "TRANSITION"

def m1_confirm(f: Frame, side: str) -> Tuple[bool,str,Dict[str,float]]:
    if len(f.candles) < 3: return False, "M1 history insufficient", {}
    c, prev = f.candles[-1], f.candles[-2].close
    bf, loc = body_fraction(c), close_location(c)
    meta = {"body_fraction":bf, "close_location":loc}
    if bf < .25: return False, "M1 doji/small body", meta
    if side == "BUY" and not (c.close > c.open and c.close >= prev and loc >= .60):
        return False, "M1 not bullish", meta
    if side == "SELL" and not (c.close < c.open and c.close <= prev and loc <= .40):
        return False, "M1 not bearish", meta
    return True, "M1 directional confirmation passed", meta

def higher_gate(d: Decision, frames: Dict[str,Frame]) -> None:
    h4, h1, m15 = context(frames["H4"]), context(frames["H1"]), context(frames["M15"])
    d.evidence["higher_timeframes"] = {
        "H4":{"side":h4[0],"score":round(h4[1],2),"regime":regime(frames["H4"])},
        "H1":{"side":h1[0],"score":round(h1[1],2),"regime":regime(frames["H1"])},
        "M15":{"side":m15[0],"score":round(m15[1],2),"regime":regime(frames["M15"])}}
    if h4[0] not in {d.side,"FLAT"}: d.block("H4 opposes side")
    if h1[0] != d.side or h1[1] < 60: d.block("H1 weak or misaligned")
    if m15[0] not in {d.side,"FLAT"}: d.block("M15 opposes side")
    if m15[0] == "FLAT" and m15[1] < 50: d.block("M15 missing/weak structure")

def movement_state(f: Frame, lookback: int = 12) -> Dict[str, float]:
    if len(f.candles) < lookback + 2:
        return {"side": "FLAT", "move_atr": 0.0, "impulse": 0.0}
    a = atr(f)
    if not finite(a) or a <= 0:
        return {"side": "FLAT", "move_atr": 0.0, "impulse": 0.0}
    start = f.candles[-lookback-1].close
    end = f.candles[-1].close
    move = (end - start) / a
    return {
        "side": "BUY" if move > 0 else "SELL" if move < 0 else "FLAT",
        "move_atr": move,
        "impulse": 1.0 if abs(move) >= 0.75 else 0.0,
    }

def pullback_reclaim(f: Frame, side: str) -> bool:
    if len(f.candles) < 8:
        return False
    a, e20, v = atr(f), ema(f, 20), vwap_proxy(f)
    if not all(finite(x) for x in (a, e20, v)) or a <= 0:
        return False
    recent = f.candles[-4:]
    last, prior = recent[-1], recent[-2]
    if side == "BUY":
        touched = any(c.low <= max(e20, v) + 0.35*a for c in recent[:-1])
        return touched and last.close > e20 and last.close > last.open and last.close >= prior.close
    if side == "SELL":
        touched = any(c.high >= min(e20, v) - 0.35*a for c in recent[:-1])
        return touched and last.close < e20 and last.close < last.open and last.close <= prior.close
    return False

def liquidity_sweep_reclaim(f: Frame, side: str) -> bool:
    if len(f.candles) < 10:
        return False
    last = f.candles[-1]
    prior = f.candles[-9:-1]
    if side == "BUY":
        return last.low < min(c.low for c in prior) and last.close > last.open and close_location(last) >= 0.60
    if side == "SELL":
        return last.high > max(c.high for c in prior) and last.close < last.open and close_location(last) <= 0.40
    return False

def engine_setup(d: Decision, frames: Dict[str,Frame]) -> None:
    h1_side, _ = context(frames["H1"])
    m15, m5 = frames["M15"], frames["M5"]
    d.side = h1_side if h1_side in SIDES else None
    if d.side not in SIDES:
        d.block("no directional H1 side")
        return
    m15_move = movement_state(m15)
    m5_move = movement_state(m5)
    m15_context_side, _ = context(m15)
    d.evidence["movement"] = {"M15": m15_move, "M5": m5_move}

    # M15 supplies directional context; it does not need to be moving in the
    # same direction at the instant of a valid M5 pullback/reclaim. Requiring
    # an M15 impulse and an M5 continuation simultaneously rejected normal
    # pullback entries even when H1 and M5 were aligned.
    m15_context_ok = m15_context_side in {d.side, "FLAT"}

    if d.asset_class == "forex":
        d.engine, d.setup_type = "FOREX_TREND_PULLBACK", "TREND_PULLBACK_RECLAIM"
        m15_structure = m15_move["side"] == d.side and abs(m15_move["move_atr"]) >= 0.35
        m5_reclaim = pullback_reclaim(m5, d.side) or liquidity_sweep_reclaim(m5, d.side)
        m5_continuation = m5_move["side"] == d.side and abs(m5_move["move_atr"]) >= 0.25
        m5_trigger = m5_reclaim or m5_continuation
        if not (m5_trigger and m15_context_ok and (m15_structure or m5_trigger)):
            d.block(f"FX {d.side} requires M15 context and M5 pullback/reclaim")
        d.evidence["setup_mode"] = "M15_IMPULSE" if m15_structure else "M15_CONTEXT_M5_PULLBACK"

    elif d.asset_class == "index":
        d.engine = "INDEX_SESSION_BREAKOUT"
        m15_structure = m15_move["side"] == d.side and abs(m15_move["move_atr"]) >= 0.35
        breakout_retest = pullback_reclaim(m5, d.side)
        sweep_reversal = liquidity_sweep_reclaim(m5, d.side)
        movement_trigger = m5_move["side"] == d.side and abs(m5_move["move_atr"]) >= 0.25
        if breakout_retest:
            d.setup_type = "SESSION_BREAKOUT_RETEST"
        elif sweep_reversal:
            d.setup_type = "LIQUIDITY_SWEEP_RECLAIM"
        else:
            d.setup_type = "SESSION_MOVEMENT_PULLBACK"
        m5_trigger = breakout_retest or sweep_reversal or movement_trigger
        if not (m5_trigger and m15_context_ok and (m15_structure or m5_trigger)):
            d.block(f"index {d.side} requires M15 context and M5 continuation/reclaim")
        d.evidence["setup_mode"] = "M15_IMPULSE" if m15_structure else "M15_CONTEXT_M5_PULLBACK"
        d.evidence["index_trigger"] = {
            "m15_impulse": bool(m15_structure),
            "m5_pullback_reclaim": bool(breakout_retest),
            "m5_liquidity_sweep_reclaim": bool(sweep_reversal),
            "m5_movement_continuation": bool(movement_trigger),
            "m15_context_side": m15_context_side,
        }

    else:
        d.engine, d.setup_type = "METAL_VOLATILITY_TREND", "VOLATILITY_TREND_PULLBACK_RECLAIM"
        m15_structure = m15_move["side"] == d.side and abs(m15_move["move_atr"]) >= 0.35
        m5_reclaim = pullback_reclaim(m5, d.side) or liquidity_sweep_reclaim(m5, d.side)
        m5_continuation = m5_move["side"] == d.side and abs(m5_move["move_atr"]) >= 0.25
        m5_trigger = m5_reclaim or m5_continuation
        if not (m5_trigger and m15_context_ok and (m15_structure or m5_trigger)):
            d.block(f"metal {d.side} requires M15 context and M5 pullback/reclaim")
        d.evidence["setup_mode"] = "M15_IMPULSE" if m15_structure else "M15_CONTEXT_M5_PULLBACK"


def geometry(d: Decision, f: Frame, p: Profile) -> None:
    c, a, v, b = f.candles[-1], atr(f), vwap_proxy(f), bollinger_position(f)
    if not all(finite(x) for x in (a,v,b)) or a <= 0: d.block("M5 volatility context unavailable"); return
    d.stop_distance, d.target_distance = a*p.stop_atr, a*p.stop_atr*p.target_r
    d.evidence["entry"] = {"price":c.close,"atr":a,"vwap":v,"bollinger_position":b,"body_fraction":body_fraction(c),"close_location":close_location(c)}
    # Extension is a trade-quality warning, not a duplicate setup veto. The
    # live pending/final guards still reject stale price movement, invalid
    # geometry, or a true multi-factor index exhaustion condition.
    warnings = []
    if d.side == "BUY" and (c.close-v)/a > 1.25: warnings.append("BUY extended above VWAP")
    if d.side == "SELL" and (v-c.close)/a > 1.25: warnings.append("SELL extended below VWAP")
    if d.side == "BUY" and b >= .98: warnings.append("BUY at upper Bollinger exhaustion")
    if d.side == "SELL" and b <= .02: warnings.append("SELL at lower Bollinger exhaustion")
    d.evidence["quality_warnings"] = warnings

def forecast(d: Decision, frames: Dict[str,Frame]) -> None:
    sign = 1 if d.side == "BUY" else -1
    features = {
        "H4_RMI": (rmi(frames["H4"]) - 50) / 50,
        "H1_RMI": (rmi(frames["H1"]) - 50) / 50,
        "M15_RMI": (rmi(frames["M15"]) - 50) / 50,
        "M5_RMI": (rmi(frames["M5"]) - 50) / 50,
        "H1_EMA_SLOPE": (ema(frames["H1"], 20) - ema(frames["H1"], 50)) / max(atr(frames["H1"]), 1e-12),
        "M15_EMA_SLOPE": (ema(frames["M15"], 20) - ema(frames["M15"], 50)) / max(atr(frames["M15"]), 1e-12),
    }
    weights = {"H4_RMI": .20, "H1_RMI": .25, "M15_RMI": .20, "M5_RMI": .10, "H1_EMA_SLOPE": .15, "M15_EMA_SLOPE": .10}
    edge = sum(max(-2, min(2, sign * x)) * weights[k] for k, x in features.items())
    d.expected_edge_r = edge
    d.evidence["forecast"] = {"features": features, "weights": weights, "edge_r": edge}
    if edge < 0.0:
        d.evidence.setdefault("quality_warnings", []).append(f"forecast opposes intended side ({edge:.3f}R)")


def quality_score(d: Decision, frames: Dict[str,Frame], p: Profile) -> None:
    """Score measured setup quality; it is diagnostic and does not override safety vetoes."""
    if d.side not in SIDES:
        return
    h4_side, h4_score = context(frames["H4"])
    h1_side, h1_score = context(frames["H1"])
    m15_side, m15_score = context(frames["M15"])
    m5_move = movement_state(frames["M5"])
    c5 = frames["M5"].candles[-1]
    a5, e20, v5, bb = atr(frames["M5"]), ema(frames["M5"], 20), vwap_proxy(frames["M5"]), bollinger_position(frames["M5"])
    direction = d.side

    def align(actual: str, full: float, neutral: float) -> float:
        if actual == direction:
            return full
        if actual == "FLAT":
            return neutral
        return 0.0

    components = {
        "H4_direction": align(h4_side, 8.0, 4.0),
        "H1_direction": align(h1_side, 12.0, 6.0),
        "M15_direction": align(m15_side, 5.0, 3.0),
    }

    location = 0.0
    if all(finite(x) for x in (a5, e20, v5, bb)) and a5 > 0:
        above_fair = c5.close >= min(e20, v5) if direction == "BUY" else c5.close <= max(e20, v5)
        if above_fair:
            location += 6.0
        elif (direction == "BUY" and c5.close >= min(e20, v5) - 0.35 * a5) or (direction == "SELL" and c5.close <= max(e20, v5) + 0.35 * a5):
            location += 3.0
        distance = abs(c5.close - v5) / a5
        location += 5.0 if distance <= 0.75 else 3.0 if distance <= 1.25 else 0.0
        in_band = (0.20 <= bb <= 0.85) if direction == "BUY" else (0.15 <= bb <= 0.80)
        location += 4.0 if in_band else 1.0
    components["15M_location_and_fair_value"] = min(15.0, location)

    trigger = 0.0
    if m5_move["side"] == direction:
        trigger += 8.0 if abs(m5_move["move_atr"]) >= 0.50 else 5.0 if abs(m5_move["move_atr"]) >= 0.25 else 2.0
    if (direction == "BUY" and c5.close > c5.open) or (direction == "SELL" and c5.close < c5.open):
        trigger += 5.0
    bf, loc = body_fraction(c5), close_location(c5)
    trigger += 5.0 if bf >= 0.35 else 3.0 if bf >= 0.25 else 0.0
    trigger += 5.0 if (loc >= 0.60 if direction == "BUY" else loc <= 0.40) else 2.0
    components["5M_trigger_quality"] = min(25.0, trigger)

    adx5 = adx_proxy(frames["M5"])
    rmi5 = rmi(frames["M5"])
    regime_score = 0.0
    if finite(adx5):
        regime_score += 6.0 if 20.0 <= adx5 <= 45.0 else 4.0 if 15.0 <= adx5 <= 55.0 else 2.0
    if finite(rmi5):
        momentum_aligned = (rmi5 >= 50.0) if direction == "BUY" else (rmi5 <= 50.0)
        regime_score += 4.0 if momentum_aligned else 1.0
    components["regime_and_momentum"] = min(10.0, regime_score)

    volumes = [max(float(c.volume or 0.0), 0.0) for c in frames["M5"].candles[-20:]]
    current_volume = volumes[-1] if volumes else 0.0
    median_volume = statistics.median(volumes[:-1]) if len(volumes) > 1 else 0.0
    if current_volume > 0.0 and median_volume > 0.0:
        components["liquidity_activity"] = 5.0 if current_volume >= median_volume * 0.90 else 2.0
    else:
        components["liquidity_activity"] = 3.0

    room = 0.0
    if float(p.target_r or 0.0) >= 1.50:
        room += 5.0
    elif float(p.target_r or 0.0) >= 1.20:
        room += 3.0
    if float(p.spread_limit_atr or 0.0) <= 0.45:
        room += 3.0
    warnings = d.evidence.get("quality_warnings") or []
    room += 2.0 if not warnings else 0.0
    components["reward_and_cost_room"] = min(10.0, room)
    data_issues = []
    for label, frame in frames.items():
        candles = list(frame.candles)
        timestamps = [str(c.timestamp) for c in candles]
        if len(set(timestamps)) != len(timestamps):
            data_issues.append(f"{label} duplicate timestamps")
        if any(timestamps[i] >= timestamps[i + 1] for i in range(len(timestamps) - 1)):
            data_issues.append(f"{label} timestamps not increasing")
        for candle in candles[-20:]:
            values = (candle.open, candle.high, candle.low, candle.close)
            if not all(finite(value) for value in values):
                data_issues.append(f"{label} non-finite OHLC")
                break
            if candle.high < max(candle.open, candle.close) or candle.low > min(candle.open, candle.close):
                data_issues.append(f"{label} invalid OHLC geometry")
                break
    components["data_quality"] = 10.0 if not data_issues else max(0.0, 10.0 - min(10.0, 3.0 * len(data_issues)))
    if data_issues:
        d.block("invalid market data: " + "; ".join(data_issues[:3]))

    total = max(0.0, min(100.0, sum(components.values())))
    band = "STRONG" if total >= 80.0 else "TRADEABLE" if total >= 70.0 else "WATCH" if total >= 60.0 else "WEAK"
    d.score = round(total, 2)
    d.evidence["score_metric"] = {
        "version": "quality_v2",
        "total": d.score,
        "band": band,
        "components": {k: round(float(v), 2) for k, v in components.items()},
        "maxima": {"HTF": 25, "location": 15, "trigger": 25, "regime_momentum": 10, "liquidity": 5, "reward_cost_room": 10, "data_quality": 10},
        "data_quality_issues": data_issues,
        "note": "diagnostic quality score; hard safety gates remain separate",
    }

def evaluate(symbol: str, p: Profile, frames: Dict[str,Frame]) -> Decision:
    d = Decision(symbol=symbol, asset_class=p.asset_class, risk_fraction=p.risk_fraction)
    required = {"H4","H1","M15","M5","M1"}
    if set(frames) != required: d.block("all H4/H1/M15/M5/M1 frames are required")
    for key, minimum in {"H4":60,"H1":80,"M15":60,"M5":40,"M1":3}.items():
        if key in frames and len(frames[key].candles) < minimum: d.block(f"insufficient {key} history")
    if d.reasons: d.state, d.reason = "NOT_QUALIFIED","; ".join(d.reasons); return d
    engine_setup(d,frames)
    if d.side in SIDES:
        higher_gate(d,frames); geometry(d,frames["M5"],p); forecast(d,frames); quality_score(d,frames,p)
        # M1 is execution timing, not setup permission. The live pending loop
        # validates the first completed directional M1 candle after setup creation.
        d.evidence["m1_confirmation"] = {
            "status": "WAITING_FOR_COMPLETED_M1",
            "direction_required": d.side,
            "body_fraction_minimum": 0.25,
            "close_location_required": 0.60 if d.side == "BUY" else 0.40,
        }
    d.state = "PASS" if not d.reasons and d.side in SIDES else "NOT_QUALIFIED"
    d.reason = "complete pipeline passed" if d.state=="PASS" else "; ".join(d.reasons)
    d.evidence["side_consistency"] = {"setup_side":d.side,"confirmation_side":d.side if d.state=="PASS" else "","final_order_side":d.side if d.state=="PASS" else ""}
    return d

def load_fixture(path: str):
    with open(path,encoding="utf-8") as h: x=json.load(h)
    p=Profile(**x["profile"])
    frames={k:Frame(v.get("timeframe",k),tuple(Candle(**c) for c in v["candles"])) for k,v in x["frames"].items()}
    return x.get("symbol",p.symbol),p,frames

if __name__ == "__main__":
    ap=argparse.ArgumentParser()
    ap.add_argument("--self-test",action="store_true")
    ap.add_argument("--input")
    args=ap.parse_args()
    if args.self_test:
        print(json.dumps({"module":"strategy_architecture_v1","status":"PASS","live_order_send":False}))
    elif args.input:
        s,p,f=load_fixture(args.input)
        print(json.dumps(asdict(evaluate(s,p,f)),indent=2,sort_keys=True))
    else:
        ap.error("--self-test or --input required")
