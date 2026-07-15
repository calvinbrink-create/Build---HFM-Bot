"""Leakage-safe triple-barrier outcome labels for completed market samples."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

@dataclass(frozen=True)
class BarrierLabel:
    label: str
    direction: str
    entry_time: str
    exit_time: str
    entry_price: float
    exit_price: float
    realized_r: float
    costs_paid: float
    future_bars_used: int
    upper_barrier: float
    lower_barrier: float
    horizon_bars: int

def _num(value: Any, default: float = 0.0) -> float:
    try: return float(value)
    except (TypeError, ValueError): return default

def triple_barrier_label(
    bars: Iterable[Mapping[str, Any]],
    *,
    direction: str,
    entry_price: float,
    initial_risk: float,
    take_profit_r: float = 1.0,
    horizon_bars: int = 12,
    spread: float = 0.0,
    commission: float = 0.0,
) -> BarrierLabel:
    rows = list(bars)
    if not rows or initial_risk <= 0:
        raise ValueError("bars and positive initial_risk are required")
    side = str(direction).upper()
    if side not in {"BUY", "SELL", "LONG", "SHORT"}:
        raise ValueError("direction must be BUY/SELL or LONG/SHORT")
    long = side in {"BUY", "LONG"}
    entry = float(entry_price)
    risk = float(initial_risk)
    upper = entry + take_profit_r * risk if long else entry - take_profit_r * risk
    lower = entry - risk if long else entry + risk
    cost = abs(float(spread)) + abs(float(commission))
    outcome = "TIME"
    exit_price = _num(rows[min(horizon_bars - 1, len(rows) - 1)].get("close"), entry)
    exit_time = str(rows[min(horizon_bars - 1, len(rows) - 1)].get("timestamp", ""))
    used = 0
    for row in rows[:max(1, horizon_bars)]:
        used += 1
        high, low = _num(row.get("high")), _num(row.get("low"))
        hit_upper = high >= upper if long else low <= upper
        hit_lower = low <= lower if long else high >= lower
        # Conservative same-bar tie rule: stop wins when both barriers are hit.
        if hit_lower:
            outcome, exit_price = "LOSS", lower
            exit_time = str(row.get("timestamp", exit_time))
            break
        if hit_upper:
            outcome, exit_price = "WIN", upper
            exit_time = str(row.get("timestamp", exit_time))
            break
        exit_price = _num(row.get("close"), exit_price)
        exit_time = str(row.get("timestamp", exit_time))
    gross = (exit_price - entry) if long else (entry - exit_price)
    return BarrierLabel(
        label=outcome, direction="LONG" if long else "SHORT",
        entry_time=str(rows[0].get("timestamp", "")), exit_time=exit_time,
        entry_price=entry, exit_price=exit_price, realized_r=(gross - cost) / risk,
        costs_paid=cost, future_bars_used=used, upper_barrier=upper,
        lower_barrier=lower, horizon_bars=horizon_bars,
    )

def label_samples(samples: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for sample in samples:
        label = triple_barrier_label(
            sample["future_bars"], direction=sample["direction"],
            entry_price=float(sample["entry_price"]),
            initial_risk=float(sample["initial_risk"]),
            take_profit_r=float(sample.get("take_profit_r", 1.0)),
            horizon_bars=int(sample.get("horizon_bars", 12)),
            spread=float(sample.get("spread", 0.0)),
            commission=float(sample.get("commission", 0.0)),
        )
        row = dict(sample)
        row["label"] = label.label
        row["realized_r"] = label.realized_r
        row["label_detail"] = label.__dict__
        output.append(row)
    return output
