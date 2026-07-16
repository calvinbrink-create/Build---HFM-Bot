"""Read-only portfolio supervision for CipherFX.

This module has no imports from strategy, learning, execution, risk, or
position-management code. It consumes already-produced facts and returns
informational portfolio analytics. It cannot approve, reject, delay, or
modify a trade.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import math
from typing import Any, Iterable


TERMINAL_PROPOSAL_STATES = {
    "EXECUTED",
    "EXPIRED",
    "INVALIDATED",
    "REJECTED",
    "EXECUTION_BLOCKED",
    "CANCELLED",
}


def _number(value: Any, default: float | None = None) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _text(value: Any, default: str = "") -> str:
    value = str(value or "").strip()
    return value if value else default


def _first_number(row: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _number(row.get(key))
        if value is not None:
            return value
    return None


def _health_label(score: int) -> str:
    if score >= 90:
        return "Excellent"
    if score >= 75:
        return "Strong"
    if score >= 60:
        return "Healthy"
    if score >= 45:
        return "Neutral"
    if score >= 30:
        return "Weak"
    if score >= 15:
        return "Danger"
    return "Critical"


def _position_rank(profit: float | None, equity: float | None) -> str:
    if profit is None:
        return "Neutral"
    if profit > 0:
        return "Strong" if equity is None or profit / max(abs(equity), 1.0) < 0.01 else "Excellent"
    if profit == 0:
        return "Neutral"
    loss_ratio = abs(profit) / max(abs(equity or 0.0), 1.0)
    if loss_ratio >= 0.03:
        return "Critical"
    if loss_ratio >= 0.015:
        return "Danger"
    return "Weak"


def _risk_status(
    *,
    connected: bool,
    stale_ticks: int,
    margin_utilization: float | None,
    equity: float | None,
    unrealized_pnl: float | None,
) -> str:
    if not connected:
        return "CRITICAL"
    if stale_ticks > 0:
        return "HIGH"
    if margin_utilization is not None and margin_utilization >= 0.80:
        return "HIGH"
    if margin_utilization is not None and margin_utilization >= 0.50:
        return "ELEVATED"
    if equity and unrealized_pnl is not None and unrealized_pnl / abs(equity) <= -0.02:
        return "ELEVATED"
    return "NORMAL"


def _group_exposure(rows: Iterable[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        label = _text(row.get(key), "UNATTRIBUTED").upper()
        item = grouped.setdefault(label, {"name": label, "positions": 0, "volume": 0.0, "pnl": 0.0})
        item["positions"] += 1
        item["volume"] += _number(row.get("volume", row.get("qty")), 0.0) or 0.0
        item["pnl"] += _number(row.get("profit", row.get("unrealized")), 0.0) or 0.0
    return sorted(
        [
            {**item, "volume": round(item["volume"], 4), "pnl": round(item["pnl"], 2)}
            for item in grouped.values()
        ],
        key=lambda item: abs(item["volume"]),
        reverse=True,
    )


def build_portfolio_snapshot(
    *,
    account: dict[str, Any] | None,
    positions: Iterable[dict[str, Any]] | None,
    orders: Iterable[dict[str, Any]] | None,
    proposal_states: dict[str, int] | None,
    closed_summaries: dict[str, dict[str, Any] | None] | None,
    operational: dict[str, Any] | None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Build a portfolio-only snapshot from already authoritative facts."""
    account = dict(account or {})
    rows = [dict(row) for row in (positions or []) if isinstance(row, dict)]
    pending_orders = [dict(row) for row in (orders or []) if isinstance(row, dict)]
    operational = dict(operational or {})
    proposal_states = {str(key).upper(): int(value or 0) for key, value in (proposal_states or {}).items()}
    closed_summaries = dict(closed_summaries or {})

    balance = _first_number(account, "balance")
    equity = _first_number(account, "equity")
    margin = _first_number(account, "margin")
    free_margin = _first_number(account, "free_margin", "free")
    if free_margin is None and equity is not None and margin is not None:
        free_margin = equity - margin
    margin_utilization = None
    if equity is not None and equity > 0 and margin is not None:
        margin_utilization = max(0.0, margin / equity)

    unrealized_pnl = round(sum(_first_number(row, "profit", "unrealized") or 0.0 for row in rows), 2)
    gross_volume = round(sum(abs(_first_number(row, "volume", "qty") or 0.0) for row in rows), 4)
    net_volume = 0.0
    for row in rows:
        volume = abs(_first_number(row, "volume", "qty") or 0.0)
        side = _text(row.get("side", row.get("direction"))).upper()
        net_volume += volume if side == "BUY" else -volume if side == "SELL" else 0.0
    stale_ticks = int(operational.get("stale_tick_count") or 0)
    connected = bool(operational.get("mt5_connected"))
    health_score = 100
    if not connected:
        health_score -= 45
    if stale_ticks:
        health_score -= min(25, 10 + stale_ticks)
    if equity is None or equity <= 0:
        health_score -= 20
    if margin_utilization is not None:
        if margin_utilization >= 0.80:
            health_score -= 30
        elif margin_utilization >= 0.50:
            health_score -= 15
    if equity and unrealized_pnl / abs(equity) <= -0.02:
        health_score -= 15
    health_score = max(0, min(100, int(health_score)))

    symbol_counts = Counter(_text(row.get("symbol", row.get("sym")), "UNKNOWN").upper() for row in rows)
    side_counts = Counter(_text(row.get("side", row.get("direction")), "UNKNOWN").upper() for row in rows)
    symbol_exposure = _group_exposure(
        [{"symbol": _text(row.get("symbol", row.get("sym")), "UNKNOWN"), **row} for row in rows],
        "symbol",
    )

    position_views = []
    for row in rows:
        symbol = _text(row.get("symbol", row.get("sym")), "UNKNOWN").upper()
        profit = _first_number(row, "profit", "unrealized")
        position_views.append({
            "symbol": symbol,
            "side": _text(row.get("side", row.get("direction")), "UNKNOWN").upper(),
            "volume": round(abs(_first_number(row, "volume", "qty") or 0.0), 4),
            "entry": _first_number(row, "entry", "price_open"),
            "current": _first_number(row, "current", "price_current"),
            "sl": _first_number(row, "sl"),
            "tp": _first_number(row, "tp"),
            "pnl": round(profit, 2) if profit is not None else None,
            "rank": _position_rank(profit, equity),
            "engine": _text(row.get("engine"), "UNATTRIBUTED").upper(),
        })

    states_total = sum(proposal_states.values())
    pending_proposals = sum(
        count for state, count in proposal_states.items() if state not in TERMINAL_PROPOSAL_STATES
    )
    risk_status = _risk_status(
        connected=connected,
        stale_ticks=stale_ticks,
        margin_utilization=margin_utilization,
        equity=equity,
        unrealized_pnl=unrealized_pnl,
    )
    realized_pnl = round(
        sum(
            _number(summary.get("realized_pnl", summary.get("pnl")), 0.0) or 0.0
            for summary in closed_summaries.values()
            if isinstance(summary, dict)
        ),
        2,
    )
    current_drawdown = round(max(0.0, -unrealized_pnl), 2)
    current_drawdown_pct = (
        round(current_drawdown / abs(equity), 6)
        if equity and equity > 0 else None
    )
    position_risk = sum(
        abs(_first_number(row, "risk_amount", "risk") or 0.0) for row in rows
    )
    portfolio_heat = (
        round(position_risk / abs(equity), 6)
        if position_risk and equity and equity > 0 else None
    )
    return {
        "contract_version": "portfolio-intelligence-v1",
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "authority": {
            "informational_only": True,
            "trade_decision_authority": False,
            "can_create_trades": False,
            "can_approve_trades": False,
            "can_reject_trades": False,
            "can_delay_trades": False,
            "can_modify_proposals": False,
            "can_modify_positions": False,
        },
        "sources": {
            "account": "mt5_bridge_account",
            "positions": "mt5_bridge_positions",
            "orders": "mt5_bridge_orders",
            "proposals": "mt5_state.db.trade_proposals",
            "closed_trades": "mt5_state.db.trades",
        },
        "portfolio_health": {
            "score": health_score,
            "label": _health_label(health_score),
            "risk_status": risk_status,
            "status": "AVAILABLE" if connected else "DATA_UNAVAILABLE",
        },
        "capital": {
            "balance": balance,
            "equity": equity,
            "margin": margin,
            "free_margin": free_margin,
            "margin_utilization": round(margin_utilization, 4) if margin_utilization is not None else None,
        },
        "risk": {
            "status": risk_status,
            "portfolio_heat": portfolio_heat,
            "risk_budget": operational.get("risk_budget"),
            "drawdown": {
                "current": current_drawdown,
                "current_pct": current_drawdown_pct,
                "basis": "open_position_unrealized_pnl",
            },
        },
        "exposure": {
            "open_positions": len(rows),
            "pending_orders": len(pending_orders),
            "gross_volume": gross_volume,
            "net_volume": round(net_volume, 4),
            "by_symbol": symbol_exposure,
            "by_direction": dict(side_counts),
            "by_engine": _group_exposure(rows, "engine"),
            "directional_bias": "BUY" if net_volume > 0 else "SELL" if net_volume < 0 else "FLAT",
            "concentration": {
                "top_symbol": symbol_exposure[0]["name"] if symbol_exposure else None,
                "top_symbol_positions": symbol_counts.most_common(1)[0][1] if symbol_counts else 0,
                "directional_bias": "BUY" if net_volume > 0 else "SELL" if net_volume < 0 else "FLAT",
            },
        },
        "positions": position_views,
        "proposals": {
            "pending": pending_proposals,
            "total_recorded": states_total,
            "by_state": proposal_states,
        },
        "performance": {
            "closed_summaries": closed_summaries,
            "realized_pnl": realized_pnl,
            "unrealized_pnl": unrealized_pnl,
        },
        "correlation": {
            "status": "UNAVAILABLE",
            "reason": "No return series was supplied; no synthetic correlation is reported.",
            "same_symbol_concentration": dict(symbol_counts),
        },
        "operational": operational,
        "architecture": {
            "market_data": "observe",
            "learning": "unchanged",
            "execution": "unchanged",
            "trade_management": "analytics_only",
            "learning_feedback": "unchanged",
        },
    }

class PortfolioIntelligenceEngine:
    """Pure portfolio analytics; never participates in a trade decision."""

    def snapshot(self, **facts: Any) -> dict[str, Any]:
        return build_portfolio_snapshot(**facts)
