#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import math
import random
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

FOREX_ENGINE = "FOREX_ENGINE"
INDEX_ENGINE = "INDEX_ENGINE"
METALS_ENGINE = "METALS_ENGINE"

MAX_DAILY_LOSS_USD = 5000.0
MAX_TRADES_PER_DAY = 200
MAX_OPEN_TRADES_TOTAL = 30
MAX_OPEN_TRADES_PER_SYMBOL = 10
MAX_CORRELATED_SAME_DIRECTION = 30
MAX_ASSET_CLASS_EXPOSURE = {"forex": 30, "index": 30, "metal": 30}
MAX_SYMBOL_LOSSES_PER_DAY = 2
MAX_ENGINE_LOSSES_PER_DAY = 4
MAX_CONSECUTIVE_LOSSES = 5

CSV_FIELDS = [
    "timestamp", "symbol", "engine", "strategy", "side", "score", "status",
    "block_reason", "spread", "slippage", "cost", "R_result", "profit_usd",
    "session", "regime", "population",
]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        value = float(value)
    except Exception:
        return float(default)
    return value if math.isfinite(value) else float(default)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return int(default)


def _canonical_asset_class(value: str) -> str:
    value = str(value or "").lower()
    if value in {"index_cfd", "index_cfd_eu", "indices"}:
        return "index"
    if value in {"metal", "metals"}:
        return "metal"
    if value == "forex":
        return "forex"
    return value or "unknown"


def _row_symbol(row: Any) -> str:
    if isinstance(row, dict):
        return str(row.get("sym") or row.get("symbol") or "").upper()
    return str(getattr(row, "symbol", "") or getattr(row, "sym", "") or "").upper()


def _row_direction(row: Any) -> str:
    if isinstance(row, dict):
        return str(row.get("direction") or row.get("side") or "").upper()
    return str(getattr(row, "direction", "") or getattr(row, "side", "") or "").upper()


def _row_asset_class(row: Any) -> str:
    if isinstance(row, dict):
        return _canonical_asset_class(str(row.get("asset_class") or row.get("market") or ""))
    return _canonical_asset_class(str(getattr(row, "asset_class", "") or getattr(row, "market", "") or ""))


def _trade_is_loss(row: dict[str, Any]) -> bool:
    outcome = str(row.get("outcome") or "").lower()
    if outcome == "loss":
        return True
    return _safe_float(row.get("realized") or row.get("profit_usd") or 0.0) < 0.0


def _trade_engine(row: dict[str, Any]) -> str:
    raw = row.get("engine")
    if raw:
        return str(raw)
    breakdown = row.get("score_breakdown")
    if isinstance(breakdown, str):
        try:
            breakdown = json.loads(breakdown)
        except Exception:
            breakdown = {}
    if isinstance(breakdown, dict):
        return str(breakdown.get("engine") or "")
    return ""


def _consecutive_losses(closed_trades: Iterable[dict[str, Any]]) -> int:
    rows = sorted(
        list(closed_trades or []),
        key=lambda row: str(row.get("closed_at") or row.get("opened_at") or row.get("timestamp") or ""),
        reverse=True,
    )
    streak = 0
    for row in rows:
        if _trade_is_loss(row):
            streak += 1
            continue
        if str(row.get("outcome") or "").lower() in {"win", "breakeven", "flat"} or _safe_float(row.get("realized") or 0.0) >= 0:
            break
    return streak


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    reason: str
    details: dict[str, Any]


class PortfolioRiskManager:
    def __init__(self, max_daily_loss_usd: float = MAX_DAILY_LOSS_USD, max_trades_per_day: int = MAX_TRADES_PER_DAY,
                 max_open_trades_total: int = MAX_OPEN_TRADES_TOTAL, max_open_trades_per_symbol: int = MAX_OPEN_TRADES_PER_SYMBOL,
                 max_correlated_same_direction: int = MAX_CORRELATED_SAME_DIRECTION,
                 max_asset_class_exposure: dict[str, int] | None = None,
                 max_symbol_losses_per_day: int = MAX_SYMBOL_LOSSES_PER_DAY,
                 max_engine_losses_per_day: int = MAX_ENGINE_LOSSES_PER_DAY,
                 max_consecutive_losses: int = MAX_CONSECUTIVE_LOSSES):
        self.max_daily_loss_usd = float(max_daily_loss_usd)
        self.max_trades_per_day = int(max_trades_per_day)
        self.max_open_trades_total = int(max_open_trades_total)
        self.max_open_trades_per_symbol = int(max_open_trades_per_symbol)
        self.max_correlated_same_direction = int(max_correlated_same_direction)
        self.max_asset_class_exposure = dict(max_asset_class_exposure or MAX_ASSET_CLASS_EXPOSURE)
        self.max_symbol_losses_per_day = int(max_symbol_losses_per_day)
        self.max_engine_losses_per_day = int(max_engine_losses_per_day)
        self.max_consecutive_losses = int(max_consecutive_losses)

    def risk_multiplier(self, consecutive_losses: int) -> float:
        return 0.5 if int(consecutive_losses or 0) >= 3 else 1.0

    def can_trade(self, symbol: str, engine: str, side: str, *, daily_loss_usd: float = 0.0,
                  trades_today: int = 0, open_positions: Iterable[Any] | None = None,
                  closed_trades: Iterable[dict[str, Any]] | None = None, asset_class: str = "",
                  news_blocked: bool = False) -> tuple[bool, str, dict[str, Any]]:
        symbol = str(symbol or "").upper()
        side = str(side or "").upper()
        asset_class = _canonical_asset_class(asset_class)
        open_positions = list(open_positions or [])
        closed_trades = list(closed_trades or [])
        details: dict[str, Any] = {
            "daily_loss_usd": round(float(daily_loss_usd), 2),
            "trades_today": int(trades_today or 0),
            "open_trades_total": len(open_positions),
            "symbol": symbol,
            "engine": engine,
            "asset_class": asset_class,
            "side": side,
        }
        if news_blocked:
            return False, "major news window blocks new entries", details
        if float(daily_loss_usd) <= -abs(self.max_daily_loss_usd):
            return False, f"daily loss cap reached ({daily_loss_usd:.2f} <= -{abs(self.max_daily_loss_usd):.2f})", details
        if int(trades_today or 0) >= self.max_trades_per_day:
            details["daily_trade_limit_observed"] = True
            details["daily_trade_limit_reason"] = f"daily trade limit observed ({trades_today}/{self.max_trades_per_day})"
        same_symbol_open = [pos for pos in open_positions if _row_symbol(pos) == symbol]
        details["open_trades_for_symbol"] = len(same_symbol_open)
        if len(open_positions) >= self.max_open_trades_total:
            return False, f"max open trades total reached ({len(open_positions)}/{self.max_open_trades_total})", details
        if len(same_symbol_open) >= self.max_open_trades_per_symbol:
            return False, f"max open trades for symbol reached ({symbol}: {len(same_symbol_open)}/{self.max_open_trades_per_symbol})", details
        class_open = [pos for pos in open_positions if _row_asset_class(pos) == asset_class]
        details["open_trades_for_asset_class"] = len(class_open)
        class_limit = int(self.max_asset_class_exposure.get(asset_class, self.max_open_trades_total))
        if class_limit > 0 and len(class_open) >= class_limit:
            return False, f"asset class exposure limit reached ({asset_class}: {len(class_open)}/{class_limit})", details
        correlated = [pos for pos in class_open if _row_direction(pos) == side]
        details["correlated_same_direction"] = len(correlated)
        if side and len(correlated) >= self.max_correlated_same_direction:
            return False, f"correlated same-direction limit reached ({asset_class} {side}: {len(correlated)}/{self.max_correlated_same_direction})", details
        symbol_losses = [row for row in closed_trades if str(row.get("sym") or row.get("symbol") or "").upper() == symbol and _trade_is_loss(row)]
        details["symbol_losses_today"] = len(symbol_losses)
        if len(symbol_losses) >= self.max_symbol_losses_per_day:
            details["symbol_loss_limit_observed"] = True
            details["symbol_loss_limit_reason"] = f"symbol daily loss limit observed ({symbol}: {len(symbol_losses)}/{self.max_symbol_losses_per_day})"
        engine_losses = [row for row in closed_trades if _trade_engine(row) == engine and _trade_is_loss(row)]
        details["engine_losses_today"] = len(engine_losses)
        if len(engine_losses) >= self.max_engine_losses_per_day:
            details["engine_loss_limit_observed"] = True
            details["engine_loss_limit_reason"] = f"engine daily loss limit observed ({engine}: {len(engine_losses)}/{self.max_engine_losses_per_day})"
        details["loss_limit_policy"] = "observe_only"
        streak = _consecutive_losses(closed_trades)
        details["consecutive_losses_today"] = streak
        details["risk_multiplier"] = self.risk_multiplier(streak)
        return True, "OK", details


class CostModel:
    def estimate_trade_cost(self, symbol: str, side: str, entry: float, stop: float, target: float,
                            volume: float = 1.0, *, spread_price: float = 0.0, slippage_price: float = 0.0,
                            commission_usd: float = 0.0, swap_usd: float = 0.0, contract_size: float = 1.0,
                            expected_profit_usd: float | None = None) -> dict[str, float]:
        volume = max(_safe_float(volume), 0.0)
        contract_size = max(_safe_float(contract_size, 1.0), 0.0)
        spread_cost_usd = abs(_safe_float(spread_price)) * volume * contract_size
        slippage_cost_usd = abs(_safe_float(slippage_price)) * volume * contract_size
        commission_usd = max(_safe_float(commission_usd), 0.0)
        swap_usd = max(_safe_float(swap_usd), 0.0)
        target_missing = _safe_float(entry) <= 0.0 or _safe_float(target) <= 0.0 or _safe_float(entry) == _safe_float(target)
        if expected_profit_usd is None:
            expected_profit_usd = None if target_missing else abs(_safe_float(target) - _safe_float(entry)) * volume * contract_size
        expected_profit_value = max(_safe_float(expected_profit_usd), 0.0) if expected_profit_usd is not None else 0.0
        total_cost_usd = spread_cost_usd + slippage_cost_usd + commission_usd + swap_usd
        cost_to_target_pct = (total_cost_usd / expected_profit_value * 100.0) if expected_profit_value > 0 else None
        return {
            "spread_cost_usd": round(spread_cost_usd, 6),
            "slippage_cost_usd": round(slippage_cost_usd, 6),
            "commission_usd": round(commission_usd, 6),
            "swap_usd": round(swap_usd, 6),
            "total_cost_usd": round(total_cost_usd, 6),
            "expected_profit_usd": round(expected_profit_value, 6) if expected_profit_usd is not None else None,
            "cost_to_target_pct": round(cost_to_target_pct, 6) if cost_to_target_pct is not None else None,
            "cost_to_target_reason": "" if cost_to_target_pct is not None else "target missing",
        }

    def cost_gate(self, cost: dict[str, float], score: float) -> tuple[bool, str, float, dict[str, Any]]:
        adjusted_score = _safe_float(score)
        raw_pct = cost.get("cost_to_target_pct")
        if raw_pct is None:
            details = {
                "cost_to_target_pct": None,
                "cost_to_target_status": "unavailable",
                "cost_to_target_reason": cost.get("cost_to_target_reason") or "target missing",
                "score_before": adjusted_score,
                "score_after": adjusted_score,
            }
            return True, "cost-to-target unavailable; gate skipped", adjusted_score, details
        pct = _safe_float(raw_pct, 999.0)
        details = {"cost_to_target_pct": pct, "cost_to_target_status": "calculated", "score_before": adjusted_score, "score_after": adjusted_score}
        if pct > 25.0:
            return False, f"cost-to-target too high ({pct:.2f}% > 25.00%)", adjusted_score, details
        if pct > 15.0:
            adjusted_score = max(0.0, adjusted_score - 5.0)
            details["score_after"] = adjusted_score
            details["score_penalty"] = -5.0
            return True, f"cost elevated ({pct:.2f}%); score reduced by 5", adjusted_score, details
        return True, "OK", adjusted_score, details


class ExecutionQualityEngine:
    def pre_order_check(self, symbol: str, side: str, entry: float, sl: float, tp: float, volume: float, *,
                        market_open: bool = True, duplicate_ok: bool = True, tick_ok: bool = True,
                        lot_ok: bool = True, stop_ok: bool = True,
                        price_moved_too_far: bool = False) -> tuple[bool, str, dict[str, Any]]:
        side = str(side or "").upper()
        details = {
            "symbol": symbol, "side": side, "entry": entry, "sl": sl, "tp": tp, "volume": volume,
            "market_open": bool(market_open), "duplicate_ok": bool(duplicate_ok), "tick_ok": bool(tick_ok),
            "lot_ok": bool(lot_ok), "stop_ok": bool(stop_ok), "price_moved_too_far": bool(price_moved_too_far),
        }
        if not market_open:
            return False, "market is closed before order", details
        if not duplicate_ok:
            return False, "duplicate order check failed before order", details
        if not tick_ok:
            return False, "tick execution check failed before order", details
        if not lot_ok or _safe_float(volume) <= 0:
            return False, "lot size invalid before order", details
        if not stop_ok or _safe_float(sl) <= 0 or _safe_float(tp) <= 0:
            return False, "stop/target invalid before order", details
        if side == "BUY" and not (_safe_float(sl) < _safe_float(entry) < _safe_float(tp)):
            return False, "BUY SL/TP geometry invalid before order", details
        if side == "SELL" and not (_safe_float(tp) < _safe_float(entry) < _safe_float(sl)):
            return False, "SELL SL/TP geometry invalid before order", details
        if price_moved_too_far:
            details["quality_filter_observed"] = "price_extension_timing"
            return True, "NOT_QUALIFIED price extension observed; final execution advisory", details
        return True, "OK", details

    def record_result(self, requested_price: float, filled_price: float, status: str, ticket: str = "") -> dict[str, Any]:
        return {
            "requested_price": _safe_float(requested_price),
            "filled_price": _safe_float(filled_price),
            "slippage": _safe_float(filled_price) - _safe_float(requested_price),
            "fill_time": _utc_now().isoformat(),
            "execution_status": str(status or ""),
            "ticket": str(ticket or ""),
        }


class AnalyticsEngine:
    def __init__(self, export_root: str | Path = "/opt/cipherfx_mt5/state/systematic_exports"):
        self.export_root = Path(export_root)

    def day_dir(self, when: datetime | None = None) -> Path:
        when = when or _utc_now()
        path = self.export_root / when.strftime("%Y-%m-%d")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _append(self, filename: str, row: dict[str, Any]) -> None:
        path = self.day_dir() / filename
        exists = path.exists()
        payload = {field: row.get(field, "") for field in CSV_FIELDS}
        with path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
            if not exists:
                writer.writeheader()
            writer.writerow(payload)

    def _base_row(self, **kwargs: Any) -> dict[str, Any]:
        row = {field: "" for field in CSV_FIELDS}
        row["timestamp"] = kwargs.pop("timestamp", _utc_now().isoformat())
        row.update(kwargs)
        return row

    def record_signal(self, **kwargs: Any) -> None:
        self._append("signals.csv", self._base_row(**kwargs))

    def record_pending_setup(self, **kwargs: Any) -> None:
        self._append("pending_setups.csv", self._base_row(**kwargs))

    def record_execution(self, **kwargs: Any) -> None:
        self._append("executions.csv", self._base_row(**kwargs))

    def record_closed_trade(self, **kwargs: Any) -> None:
        self._append("closed_trades.csv", self._base_row(**kwargs))

    def record_blocked_signal(self, **kwargs: Any) -> None:
        self._append("blocked_signals.csv", self._base_row(**kwargs))

    def write_summary(self, rows: Iterable[dict[str, Any]] = ()) -> Path:
        path = self.day_dir() / "analytics_summary.csv"
        summary_rows = list(rows or []) or [{"metric": "generated_at", "value": _utc_now().isoformat()}]
        fields = sorted({key for row in summary_rows for key in row.keys()})
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(summary_rows)
        return path

    def calculate_expectancy(self, trades: Iterable[dict[str, Any]]) -> float:
        values = [_safe_float(row.get("R_result") if isinstance(row, dict) else row) for row in trades]
        return round(sum(values) / len(values), 6) if values else 0.0

    def calculate_profit_factor(self, trades: Iterable[dict[str, Any]]) -> float:
        profits = [_safe_float(row.get("profit_usd") or row.get("realized") or 0.0) for row in trades]
        gains = sum(v for v in profits if v > 0)
        losses = abs(sum(v for v in profits if v < 0))
        if losses <= 0:
            return float("inf") if gains > 0 else 0.0
        return round(gains / losses, 6)

    def max_drawdown(self, trades: Iterable[dict[str, Any]]) -> float:
        equity = 0.0
        peak = 0.0
        drawdown = 0.0
        for row in trades:
            equity += _safe_float(row.get("profit_usd") or row.get("realized") or 0.0)
            peak = max(peak, equity)
            drawdown = min(drawdown, equity - peak)
        return round(abs(drawdown), 6)

    def calculate_symbol_health(self, trades: Iterable[dict[str, Any]], symbol: str) -> dict[str, Any]:
        rows = [row for row in trades if str(row.get("symbol") or row.get("sym") or "").upper() == str(symbol or "").upper()]
        wins = sum(1 for row in rows if _safe_float(row.get("profit_usd") or row.get("realized") or 0.0) > 0)
        return {
            "symbol": str(symbol or "").upper(), "trade_count": len(rows),
            "win_rate": round((wins / len(rows) * 100.0), 4) if rows else 0.0,
            "expectancy": self.calculate_expectancy(rows),
            "profit_factor": self.calculate_profit_factor(rows),
            "max_drawdown": self.max_drawdown(rows),
        }

    def calculate_engine_health(self, trades: Iterable[dict[str, Any]], engine: str) -> dict[str, Any]:
        rows = [row for row in trades if str(row.get("engine") or "") == str(engine or "")]
        return {
            "engine": str(engine or ""), "trade_count": len(rows),
            "expectancy": self.calculate_expectancy(rows),
            "profit_factor": self.calculate_profit_factor(rows),
            "max_drawdown": self.max_drawdown(rows),
        }


class SymbolHealthManager:
    def __init__(self, state_path: str | Path = "/opt/cipherfx_mt5/state/systematic_disabled.json"):
        self.state_path = Path(state_path)
        self.state = self._load()

    def _load(self) -> dict[str, Any]:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return {"symbols": {}, "engines": {}}

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self.state, indent=2, sort_keys=True), encoding="utf-8")

    def is_symbol_disabled(self, symbol: str) -> tuple[bool, str]:
        item = self.state.setdefault("symbols", {}).get(str(symbol or "").upper())
        if not isinstance(item, dict):
            return False, "OK"
        return bool(item.get("disabled")), str(item.get("reason") or "symbol disabled by health manager")

    def disable_symbol(self, symbol: str, reason: str) -> None:
        self.state.setdefault("symbols", {})[str(symbol or "").upper()] = {
            "disabled": True, "reason": str(reason or ""), "disabled_at": _utc_now().isoformat(),
        }
        self._save()

    def evaluate_symbol(self, symbol: str, metrics: dict[str, Any]) -> tuple[bool, str]:
        count = _safe_int(metrics.get("trade_count"))
        if count < 50:
            return False, "minimum symbol sample not reached"
        expectancy = _safe_float(metrics.get("expectancy"))
        profit_factor = _safe_float(metrics.get("profit_factor"))
        max_drawdown = _safe_float(metrics.get("max_drawdown"))
        win_rate = _safe_float(metrics.get("win_rate"))
        avg_cost_pct = _safe_float(metrics.get("avg_cost_to_profit_pct"))
        if expectancy < 0:
            self.disable_symbol(symbol, "negative expectancy")
            return True, "negative expectancy"
        if profit_factor < 1.10:
            self.disable_symbol(symbol, "profit factor below 1.10")
            return True, "profit factor below 1.10"
        if max_drawdown > _safe_float(metrics.get("max_allowed_drawdown"), 1000.0):
            self.disable_symbol(symbol, "max drawdown above allowed")
            return True, "max drawdown above allowed"
        if avg_cost_pct > 30.0:
            self.disable_symbol(symbol, "average cost above 30% of average profit")
            return True, "average cost above 30% of average profit"
        if win_rate < 40.0 and _safe_float(metrics.get("avg_r")) < 1.5:
            self.disable_symbol(symbol, "win rate below 40% without compensating average R")
            return True, "win rate below 40% without compensating average R"
        return False, "OK"

    def evaluate_engine(self, engine: str, metrics: dict[str, Any]) -> tuple[str, str]:
        count = _safe_int(metrics.get("trade_count"))
        if count < 100:
            return "normal", "minimum engine sample not reached"
        expectancy = _safe_float(metrics.get("expectancy"))
        profit_factor = _safe_float(metrics.get("profit_factor"))
        if profit_factor < 1.0:
            action = "pause"
            reason = "engine profit factor below 1.0"
        elif expectancy < 0:
            action = "reduce_risk_50"
            reason = "engine expectancy negative"
        else:
            return "normal", "OK"
        self.state.setdefault("engines", {})[str(engine or "")] = {"action": action, "reason": reason, "updated_at": _utc_now().isoformat()}
        self._save()
        return action, reason


class WalkForwardValidator:
    def evaluate(self, in_sample: dict[str, Any], out_of_sample: dict[str, Any]) -> dict[str, Any]:
        metrics = ["win_rate", "profit_factor", "expectancy"]
        degradation: dict[str, float] = {}
        overfit = False
        for metric in metrics:
            is_value = _safe_float(in_sample.get(metric))
            oos_value = _safe_float(out_of_sample.get(metric))
            drop = 0.0 if is_value <= 0 else max(0.0, (is_value - oos_value) / is_value * 100.0)
            degradation[metric] = round(drop, 4)
            if drop > 40.0:
                overfit = True
        return {"overfit": overfit, "degradation_pct": degradation, "rule": "OOS performance drop > 40% marks overfit"}


class MonteCarloRiskEngine:
    def simulate(self, r_multiples: Iterable[float], iterations: int = 1000, seed: int | None = 17,
                 daily_loss_r: float = 20.0) -> dict[str, Any]:
        values = [_safe_float(v) for v in r_multiples] or [0.0]
        rnd = random.Random(seed)
        drawdowns: list[float] = []
        max_losing_streaks: list[int] = []
        daily_loss_hits = 0
        ruin_hits = 0
        sequence_len = len(values)
        for _ in range(max(1, int(iterations))):
            equity = 0.0
            peak = 0.0
            max_dd = 0.0
            streak = 0
            max_streak = 0
            for _n in range(sequence_len):
                r = rnd.choice(values)
                equity += r
                peak = max(peak, equity)
                max_dd = min(max_dd, equity - peak)
                if r < 0:
                    streak += 1
                    max_streak = max(max_streak, streak)
                else:
                    streak = 0
            drawdowns.append(abs(max_dd))
            max_losing_streaks.append(max_streak)
            if abs(max_dd) >= daily_loss_r:
                daily_loss_hits += 1
            if equity <= -daily_loss_r * 3:
                ruin_hits += 1
        dd_sorted = sorted(drawdowns)
        p95_index = min(len(dd_sorted) - 1, int(len(dd_sorted) * 0.95))
        losing_streak_probability = sum(1 for item in max_losing_streaks if item >= 5) / len(max_losing_streaks)
        daily_loss_probability = daily_loss_hits / len(drawdowns)
        ruin_probability = ruin_hits / len(drawdowns)
        recommended_reduction = daily_loss_probability > 0.20 or ruin_probability > 0.05
        return {
            "iterations": max(1, int(iterations)),
            "max_drawdown_estimate_r": round(dd_sorted[p95_index], 6),
            "average_drawdown_r": round(statistics.mean(drawdowns) if drawdowns else 0.0, 6),
            "losing_streak_probability": round(losing_streak_probability, 6),
            "daily_loss_probability": round(daily_loss_probability, 6),
            "ruin_probability": round(ruin_probability, 6),
            "safe_risk_multiplier": 0.5 if recommended_reduction else 1.0,
            "current_risk_warning": bool(recommended_reduction),
            "recommended_reduction": "reduce risk by 50%" if recommended_reduction else "none",
        }


class SessionManager:
    def session_info(self, symbol: str, asset_class: str, now: datetime | None = None) -> dict[str, Any]:
        now = now or _utc_now()
        sast = now + timedelta(hours=2)
        minutes = sast.hour * 60 + sast.minute
        weekday = sast.weekday()
        asset_class = _canonical_asset_class(asset_class)
        symbol = str(symbol or "").upper()
        if weekday >= 5:
            return {"session": "WEEKEND", "session_score": 0, "reason": "weekend"}
        london = 10 * 60 <= minutes < 19 * 60 + 30
        ny = 15 * 60 + 20 <= minutes < 23 * 60
        overlap = london and ny
        if asset_class == "forex":
            if overlap:
                return {"session": "LONDON_NY_OVERLAP", "session_score": 10, "reason": "best forex liquidity"}
            if london or ny:
                return {"session": "LONDON" if london else "NY", "session_score": 8, "reason": "active forex liquidity"}
            if symbol.endswith(("JPY", "AUD", "NZD")):
                return {"session": "ASIA_ALLOWED", "session_score": 5, "reason": "Asia allowed for JPY/AUD/NZD"}
            return {"session": "LOW_LIQUIDITY", "session_score": 2, "reason": "dead forex hours"}
        if asset_class == "index":
            if ny or london:
                return {"session": "INDEX_CASH_WINDOW", "session_score": 8 if overlap else 6, "reason": "main index liquidity window"}
            return {"session": "INDEX_CHOP_RISK", "session_score": 2, "reason": "outside main index window"}
        if asset_class == "metal":
            if overlap:
                return {"session": "METALS_OVERLAP", "session_score": 9, "reason": "best metals liquidity"}
            if london or ny:
                return {"session": "METALS_ACTIVE", "session_score": 7, "reason": "active metals session"}
            return {"session": "METALS_LOW_LIQUIDITY", "session_score": 2, "reason": "avoid low-liquidity metals"}
        return {"session": "UNKNOWN", "session_score": 0, "reason": "unknown asset class"}


class RegimeEngine:
    def detect(self, sig: dict[str, Any], market: str = "") -> dict[str, Any]:
        adx = _safe_float(sig.get("adx"))
        atr = _safe_float(sig.get("atr"))
        price = _safe_float(sig.get("price"))
        atr_pct = (atr / price * 100.0) if price > 0 else 0.0
        vwap_z = abs(_safe_float(sig.get("vwap_z")))
        spread_atr = _safe_float(sig.get("spread_signal_atr") or sig.get("spread_atr"))
        if spread_atr > 0.30:
            return {"regime": "NEWS_RISK", "regime_score": -10, "reason": "spread behaviour resembles news/liquidity risk"}
        if atr_pct > 0.45 or adx >= 38:
            return {"regime": "HIGH_VOLATILITY", "regime_score": -5, "reason": "high ATR/ADX"}
        if adx >= 25:
            return {"regime": "TREND", "regime_score": 5, "reason": "ADX trend conditions"}
        if vwap_z >= 1.8:
            return {"regime": "MEAN_REVERSION", "regime_score": 4, "reason": "extended VWAP z-score"}
        if atr_pct < 0.04 and adx < 16:
            return {"regime": "LOW_VOLATILITY", "regime_score": -5, "reason": "low ATR and weak ADX"}
        if adx < 18:
            return {"regime": "CHOP", "regime_score": -4, "reason": "weak trend/chop"}
        return {"regime": "BREAKOUT", "regime_score": 3, "reason": "neutral-to-expanding setup"}


class StrategyScoreExplainer:
    def explain(self, sig: dict[str, Any], status: str, reason: str) -> str:
        symbol = str(sig.get("symbol") or "")
        side = str(sig.get("direction") or "")
        head = " ".join(part for part in (symbol, side) if part) or "signal"
        return (
            f"{head} engine={sig.get('engine') or '-'} "
            f"strategy_before={sig.get('strategy_before') or sig.get('strategy') or '-'} "
            f"strategy_after={sig.get('strategy_after') or sig.get('strategy_family') or '-'} "
            f"regime={sig.get('regime') or '-'} session={sig.get('session') or '-'} "
            f"score_before={sig.get('score_before') or sig.get('score') or 0} "
            f"score_after={sig.get('score_after') or sig.get('score') or 0} "
            f"min_score={sig.get('min_score') or 0} adx={sig.get('adx') or 0} "
            f"spread={sig.get('spread_signal') or sig.get('spread_pct') or 0} "
            f"cost_to_target={sig.get('cost_to_target_pct') or 0}% status={status} reason={reason}"
        )


class SystematicTradingEngine:
    def __init__(self, export_root: str | Path = "/opt/cipherfx_mt5/state/systematic_exports",
                 disabled_state_path: str | Path = "/opt/cipherfx_mt5/state/systematic_disabled.json"):
        self.portfolio = PortfolioRiskManager()
        self.cost = CostModel()
        self.execution_quality = ExecutionQualityEngine()
        self.analytics = AnalyticsEngine(export_root)
        self.health = SymbolHealthManager(disabled_state_path)
        self.walk_forward = WalkForwardValidator()
        self.monte_carlo = MonteCarloRiskEngine()
        self.session = SessionManager()
        self.regime = RegimeEngine()
        self.explainer = StrategyScoreExplainer()
