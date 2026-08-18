"""Live trade signals for MANUAL execution.

Reads the same engine classes the bot runs, feeds them current bridge candles,
and reports what each one is watching: direction, the exact entry price, the
stop, and the target.

This is a READ-ONLY feed. It never places, modifies or closes anything. It
exists so a human can see the setup and decide for themselves - the account it
is meant to be traded on (336722733) has no bot attached to it at all.

Why it recomputes rather than reading the bot's event log: the router
overwrites last_report as it walks the engine chain, so SDZONE's view is lost
whenever a later engine also reports. Recomputing from the same classes gives
the true current state.
"""
from __future__ import annotations

import csv
import os
import types
from datetime import datetime, timezone
from pathlib import Path

# broker rate files are stamped in BROKER time (UTC+3), not UTC
BROKER_OFFSET_SECONDS = 10800


def _bridge() -> Path:
    return Path(os.getenv("MT5_BRIDGE_DIR",
                          "/root/.mt5/drive_c/Program Files/MetaTrader 5/MQL5/Files/cipherfx"))


def _candles(symbol: str, timeframe: str, limit: int = 600):
    """Completed candles only - never the forming bar."""
    path = _bridge() / f"rates_{symbol}_{timeframe}.csv"
    if not path.exists():
        return []
    rows = []
    try:
        with path.open(newline="", errors="ignore") as fh:
            for r in csv.DictReader(fh):
                try:
                    rows.append(types.SimpleNamespace(
                        timestamp=int(r["time"]) - BROKER_OFFSET_SECONDS,
                        open=float(r["open"]), high=float(r["high"]),
                        low=float(r["low"]), close=float(r["close"]),
                    ))
                except (TypeError, ValueError, KeyError):
                    continue
    except OSError:
        return []
    rows.sort(key=lambda c: c.timestamp)
    return rows[-limit:]


def _tick(symbol: str):
    path = _bridge() / f"tick_{symbol}.txt"
    out = {}
    try:
        for line in path.read_text().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                try:
                    out[k.strip()] = float(v)
                except ValueError:
                    pass
    except OSError:
        return None
    if not out.get("bid") or not out.get("ask"):
        return None
    return out


PIP = 0.1  # index instruments: 1 pip = 0.1 index point (broker point = 0.01)



def _pivots(cs, piv=5):
    """Confirmed swing highs/lows. A pivot at index i is only valid once `piv`
    bars have printed after it, so nothing here uses unformed structure."""
    highs, lows = [], []
    for i in range(piv, len(cs) - piv):
        w = cs[i - piv:i + piv + 1]
        if cs[i].high == max(c.high for c in w):
            highs.append({"i": i, "price": round(cs[i].high, 2)})
        if cs[i].low == min(c.low for c in w):
            lows.append({"i": i, "price": round(cs[i].low, 2)})
    return highs, lows


def _label_structure(highs, lows):
    """Label swings HH/HL/LH/LL and derive trend, the way a trader reads a chart.

    HH = higher high, HL = higher low  -> uptrend
    LH = lower high,  LL = lower low   -> downtrend
    Trend is taken from the two most recent swings of each kind: a chart making
    higher highs AND higher lows is an uptrend; lower highs AND lower lows is a
    downtrend; anything else is range.
    """
    for i, h in enumerate(highs):
        h["label"] = "HH" if i and h["price"] > highs[i - 1]["price"] else ("LH" if i else "H")
    for i, l in enumerate(lows):
        l["label"] = "HL" if i and l["price"] > lows[i - 1]["price"] else ("LL" if i else "L")
    trend = "RANGE"
    if len(highs) >= 2 and len(lows) >= 2:
        up = highs[-1]["price"] > highs[-2]["price"] and lows[-1]["price"] > lows[-2]["price"]
        dn = highs[-1]["price"] < highs[-2]["price"] and lows[-1]["price"] < lows[-2]["price"]
        trend = "UPTREND" if up else ("DOWNTREND" if dn else "RANGE")
    return trend


def _chart(cs, bars=90):
    """Candles for drawing, plus the swing structure the engine reasons about."""
    tail = cs[-bars:]
    off = len(cs) - len(tail)
    highs, lows = _pivots(cs)
    trend = _label_structure(highs, lows)
    return {
        "trend": trend,
        "first_ts": tail[0].timestamp if tail else None,
        "last_ts": tail[-1].timestamp if tail else None,
        "candles": [{"t": c.timestamp, "o": round(c.open, 2), "h": round(c.high, 2),
                     "l": round(c.low, 2), "c": round(c.close, 2)} for c in tail],
        "swing_highs": [{"i": h["i"] - off, "price": h["price"], "label": h.get("label", "H")}
                        for h in highs if h["i"] >= off],
        "swing_lows": [{"i": l["i"] - off, "price": l["price"], "label": l.get("label", "L")}
                       for l in lows if l["i"] >= off],
    }


def _sdzone(symbol: str, broker_symbol: str) -> dict | None:
    try:
        from cipherfx_platform.engines.sdzone import SupplyDemandZoneEngine
    except Exception:
        return None
    cs = _candles(broker_symbol, "M30")
    if len(cs) < 60:
        return None
    tick = _tick(broker_symbol)
    if not tick:
        return None
    eng = SupplyDemandZoneEngine()
    found = eng._find_zone(cs)
    if not found:
        # No setup - still report the symbol so it is visibly being watched
        # rather than silently missing from the page.
        ch = {}
        for tf in ("H4", "H1", "M30", "M15", "M5", "M1"):
            t_cs = _candles(broker_symbol, tf, 200)
            if len(t_cs) >= 20:
                ch[tf] = _chart(t_cs, bars=min(140, len(t_cs)))
        return dict(engine="SDZONE", symbol=symbol, side=None, status="no setup",
                    price=round((tick["bid"] + tick["ask"]) / 2, 2),
                    chart=ch.get("M30") or _chart(cs), charts=ch, default_tf="M30",
                    triggered=False,
                    note="no fresh supply/demand zone on M30 right now")
    side, proximal, distal = found
    # Find which bar the zone formed on so the chart can include it. Without
    # this the level is drawn off the edge of the candles - the engine looks
    # back 480 bars but the chart only drew 90, so an older zone rendered
    # below/above every candle and looked wrong.
    price = tick["ask"] if side == "BUY" else tick["bid"]
    stop = proximal - eng.stop_pips * PIP if side == "BUY" else proximal + eng.stop_pips * PIP
    level = eng._next_structural_level(cs, proximal, side)
    if level is not None and abs(level - proximal) >= eng.min_target_pips * PIP:
        target, tsrc = level, "next swing level"
    else:
        target = proximal + eng.target_pips * PIP if side == "BUY" else proximal - eng.target_pips * PIP
        tsrc = "fixed"
    away = abs(price - proximal) / PIP
    triggered = (price <= proximal) if side == "BUY" else (price >= proximal)
    # Charts on several timeframes. The engine decides on M30, but a trader
    # needs context (H4/H1) to see where the level sits, and something fast
    # (M5/M1) to time the entry. Measured on live data: GOLD's 1000-pip stop
    # only fits inside the H4 range - on M30 it drew off the edge of the
    # candles, which is what made the first version look wrong.
    charts = {}
    for tf in ("H4", "H1", "M30", "M15", "M5", "M1"):
        tf_cs = _candles(broker_symbol, tf, 200)
        if len(tf_cs) >= 20:
            charts[tf] = _chart(tf_cs, bars=min(140, len(tf_cs)))
    levels = [v for v in (proximal, stop, target) if v is not None]
    # Default to the FRESHEST timeframe that still contains the whole setup.
    # Bar files only rewrite when a bar closes, so measured live: M1 52s old,
    # M5 172s, M30 22min, H4 3.4 HOURS. Defaulting to H4 for "context" meant
    # the chart was hours behind the live tick price shown beside it.
    default_tf = "M5"
    for tf in ("M5", "M15", "M30", "H1", "H4"):
        c = charts.get(tf)
        if not c or not c.get("candles"):
            continue
        lo_c = min(r["l"] for r in c["candles"])
        hi_c = max(r["h"] for r in c["candles"])
        if all(lo_c <= v <= hi_c for v in levels):
            default_tf = tf
            break
    chart = charts.get(default_tf) or _chart(cs)
    sizing = _sizing(broker_symbol, proximal, stop)
    risk = abs(proximal - stop) / PIP
    reward = abs(target - proximal) / PIP
    return dict(
        engine="SDZONE", symbol=symbol, side=side, chart=chart,
        charts=charts, default_tf=default_tf, sizing=sizing,
        zone_high=round(max(proximal, distal), 2),
        zone_low=round(min(proximal, distal), 2),
        entry=round(proximal, 2), stop=round(stop, 2), target=round(target, 2),
        target_source=tsrc, price=round(price, 2),
        pips_away=round(away, 0), triggered=triggered,
        risk_pips=round(risk, 0), reward_pips=round(reward, 0),
        rr=round(reward / risk, 2) if risk else None,
        note=f"fresh {'demand' if side == 'BUY' else 'supply'} zone, entry at the proximal edge",
    )


def _fade(symbol: str, broker_symbol: str) -> dict | None:
    cs = _candles(broker_symbol, "H1")
    if len(cs) < 25:
        return None
    tick = _tick(broker_symbol)
    if not tick:
        return None
    closes = [c.close for c in cs][-20:]
    mean = sum(closes) / len(closes)
    sd = (sum((x - mean) ** 2 for x in closes) / len(closes)) ** 0.5
    if sd <= 0:
        return None
    upper, lower = mean + 2 * sd, mean - 2 * sd
    last = cs[-1].close
    side = "BUY" if last < lower else ("SELL" if last > upper else None)
    if side is None:
        return None  # SDZONE already reports this symbol's "watching" state
    price = tick["ask"] if side == "BUY" else tick["bid"]
    edge = lower if side == "BUY" else upper
    return dict(
        engine="FADE", symbol=symbol, side=side, chart=_chart(cs),
        entry=round(edge, 2) if side else None,
        stop=None, target=round(mean, 2),
        target_source="band mean", price=round(price, 2),
        pips_away=round(abs(last - edge) / PIP, 0),
        triggered=bool(side),
        band_upper=round(upper, 2), band_lower=round(lower, 2),
        band_mean=round(mean, 2), h1_close=round(last, 2),
        note="H1 close outside the 20/2 Bollinger band - fade back to the mean",
    )


SYMBOLS = [("US30", "US30Cash"), ("NAS100", "US100Cash"),
           ("SPX500", "US500Cash"), ("XAUUSD", "GOLD"), ("UK100", "UK100Cash")]


def _feed_age() -> dict:
    """Seconds since each rate file was last written - bar files only rewrite
    on bar close, so higher timeframes are legitimately old."""
    import time
    out = {}
    for tf in ("M1", "M5", "M15", "M30", "H1", "H4"):
        f = _bridge() / f"rates_US30Cash_{tf}.csv"
        try:
            out[tf] = int(time.time() - f.stat().st_mtime)
        except OSError:
            pass
    return out


def _sizing(symbol_broker: str, entry: float, stop: float) -> dict:
    """What one lot risks on this trade, so the size can be chosen BEFORE the
    order is placed. MT5 clears SL/TP when a ticket's volume is edited, so
    "place at minimum lot then resize" silently strips the protection - the
    size has to be right at placement time.
    """
    try:
        from config.settings import load_settings
        from mt5_xm_gateway import MT5Gateway
        gw = MT5Gateway(load_settings().runtime)
        spec = gw.symbol_info(symbol_broker)
        contract = float(getattr(spec, "contract_size", 1.0) or 1.0)
        dist = abs(float(entry) - float(stop))
        return {"min_lot": float(spec.volume_min), "lot_step": float(spec.volume_step),
                "max_lot": float(spec.volume_max),
                "risk_per_lot": round(dist * contract, 2),
                "stop_distance": round(dist, 2)}
    except Exception:
        return {}


def build_signals(only_tf: str | None = None) -> dict:
    out = []
    for canonical, broker in SYMBOLS:
        for fn in (_sdzone, _fade):
            try:
                sig = fn(canonical, broker)
            except Exception as exc:
                sig = dict(engine=fn.__name__.strip("_").upper(), symbol=canonical,
                           error=str(exc)[:160])
            if sig and (sig.get("side") or sig.get("error") or sig.get("status")):
                sig["broker_symbol"] = broker
                out.append(sig)
    out.sort(key=lambda s: (not s.get("triggered"), s.get("pips_away") or 9e9))
    # Ship ONE timeframe's candles, not all five. Sending every timeframe made
    # the payload 263 KB on each 10s poll, which on mobile looked like the page
    # was not loading at all. The page asks for another timeframe on demand.
    for sig in out:
        charts = sig.get("charts") or {}
        sig["available_tfs"] = list(charts.keys())
        want = only_tf if (only_tf and only_tf in charts) else sig.get("default_tf")
        if want and want in charts:
            sig["chart"] = charts[want]
            sig["default_tf"] = want
        sig.pop("charts", None)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "feed_age": _feed_age(),
        "trade_account": os.getenv("MANUAL_TRADE_ACCOUNT", "336722733"),
        "signals": out,
    }
