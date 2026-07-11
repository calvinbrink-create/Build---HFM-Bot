#!/usr/bin/env python3
"""VPS-only shadow adapter for strategy_architecture_v1. It never sends orders."""
from __future__ import annotations
import json, os, sys
from datetime import datetime, timezone
from pathlib import Path

from mt5_xm_config import MT5RuntimeConfig
from mt5_xm_gateway import MT5Gateway
from strategy_architecture_v1 import Candle, Frame, Profile, evaluate

def asset_for_group(group):
    if group == "forex":
        return "forex"
    if group == "metals":
        return "metal"
    if group in {"indices", "stocks", "etfs"}:
        return "index"
    return ""

def frame_from_df(df, label):
    rows = []
    for idx, row in df.iterrows():
        stamp = idx.isoformat() if hasattr(idx, "isoformat") else str(idx)
        rows.append(Candle(stamp, float(row["Open"]), float(row["High"]), float(row["Low"]), float(row["Close"]), float(row.get("Volume", 0.0) or 0.0)))
    return Frame(label, tuple(rows))

def profile_for(symbol, asset):
    defaults = {
        "forex": (1.25, 1.60, 0.35, 0.005),
        "index": (1.50, 1.80, 0.25, 0.005),
        "metal": (1.60, 1.80, 0.45, 0.0035),
    }
    stop, target, spread, risk = defaults[asset]
    try:
        import mt5_bot
        cfg = mt5_bot.get_symbol_config(symbol) or {}
        stop = float(cfg.get("atr_stop_mult") or stop)
        target = float(cfg.get("tp_r") or target)
    except Exception:
        pass
    return Profile(symbol, asset, stop, target, spread, risk)

def broker_geometry(gateway, symbol, decision):
    resolved, tick = gateway.symbol_tick(symbol)
    spec = gateway.symbol_info(symbol)
    price = float(tick.ask if decision.side == "BUY" else tick.bid)
    if decision.side == "BUY":
        sl, tp = price - decision.stop_distance, price + decision.target_distance
        valid = sl < price < tp
    else:
        sl, tp = price + decision.stop_distance, price - decision.target_distance
        valid = tp < price < sl
    min_distance = max(float(spec.stops_level or 0) * float(spec.point or 0.0), float(spec.point or 0.0))
    if decision.side == "BUY":
        valid = valid and price - sl >= min_distance and tp - price >= min_distance
    else:
        valid = valid and sl - price >= min_distance and price - tp >= min_distance
    return {
        "resolved_symbol": resolved,
        "bid": float(tick.bid),
        "ask": float(tick.ask),
        "price": price,
        "sl": sl,
        "tp": tp,
        "min_distance": min_distance,
        "valid": bool(valid),
        "volume_min": float(spec.volume_min),
        "volume_step": float(spec.volume_step),
        "volume_max": float(spec.volume_max),
        "tick_value": float(spec.tick_value_loss or spec.tick_value or 0.0),
        "tick_size": float(spec.tick_size or spec.point or 0.0),
    }

def run():
    runtime = MT5RuntimeConfig()
    gateway = MT5Gateway(runtime)
    gateway.connect()
    account = gateway.account_info()
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": gateway.mode,
        "connected": True,
        "trade_allowed": bool(getattr(account, "trade_allowed", False)),
        "equity": float(getattr(account, "equity", 0.0) or 0.0),
        "symbols": [],
        "counts": {"evaluated": 0, "pass": 0, "not_qualified": 0, "data_error": 0, "geometry_valid": 0, "geometry_invalid": 0},
    }
    for group, symbols in runtime.symbol_groups().items():
        asset = asset_for_group(group)
        if asset not in {"forex", "index", "metal"}:
            continue
        for symbol in symbols:
            item = {"symbol": symbol, "group": group, "asset_class": asset}
            report["counts"]["evaluated"] += 1
            try:
                frames = {
                    "H4": frame_from_df(gateway.rates(symbol, "H4", 100), "H4"),
                    "H1": frame_from_df(gateway.rates(symbol, "H1", 120), "H1"),
                    "M15": frame_from_df(gateway.rates(symbol, "M15", 160), "M15"),
                    "M5": frame_from_df(gateway.rates(symbol, "M5", 180), "M5"),
                    "M1": frame_from_df(gateway.rates(symbol, "M1", 61).iloc[:-1], "M1"),
                }
                profile = profile_for(symbol, asset)
                decision = evaluate(symbol, profile, frames)
                item["decision"] = {
                    "engine": decision.engine,
                    "setup_type": decision.setup_type,
                    "side": decision.side,
                    "state": decision.state,
                    "score": decision.score,
                    "expected_edge_r": decision.expected_edge_r,
                    "stop_distance": decision.stop_distance,
                    "target_distance": decision.target_distance,
                    "reason": decision.reason,
                    "reasons": decision.reasons,
                    "evidence": decision.evidence,
                }
                if decision.state == "PASS":
                    report["counts"]["pass"] += 1
                    geometry = broker_geometry(gateway, symbol, decision)
                    item["broker_geometry"] = geometry
                    if geometry["valid"]:
                        report["counts"]["geometry_valid"] += 1
                    else:
                        report["counts"]["geometry_invalid"] += 1
                else:
                    report["counts"]["not_qualified"] += 1
            except Exception as exc:
                report["counts"]["data_error"] += 1
                item["error"] = str(exc)[:240]
            report["symbols"].append(item)
    gateway.shutdown()
    print(json.dumps(report, indent=2, sort_keys=True))

if __name__ == "__main__":
    run()
