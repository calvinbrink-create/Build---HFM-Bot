"""Stage a signal as a PENDING order on MT5 for manual sizing.

Deliberately narrow:
  * PENDING LIMIT only - never a market order. It rests at the signal's entry
    price and fills only if price actually comes to it.
  * MINIMUM broker volume. The human resizes in MT5 before it fills. Nothing
    here decides how much money is at risk.
  * Stop and target attached at placement, so a filled order is never naked.
  * The bot's trading runtime owns its own execution path; this is separate and
    only ever fires on an explicit button press.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/opt/cipherfx_mt5")


def stage(symbol: str, side: str, entry: float, stop: float, target: float,
          volume: float | None = None) -> dict:
    from config.settings import load_settings
    from mt5_xm_gateway import MT5Gateway

    side = str(side or "").upper()
    if side not in {"BUY", "SELL"}:
        return {"ok": False, "error": "side must be BUY or SELL"}
    for name, v in (("entry", entry), ("stop", stop), ("target", target)):
        if v is None or float(v) <= 0:
            return {"ok": False, "error": f"{name} is required"}
    entry, stop, target = float(entry), float(stop), float(target)

    # geometry must be sane or the broker rejects it anyway
    if side == "BUY" and not (stop < entry < target):
        return {"ok": False, "error": f"BUY needs stop < entry < target (got {stop}/{entry}/{target})"}
    if side == "SELL" and not (target < entry < stop):
        return {"ok": False, "error": f"SELL needs target < entry < stop (got {target}/{entry}/{stop})"}

    settings = load_settings()
    gw = MT5Gateway(settings.runtime)
    spec = gw.symbol_info(symbol)
    vol = float(volume) if volume else float(spec.volume_min)

    # A BUY resting BELOW price is a BUY LIMIT; a SELL resting ABOVE price is a
    # SELL LIMIT. If price is already past the level a limit cannot be placed.
    _, tick = gw.symbol_tick(symbol)
    px = float(tick.ask if side == "BUY" else tick.bid)
    if side == "BUY" and entry >= px:
        return {"ok": False, "error": f"price {px:.2f} is already at/below the {entry:.2f} entry - a BUY LIMIT cannot be placed above price"}
    if side == "SELL" and entry <= px:
        return {"ok": False, "error": f"price {px:.2f} is already at/above the {entry:.2f} entry - a SELL LIMIT cannot be placed below price"}

    resolved, result = gw.place_pending_order(
        symbol=symbol, direction=side, volume=vol, price=entry,
        sl=stop, tp=target, pending_type="LIMIT",
        comment="signal-staged",
    )
    return {
        "ok": True, "symbol": resolved, "side": side, "volume": vol,
        "entry": entry, "stop": stop, "target": target,
        "risk_points": round(abs(entry - stop), 2),
        "reward_points": round(abs(target - entry), 2),
        "result": str(result)[:300],
        "staged_at": datetime.now(timezone.utc).isoformat(),
        "note": "PENDING LIMIT at minimum lot - resize it in MT5 before it fills",
    }
