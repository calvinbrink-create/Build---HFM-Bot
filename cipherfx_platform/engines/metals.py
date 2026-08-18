from __future__ import annotations
import os as _os
import os
from math import isfinite
from pathlib import Path
from typing import Any
from ..contracts import Candle, MarketSnapshot
from ..market_regime import classify_market

FRAME_ORDER = ("H4", "M15", "M5")
ENGINE = "METALS_ENGINE"
STRATEGY = "METALS_CHART_LIQUIDITY_GRAB_IMPULSE"
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
    c=_candles(frame)
    if len(c)<2: return 0.0
    tr=[max(x.high-x.low,abs(x.high-(c[i-1].close if i else x.open)),abs(x.low-(c[i-1].close if i else x.open))) for i,x in enumerate(c)]
    return sum(tr[-period:])/min(period,len(tr))

def _direction(frame, lookback: int = 20) -> str:
    """Direction over a RECENT window, not the whole frame.

    Was comparing c[-1] against c[0] - the OLDEST bar in a 220-bar frame,
    which on H4 is 37 days back. That made every index read "BUY" simply
    because price sat above its level from a month earlier, and the engine
    then forced BUY into intraday downtrends. Measured 2026-08-17 on the
    live ledger: 221 of 650 trades fought both H4 and H1 trend, losing
    110,614 ZAR while trend-aligned trades made +63,809.

    forex.py already used a recent window (c[-8]); indices/metals did not.
    20 bars matches the lookback classify_market() uses for the same frame.
    """
    c=_candles(frame)
    if len(c)<8: return "NONE"
    w=c[-(min(lookback,len(c)-1)+1):]
    if w[-1].close>w[0].close and w[-1].low>=w[0].low: return "BUY"
    if w[-1].close<w[0].close and w[-1].high<=w[0].high: return "SELL"
    return "MIXED"

def _recent_direction(frame, lookback: int) -> str | None:
    """Simple close-vs-N-bars-ago direction on a recent window.

    Deliberately NOT the strict high/low monotonic test used by
    _direction() - that returns MIXED too often and was the source of
    the fail-open hole. Returns None when there is not enough data.
    """
    c=_candles(frame)
    if len(c)<lookback+1: return None
    return "BUY" if c[-1].close>c[-1-lookback].close else "SELL"


def _chart_setup(frame, tick=None, expected_side: str | None = None) -> dict[str,Any]:
    c=_candles(frame)
    if len(c)<20: return {"valid":False,"side":"NONE","type":"NONE","liquidity_grab":False,"impulse":False}
    cur,ref,recent=c[-1],c[-15:-5],c[-5:-1]
    hi,lo=max(x.high for x in ref),min(x.low for x in ref)
    live_ask = float(getattr(tick, "ask", 0.0) or 0.0) if tick is not None else 0.0
    live_bid = float(getattr(tick, "bid", 0.0) or 0.0) if tick is not None else 0.0
    live_age = _live_bar_age_seconds(c, tick)
    if live_age is not None:
        _trig=_atr(frame)*float(_os.getenv("MT5_TRIGGER_ATR","0.10"))
        if live_age <= 120.0 and live_ask > cur.close+_trig and expected_side in ("BUY","ANY"):
            return {"valid":True,"side":"BUY","type":"LIVE_STRUCTURE_BREAK","liquidity_grab":False,"impulse":False,"level":cur.close,"reference_level":hi,"candle_color":"GREEN","trigger_source":"LIVE_TICK","trigger_price":live_ask,"trigger_age_seconds":live_age,"forming_open":cur.close,"forming_direction":"BUY"}
        if live_age <= 120.0 and live_bid < cur.close-_trig and expected_side in ("SELL","ANY"):
            return {"valid":True,"side":"SELL","type":"LIVE_STRUCTURE_BREAK","liquidity_grab":False,"impulse":False,"level":cur.close,"reference_level":lo,"candle_color":"RED","trigger_source":"LIVE_TICK","trigger_price":live_bid,"trigger_age_seconds":live_age,"forming_open":cur.close,"forming_direction":"SELL"}
        return {"valid":False,"side":"NONE","type":"NONE","liquidity_grab":False,"impulse":False,"trigger_source":"LIVE_WINDOW_CLOSED","trigger_age_seconds":live_age}
    atr,body=_atr(frame),abs(cur.close-cur.open);impulse=bool(atr>0 and body>=atr*.5)
    gr_buy=any(x.low<lo for x in recent) and _side(cur)=="BUY" and cur.close>lo
    gr_sell=any(x.high>hi for x in recent) and _side(cur)=="SELL" and cur.close<hi
    br_buy=_side(cur)=="BUY" and cur.close>hi and impulse;br_sell=_side(cur)=="SELL" and cur.close<lo and impulse
    if gr_buy and impulse: typ,side="LIQUIDITY_GRAB_PRESSURE","BUY"
    elif gr_sell and impulse: typ,side="LIQUIDITY_GRAB_PRESSURE","SELL"
    elif br_buy: typ,side="IMPULSE_STRUCTURE_BREAK","BUY"
    elif br_sell: typ,side="IMPULSE_STRUCTURE_BREAK","SELL"
    else: return {"valid":False,"side":"NONE","type":"NONE","liquidity_grab":gr_buy or gr_sell,"impulse":impulse}
    # FAIL-CLOSED. Was: only blocked when expected_side was BUY/SELL, so an
    # unknown direction (None) permitted BOTH sides - the filter disabled
    # itself exactly when the trend was unclear. Measured 2026-08-17: this
    # costs ~0.4% of scans to close, not meaningful trade volume.
    if expected_side == "ANY":
        pass
    elif expected_side not in {"BUY", "SELL"} or side != expected_side:
        return {"valid": False, "side": "NONE", "type": typ, "trigger_source": "SETUP_DIRECTION_MISMATCH", "setup_direction": expected_side, "detected_direction": side}
    return {"valid":True,"side":side,"type":typ,"liquidity_grab":gr_buy or gr_sell,"impulse":True,"level":lo if side=="BUY" else hi,"trigger_source":"COMPLETED_CANDLE"}

def _memory(frame, side: str) -> dict[str,Any]:
    global _MEMORY,_MEMORY_MTIME
    out={"role":"SETUP_RECOGNITION","recognized":False,"shape":None,"neighbours":0}
    try:
        from ..pattern_memory import PatternMemory,encode_shape
        path=Path(os.getenv("MT5_PATTERN_MEMORY_PATH","/opt/cipherfx_mt5/config/pattern_memory.npz"))
        stamp=path.stat().st_mtime_ns if path.exists() else None
        if _MEMORY is None or stamp!=_MEMORY_MTIME: _MEMORY,_MEMORY_MTIME=PatternMemory.load(str(path)),stamp
        shape=encode_shape([x.close for x in _candles(frame)]);out["shape"]=shape
        result=_MEMORY.query(shape) if _MEMORY is not None else None
        if result:
            taken=result["buy"] if side=="BUY" else result["sell"];other=result["sell"] if side=="BUY" else result["buy"]
            out.update({"recognized":True,"expected_r":round(taken,4),"opposite_r":round(other,4),"neighbours":result["n"]})
    except Exception: pass
    return out

def _context(frame)->dict[str,Any]:
    c=_candles(frame)
    return {"available":bool(c),"direction":_direction(frame),"bar_time":c[-1].timestamp.isoformat() if c else ""}



def build_setup(snapshot: MarketSnapshot)->dict[str,Any]:
    regime=classify_market(snapshot.frames)
    h4=_context(snapshot.frames.get("H4"));m15=_context(snapshot.frames.get("M15"))
    regime_h4=((regime.get("timeframes") or {}).get("H4") or {}).get("direction")
    if regime_h4 in {"BUY", "SELL"}:
        h4={**h4,"raw_direction":h4["direction"],"direction":regime_h4,"direction_source":"H4_REGIME"}
    # H4 establishes direction. H1 is recorded as context only and never
    # acts as a second hidden approval gate.
    _h4d=_recent_direction(snapshot.frames.get("H4"),int(float(_os.getenv("MT5_H4_TREND_LOOKBACK","3"))))
    _h1d=_recent_direction(snapshot.frames.get("H1"),int(float(_os.getenv("MT5_H1_TREND_LOOKBACK","5"))))
    setup_direction = _h4d
    h4={**h4,"h4_recent":_h4d,"h1_recent":_h1d,"direction_source":"H4_DIRECTION"}
    chart=_chart_setup(snapshot.frames.get("M5"),snapshot.tick,setup_direction)
    memory=_memory(snapshot.frames.get("M5"),setup_direction or chart["side"]);m5=_candles(snapshot.frames.get("M5"))
    frames={"H4":h4,"M15":m15,"M5":{**chart,"bar_time":m5[-1].timestamp.isoformat() if m5 else ""},"setup_direction":setup_direction or chart["side"]}
    reason="" if chart["valid"] else ("M5_TRIGGER_DIRECTION_MISMATCH" if chart.get("trigger_source") == "SETUP_DIRECTION_MISMATCH" else "M5_LIVE_TRIGGER_WINDOW_CLOSED" if chart.get("trigger_source") == "LIVE_WINDOW_CLOSED" else "M5_CHART_SETUP_NOT_FOUND")
    if reason:
        return {"valid":False,"side":"NO_TRADE","engine":ENGINE,"strategy_name":STRATEGY,"setup_type":STRATEGY,"frames":frames,"chart_setup":chart,"memory":memory,"context":{"h4":frames["H4"],"m15":frames["M15"],"market_regime":regime},"decision_role":"CHART_SETUP_WITH_MEMORY","rejection_reason":reason,"setup_direction":setup_direction or chart["side"]}
    side, atr = chart["side"], _atr(snapshot.frames.get("M5"))
    entry = float(snapshot.tick.ask if side == "BUY" else snapshot.tick.bid)
    setup_close = float(m5[-1].close) if m5 else entry
    extension = max(0.0, entry - setup_close if side == "BUY" else setup_close - entry)
    max_extension = atr * 0.20
    live_trigger = chart.get("trigger_source") == "LIVE_TICK"
    entry_timing = {
        "setup_close": setup_close,
        "live_entry": entry,
        "extension": extension,
        "max_extension": max_extension,
        "extension_atr": extension / atr if atr > 0 else 0.0,
        "status": "LIVE_TRIGGER" if live_trigger else ("PASS" if atr > 0 and extension <= max_extension else "TOO_EXTENDED"),
    }
    if entry_timing["status"] not in {"PASS", "LIVE_TRIGGER"}:
        return {"valid": False, "side": "NO_TRADE", "engine": ENGINE, "strategy_name": STRATEGY, "setup_type": STRATEGY, "entry_model": "LIVE_M5_TICK_BREAK" if chart.get("trigger_source") == "LIVE_TICK" else "LIVE_CHART_IMMEDIATE", "frames": frames, "chart_setup": chart, "context": {"h4": frames["H4"], "m15": frames["M15"], "market_regime": regime}, "memory": memory, "entry": entry, "entry_timing": entry_timing, "decision_role": "CHART_SETUP_WITH_MEMORY", "rejection_reason": "ENTRY_TOO_EXTENDED_AFTER_M5_CLOSE"}
    stop=min(x.low for x in m5[-6:])-atr*.1 if side=="BUY" else max(x.high for x in m5[-6:])+atr*.1
    structural_distance=abs(entry-stop)
    # ATR floor on the stop distance. Backtested 2026-08-17 over 69 days,
    # 8/8 folds both ways: floor ON -> 78.9% win / +0.109 R/trade / +1047R;
    # floor OFF -> 73.9% win / +0.180 R/trade / +1897R. Set to 0 for no
    # floor (structural stop only); the distance>=atr*0.2 validity check
    # below still rejects absurdly tight stops.
    _floor=float(_os.getenv("MT5_METALS_STOP_ATR_FLOOR","0.0"))
    distance=max(structural_distance, atr*_floor) if _floor>0 else structural_distance
    # Same fix as indices.py: the stop must sit at the distance the target is
    # measured from, or the advertised R:R is not the R:R actually traded.
    stop = entry - distance if side == "BUY" else entry + distance
    stop_ok = bool(atr > 0 and distance >= atr * .2 and isfinite(distance))
    _tr=float(_os.getenv("MT5_METALS_TARGET_R","3.0"))
    target = entry + distance * _tr if side == "BUY" else entry - distance * _tr
    return {"valid": stop_ok, "side": side if stop_ok else "NO_TRADE", "engine": ENGINE, "strategy_name": STRATEGY, "setup_type": STRATEGY, "entry_model": "LIVE_M5_TICK_BREAK" if chart.get("trigger_source") == "LIVE_TICK" else "LIVE_CHART_IMMEDIATE", "frames": frames, "chart_setup": chart, "context": {"h4": frames["H4"], "m15": frames["M15"], "market_regime": regime}, "memory": memory, "entry": entry, "entry_timing": entry_timing, "stop_loss": stop, "take_profit": target, "stop_distance": distance, "atr": float(atr), "stop_valid": stop_ok, "minimum_rr": _tr, "decision_role": "CHART_SETUP_WITH_MEMORY", "reasons": [f"CHART_SETUP_{chart['type']}", f"CHART_DIRECTION_{side}", "LIVE_TICK_TRIGGER" if chart.get("trigger_source") == "LIVE_TICK" else "COMPLETED_CANDLE_TRIGGER", "MEMORY_RECOGNIZED" if memory.get("recognized") else "MEMORY_OBSERVATION_ONLY", "ENTRY_LOCATION_PASS", "STOP_DISTANCE_PASS" if stop_ok else "STOP_DISTANCE_INVALID"], "rejection_reason": "" if stop_ok else "STOP_DISTANCE_INVALID","setup_direction":setup_direction or side}
