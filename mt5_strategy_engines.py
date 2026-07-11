#!/usr/bin/env python3
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any

FOREX_ENGINE = "FOREX_ENGINE"
INDEX_ENGINE = "INDEX_ENGINE"
METALS_ENGINE = "METALS_ENGINE"
TEST_MODE_GATING = True
MAX_INDEX_PENALTY_FROM_REGIME_BLOCK = 6.0

GLOBAL_SETTINGS = {
    "MAX_TRADES_PER_DAY": 60,
    "DAILY_LOSS_LIMIT_USD": 1000.0,
    "MAX_OPEN_TRADES_TOTAL": 30,
    "MAX_OPEN_TRADES_PER_SYMBOL": 10,
    "MAX_LOSSES_PER_SYMBOL_PER_DAY": 2,
    "MAX_TOTAL_CONSECUTIVE_LOSSES": 5,
    "BASE_RISK_PER_TRADE_USD": 100.0,
    "MAX_RISK_PER_TRADE_USD": 100.0,
    "REDUCED_RISK_AFTER_LOSS_USD": 10.0,
    "ALLOW_DIRECT_5M_ENTRY": False,
    "REQUIRE_1M_CONFIRMATION": True,
}

SYMBOL_CONFIG: dict[str, dict[str, Any]] = {
    "EURUSD": {"asset_class":"forex","engine":FOREX_ENGINE,"strategy":"trend_pullback_mean_reversion","min_score":85,"mr_adx_soft":28,"mr_adx_hard":34,"trend_adx_max":42,"max_spread_pips":1.5,"max_spread_pips_test":2.0,"max_spread_atr_ratio":0.50,"atr_stop_mult":1.2,"tp_r":1.5,"timeout_minutes":5},
    "GBPUSD": {"asset_class":"forex","engine":FOREX_ENGINE,"strategy":"liquidity_sweep_continuation","min_score":86,"mr_adx_soft":28,"mr_adx_hard":35,"trend_adx_max":44,"max_spread_pips":2.2,"max_spread_pips_test":2.5,"max_spread_atr_ratio":0.55,"atr_stop_mult":1.4,"tp_r":1.6,"timeout_minutes":5},
    "USDJPY": {"asset_class":"forex","engine":FOREX_ENGINE,"strategy":"trend_continuation","min_score":84,"mr_adx_soft":26,"mr_adx_hard":33,"trend_adx_max":45,"max_spread_pips":1.8,"max_spread_pips_test":2.0,"max_spread_atr_ratio":0.50,"atr_stop_mult":1.3,"tp_r":1.5,"timeout_minutes":5},
    "AUDUSD": {"asset_class":"forex","engine":FOREX_ENGINE,"strategy":"mean_reversion_pullback","min_score":84,"mr_adx_soft":28,"mr_adx_hard":34,"trend_adx_max":40,"max_spread_pips":2.5,"max_spread_pips_test":2.6,"max_spread_atr_ratio":0.50,"atr_stop_mult":1.2,"tp_r":1.4,"timeout_minutes":5},
    "NZDUSD": {"asset_class":"forex","engine":FOREX_ENGINE,"strategy":"mean_reversion_pullback","min_score":84,"mr_adx_soft":30,"mr_adx_hard":36,"trend_adx_max":40,"max_spread_pips":2.7,"max_spread_pips_test":2.8,"max_spread_atr_ratio":0.55,"atr_stop_mult":1.2,"tp_r":1.4,"timeout_minutes":5},
    "USDCAD": {"asset_class":"forex","engine":FOREX_ENGINE,"strategy":"mean_reversion","min_score":85,"mr_adx_soft":28,"mr_adx_hard":34,"trend_adx_max":40,"max_spread_pips":2.5,"max_spread_pips_test":2.7,"max_spread_atr_ratio":0.55,"atr_stop_mult":1.3,"tp_r":1.5,"timeout_minutes":5},
    "EURJPY": {"asset_class":"forex","engine":FOREX_ENGINE,"strategy":"trend_continuation","min_score":86,"mr_adx_soft":27,"mr_adx_hard":35,"trend_adx_max":45,"max_spread_pips":2.8,"max_spread_pips_test":3.0,"max_spread_atr_ratio":0.55,"atr_stop_mult":1.4,"tp_r":1.6,"timeout_minutes":5},
    "NAS100": {"asset_class":"index","engine":INDEX_ENGINE,"strategy":"momentum_breakout_pullback","min_score":88,"soft_allow_buffer":3,"adx_ideal_min":22,"adx_ideal_max":42,"adx_danger":50,"max_spread_points":3.0,"max_spread_atr_ratio":0.18,"atr_stop_mult":1.5,"tp_r":1.8,"timeout_minutes":3},
    "US30": {"asset_class":"index","engine":INDEX_ENGINE,"strategy":"trend_pullback","min_score":87,"soft_allow_buffer":3,"adx_ideal_min":20,"adx_ideal_max":40,"adx_danger":48,"max_spread_points":5.0,"max_spread_atr_ratio":0.18,"atr_stop_mult":1.6,"tp_r":1.7,"timeout_minutes":3},
    "SPX500": {"asset_class":"index","engine":INDEX_ENGINE,"strategy":"trend_continuation","min_score":86,"soft_allow_buffer":3,"adx_ideal_min":20,"adx_ideal_max":38,"adx_danger":45,"max_spread_points":2.0,"max_spread_atr_ratio":0.15,"atr_stop_mult":1.4,"tp_r":1.6,"timeout_minutes":3},
    "GER40": {"asset_class":"index","engine":INDEX_ENGINE,"strategy":"breakout_pullback","min_score":88,"soft_allow_buffer":3,"adx_ideal_min":22,"adx_ideal_max":42,"adx_danger":50,"max_spread_points":3.5,"max_spread_atr_ratio":0.18,"atr_stop_mult":1.6,"tp_r":1.8,"timeout_minutes":3},
    "UK100": {"asset_class":"index","engine":INDEX_ENGINE,"strategy":"mean_reversion_trend","min_score":84,"soft_allow_buffer":3,"adx_ideal_min":18,"adx_ideal_max":35,"adx_danger":42,"max_spread_points":2.5,"max_spread_atr_ratio":0.16,"atr_stop_mult":1.3,"tp_r":1.5,"timeout_minutes":3},
    "FRA40": {"asset_class":"index","engine":INDEX_ENGINE,"strategy":"trend_pullback","min_score":85,"soft_allow_buffer":3,"adx_ideal_min":20,"adx_ideal_max":38,"adx_danger":45,"max_spread_points":2.5,"max_spread_atr_ratio":0.16,"atr_stop_mult":1.4,"tp_r":1.6,"timeout_minutes":3},
    "EU50": {"asset_class":"index","engine":INDEX_ENGINE,"strategy":"trend_continuation","min_score":85,"soft_allow_buffer":3,"adx_ideal_min":20,"adx_ideal_max":38,"adx_danger":45,"max_spread_points":2.0,"max_spread_atr_ratio":0.16,"atr_stop_mult":1.4,"tp_r":1.6,"timeout_minutes":3},
    "JP225": {"asset_class":"index","engine":INDEX_ENGINE,"strategy":"trend_continuation","min_score":86,"soft_allow_buffer":3,"adx_ideal_min":20,"adx_ideal_max":40,"adx_danger":48,"max_spread_points":6.0,"max_spread_points_test":8.5,"max_spread_atr_ratio":0.18,"atr_stop_mult":1.5,"tp_r":1.7,"timeout_minutes":3},
    "XAUUSD": {"asset_class":"metal","engine":METALS_ENGINE,"strategy":"trend_pullback_breakout_retest","min_score":87,"adx_soft":30,"adx_hard":38,"trend_adx_max":48,"max_spread_points":0.60,"spread_atr_soft":0.25,"spread_atr_hard":0.45,"spread_atr_extreme":0.60,"max_spread_atr_ratio":0.45,"atr_stop_mult":1.6,"tp_r":1.8,"timeout_minutes":4},
    "XAGUSD": {"asset_class":"metal","engine":METALS_ENGINE,"strategy":"trend_pullback_breakout_retest","min_score":86,"adx_soft":30,"adx_hard":38,"trend_adx_max":48,"max_spread_points":0.09,"spread_atr_soft":0.35,"spread_atr_hard":0.60,"spread_atr_extreme":0.75,"max_spread_atr_ratio":0.60,"atr_stop_mult":1.7,"tp_r":1.9,"timeout_minutes":4},
}

SYMBOL_ALIASES = {
    "GOLD": "XAUUSD",
    "XAUUSDM": "XAUUSD",
    "XAUUSD.M": "XAUUSD",
    "SILVER": "XAGUSD",
    "XAGUSDM": "XAGUSD",
    "XAGUSD.M": "XAGUSD",
}

FOREX_SCORING_TABLE = {
    "h1_bias_aligned": 15,
    "m15_structure": 15,
    "m5_trigger": 20,
    "m1_confirmation": 20,
    "vwap_ema_location": 10,
    "rsi_acceptable": 5,
    "spread_acceptable": 5,
    "atr_healthy": 5,
    "session_quality": 5,
}

INDEX_SCORING_TABLE = {
    "h1_bias_aligned": 20,
    "m15_structure": 15,
    "m5_trigger": 20,
    "m1_confirmation": 20,
    "adx_momentum": 10,
    "atr_expansion": 5,
    "spread_slippage": 5,
    "session_timing": 5,
}

METALS_SCORING_TABLE = {
    "h1_bias_aligned": 18,
    "m15_structure": 15,
    "m5_trigger": 20,
    "m1_confirmation": 20,
    "adx_momentum": 10,
    "atr_healthy": 5,
    "spread_slippage": 5,
    "session_quality": 7,
}


@dataclass(frozen=True)
class EngineDecision:
    allowed: bool
    status: str
    reason: str
    symbol: str
    asset_class: str = ""
    engine: str = ""
    strategy_before: str = ""
    strategy_after: str = ""
    score_before: float = 0.0
    score_after: float = 0.0
    min_score: float = 0.0
    adx: float = 0.0
    adx_reason: str = ""
    engine_trace: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "status": self.status,
            "reason": self.reason,
            "symbol": self.symbol,
            "asset_class": self.asset_class,
            "engine": self.engine,
            "strategy_before": self.strategy_before,
            "strategy_after": self.strategy_after,
            "score_before": round(float(self.score_before), 4),
            "score_after": round(float(self.score_after), 4),
            "min_score": round(float(self.min_score), 4),
            "adx": round(float(self.adx), 4),
            "adx_reason": self.adx_reason,
            "engine_trace": dict(self.engine_trace or {}),
        }


def canonical(symbol: str) -> str:
    sym = str(symbol or "").strip().upper()
    return SYMBOL_ALIASES.get(sym, sym)


def get_symbol_config(symbol: str) -> dict[str, Any] | None:
    return SYMBOL_CONFIG.get(canonical(symbol))


def market_asset_class(market: str) -> str:
    market = str(market or "").lower()
    if market == "forex":
        return "forex"
    if market in {"index_cfd", "index_cfd_eu"}:
        return "index"
    if market == "metal":
        return "metal"
    return "unsupported"


def get_symbol_unit_size(symbol: str, asset_class: str) -> float:
    sym = canonical(symbol)
    asset_class = str(asset_class or "").lower()
    if asset_class == "forex":
        if "JPY" in sym:
            return 0.01
        return 0.0001
    if asset_class == "index":
        return 1.0
    if asset_class == "metal":
        return 1.0
    return 1.0


def _spread_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except Exception:
        return 0.0


def _spread_exceeds(value: float, limit: float) -> bool:
    return limit > 0 and value > limit + 1e-9


def test_mode_gating_enabled() -> bool:
    return _env_bool("MT5_TEST_MODE_GATING", TEST_MODE_GATING)


def _missing_spread_config(symbol: str, score: float, detail: str):
    return False, score, f"{symbol} spread config missing: {detail}", {
        "symbol": symbol,
        "decision": "block",
        "error": detail,
    }


def forex_spread_gate(symbol: str, bid: float, ask: float, atr_price: float, score: float):
    sym = canonical(symbol)
    cfg = get_symbol_config(sym)
    score = _spread_float(score)
    if not cfg:
        return _missing_spread_config(sym, score, "no SYMBOL_CONFIG entry")
    pip = get_symbol_unit_size(sym, "forex")
    spread_price = abs(_spread_float(ask) - _spread_float(bid))
    spread_pips = spread_price / pip if pip > 0 else 0.0
    atr_price = _spread_float(atr_price)
    atr_pips = atr_price / pip if atr_price > 0 and pip > 0 else 0.0
    max_pips = _spread_float(cfg.get("max_spread_pips_test") if test_mode_gating_enabled() else cfg.get("max_spread_pips"))
    if max_pips <= 0:
        max_pips = _spread_float(cfg.get("max_spread_pips"))
    max_ratio = _spread_float(cfg.get("max_spread_atr_ratio"))
    meta = {
        "symbol": sym,
        "engine": FOREX_ENGINE,
        "asset_class": "forex",
        "unit_size": pip,
        "spread_price": spread_price,
        "spread_pips": spread_pips,
        "atr_price": atr_price,
        "atr_pips": atr_pips,
        "spread_to_atr": None,
        "max_spread_pips": max_pips,
        "max_spread_pips_strict": _spread_float(cfg.get("max_spread_pips")),
        "max_spread_pips_test": _spread_float(cfg.get("max_spread_pips_test")),
        "max_spread_atr_ratio": max_ratio,
        "test_mode_gating": test_mode_gating_enabled(),
        "score_before_spread": score,
        "score_after_spread": score,
    }
    if atr_pips <= 0:
        meta["decision"] = "block"
        return False, score, f"{sym} spread block: ATR invalid", meta
    spread_to_atr = spread_pips / atr_pips
    meta["spread_to_atr"] = spread_to_atr
    use_ratio_gate = atr_pips >= 4.0
    meta["used_ratio_gate"] = use_ratio_gate
    if spread_pips <= max_pips:
        meta["decision"] = "allow"
        return (
            True,
            score,
            f"{sym} forex spread OK: {spread_pips:.2f} pips <= {max_pips:.2f} pips",
            meta,
        )
    if test_mode_gating_enabled() and spread_pips <= max_pips * 1.30 and score >= 95:
        adjusted = max(0.0, score - 5.0)
        meta["decision"] = "soft_penalty"
        meta["score_after_spread"] = adjusted
        return (
            adjusted >= float(cfg.get("min_score") or 0.0),
            adjusted,
            f"{sym} forex spread soft penalty: {spread_pips:.2f} pips > {max_pips:.2f}, score {score:.0f}->{adjusted:.0f}",
            meta,
        )
    if spread_pips > max_pips * 1.30:
        meta["decision"] = "block_absolute"
        return (
            False,
            score,
            f"{sym} forex spread hard block: {spread_pips:.2f} pips > {max_pips * 1.30:.2f}",
            meta,
        )
    if use_ratio_gate and _spread_exceeds(spread_to_atr, max_ratio):
        if test_mode_gating_enabled() and score >= 95 and spread_to_atr <= max_ratio * 1.20:
            adjusted = max(0.0, score - 5.0)
            meta["decision"] = "ratio_soft_penalty"
            meta["score_after_spread"] = adjusted
            return (
                adjusted >= float(cfg.get("min_score") or 0.0),
                adjusted,
                f"{sym} forex spread/ATR soft penalty: {spread_to_atr:.2f}, score {score:.0f}->{adjusted:.0f}",
                meta,
            )
        meta["decision"] = "block_ratio"
        return (
            False,
            score,
            f"{sym} forex spread/ATR hard block: {spread_to_atr:.2f}",
            meta,
        )
    meta["decision"] = "allow"
    return (
        True,
        score,
        f"{sym} forex spread OK after ATR floor: {spread_pips:.2f} pips, ATR {atr_pips:.2f} pips",
        meta,
    )


def index_spread_gate(symbol: str, bid: float, ask: float, atr_price: float, score: float):
    sym = canonical(symbol)
    cfg = get_symbol_config(sym)
    score = _spread_float(score)
    if not cfg:
        return _missing_spread_config(sym, score, "no SYMBOL_CONFIG entry")
    spread_points = abs(_spread_float(ask) - _spread_float(bid))
    atr_points = _spread_float(atr_price)
    max_points = _spread_float(cfg.get("max_spread_points_test") if test_mode_gating_enabled() else cfg.get("max_spread_points"))
    if max_points <= 0:
        max_points = _spread_float(cfg.get("max_spread_points"))
    max_ratio = _spread_float(cfg.get("max_spread_atr_ratio"))
    meta = {
        "symbol": sym,
        "engine": INDEX_ENGINE,
        "asset_class": "index",
        "unit_size": get_symbol_unit_size(sym, "index"),
        "spread_points": spread_points,
        "atr_points": atr_points,
        "spread_to_atr": None,
        "max_spread_points": max_points,
        "max_spread_points_strict": _spread_float(cfg.get("max_spread_points")),
        "max_spread_points_test": _spread_float(cfg.get("max_spread_points_test")),
        "max_spread_atr_ratio": max_ratio,
        "test_mode_gating": test_mode_gating_enabled(),
        "score_before_spread": score,
        "score_after_spread": score,
    }
    if atr_points <= 0:
        meta["decision"] = "block"
        return False, score, f"{sym} index spread block: ATR invalid", meta
    spread_to_atr = spread_points / atr_points
    meta["spread_to_atr"] = spread_to_atr
    if _spread_exceeds(spread_points, max_points):
        meta["decision"] = "block"
        return (
            False,
            score,
            f"{sym} index spread block: {spread_points:.2f} points > {max_points:.2f} points, "
            f"ATR {atr_points:.2f} points, spread/ATR {spread_to_atr:.2f}, limit {max_ratio:.2f}",
            meta,
        )
    if _spread_exceeds(spread_to_atr, max_ratio):
        if score >= 92 and spread_to_atr <= max_ratio * 1.15:
            adjusted = score - 5
            meta["decision"] = "soft_penalty"
            meta["score_after_spread"] = adjusted
            return (
                True,
                adjusted,
                f"{sym} index spread soft penalty: {spread_points:.2f} points, ATR {atr_points:.2f} points, "
                f"spread/ATR {spread_to_atr:.2f}, limit {max_ratio:.2f}, score {score:.0f}->{adjusted:.0f}",
                meta,
            )
        meta["decision"] = "block"
        return (
            False,
            score,
            f"{sym} index spread block: {spread_points:.2f} points, ATR {atr_points:.2f} points, "
            f"spread/ATR {spread_to_atr:.2f}, limit {max_ratio:.2f}",
            meta,
        )
    meta["decision"] = "allow"
    return (
        True,
        score,
        f"{sym} index spread OK: {spread_points:.2f} points, ATR {atr_points:.2f} points, "
        f"spread/ATR {spread_to_atr:.2f}, limit {max_ratio:.2f}",
        meta,
    )


def metals_spread_gate(symbol: str, bid: float, ask: float, atr_price: float, score: float):
    sym = canonical(symbol)
    cfg = get_symbol_config(sym)
    score = _spread_float(score)
    if not cfg:
        return _missing_spread_config(sym, score, "no SYMBOL_CONFIG entry")
    spread_points = abs(_spread_float(ask) - _spread_float(bid))
    atr_points = _spread_float(atr_price)
    max_points = _spread_float(cfg.get("max_spread_points"))
    soft_ratio = _spread_float(cfg.get("spread_atr_soft") or cfg.get("max_spread_atr_ratio"))
    hard_ratio = _spread_float(cfg.get("spread_atr_hard") or cfg.get("max_spread_atr_ratio"))
    extreme_ratio = _spread_float(cfg.get("spread_atr_extreme") or hard_ratio)
    meta = {
        "symbol": sym,
        "engine": METALS_ENGINE,
        "asset_class": "metal",
        "unit_size": get_symbol_unit_size(sym, "metal"),
        "spread_points": spread_points,
        "atr_points": atr_points,
        "spread_to_atr": None,
        "max_spread_points": max_points,
        "max_spread_atr_ratio": hard_ratio,
        "spread_atr_soft": soft_ratio,
        "spread_atr_hard": hard_ratio,
        "spread_atr_extreme": extreme_ratio,
        "score_before_spread": score,
        "score_after_spread": score,
    }
    if atr_points <= 0:
        meta["decision"] = "block"
        return False, score, f"{sym} metals spread block: ATR invalid", meta
    spread_to_atr = spread_points / atr_points
    meta["spread_to_atr"] = spread_to_atr
    if _spread_exceeds(spread_points, max_points):
        meta["decision"] = "block"
        return (
            False,
            score,
            f"{sym} metal absolute spread too wide: {spread_points:.3f} > {max_points:.3f}",
            meta,
        )
    if spread_to_atr >= extreme_ratio:
        meta["decision"] = "block_extreme"
        return (
            False,
            score,
            f"{sym} metal spread extreme: {spread_to_atr:.2f} >= {extreme_ratio:.2f}",
            meta,
        )
    if spread_to_atr >= hard_ratio:
        adjusted = max(0.0, score - 12.0)
        meta["decision"] = "hard_penalty" if adjusted >= float(cfg.get("min_score") or 0.0) else "block_after_hard_penalty"
        meta["score_after_spread"] = adjusted
        allowed = adjusted >= float(cfg.get("min_score") or 0.0)
        return (
            allowed,
            adjusted,
            f"{sym} metal spread hard penalty: {spread_to_atr:.2f}, score {score:.0f}->{adjusted:.0f}",
            meta,
        )
    if spread_to_atr >= soft_ratio:
        adjusted = max(0.0, score - 6.0)
        meta["decision"] = "soft_penalty" if adjusted >= float(cfg.get("min_score") or 0.0) else "block_after_soft_penalty"
        meta["score_after_spread"] = adjusted
        allowed = adjusted >= float(cfg.get("min_score") or 0.0)
        return (
            allowed,
            adjusted,
            f"{sym} metal spread soft penalty: {spread_to_atr:.2f}, score {score:.0f}->{adjusted:.0f}",
            meta,
        )
    meta["decision"] = "allow"
    return (
        True,
        score,
        f"{sym} metals spread OK: {spread_points:.2f}, ATR {atr_points:.2f}, "
        f"spread/ATR {spread_to_atr:.2f}, soft {soft_ratio:.2f}, hard {hard_ratio:.2f}",
        meta,
    )


def spread_gate_for_engine(symbol: str, engine: str, bid: float, ask: float, atr_price: float, score: float):
    engine = str(engine or "")
    if engine == FOREX_ENGINE:
        return forex_spread_gate(symbol, bid, ask, atr_price, score)
    if engine == INDEX_ENGINE:
        return index_spread_gate(symbol, bid, ask, atr_price, score)
    if engine == METALS_ENGINE:
        return metals_spread_gate(symbol, bid, ask, atr_price, score)
    return True, _spread_float(score), "spread gate skipped: unsupported engine", {"decision": "skip", "engine": engine}


def route_symbol(symbol: str, market: str) -> EngineDecision:
    sym = canonical(symbol)
    cfg = get_symbol_config(sym)
    if not cfg:
        return EngineDecision(False, "BLOCKED", "symbol has no two-engine config", sym)
    expected_asset = str(cfg.get("asset_class") or "")
    actual_asset = market_asset_class(market)
    if actual_asset != expected_asset:
        return EngineDecision(False, "BLOCKED", f"market asset_class mismatch {actual_asset} != {expected_asset}", sym, expected_asset, str(cfg.get("engine") or ""))
    engine = str(cfg.get("engine") or "")
    if expected_asset == "forex" and engine != FOREX_ENGINE:
        return EngineDecision(False, "BLOCKED", "forex symbol not routed to FOREX_ENGINE", sym, expected_asset, engine)
    if expected_asset == "index" and engine != INDEX_ENGINE:
        return EngineDecision(False, "BLOCKED", "index symbol not routed to INDEX_ENGINE", sym, expected_asset, engine)
    if expected_asset == "metal" and engine != METALS_ENGINE:
        return EngineDecision(False, "BLOCKED", "metal symbol not routed to METALS_ENGINE", sym, expected_asset, engine)
    if engine not in {FOREX_ENGINE, INDEX_ENGINE, METALS_ENGINE}:
        return EngineDecision(False, "BLOCKED", f"unknown strategy engine {engine}", sym, expected_asset, engine)
    return EngineDecision(True, "RAW", "OK", sym, expected_asset, engine)


def current_strategy_name(sig: dict[str, Any]) -> str:
    return str((sig or {}).get("strategy") or (sig or {}).get("mode") or "").strip().upper()


def _score_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    try:
        return float(value or 0.0) > 0.0
    except Exception:
        return bool(value)


def apply_forex_adx_logic(symbol: str, strategy: str, adx: float, score: float, h1_bias_aligned: bool, m15_structure_aligned: bool):
    cfg = SYMBOL_CONFIG[canonical(symbol)]
    strategy = str(strategy or cfg["strategy"])
    if strategy in ["mean_reversion", "mean_reversion_pullback", "trend_pullback_mean_reversion"]:
        if adx >= float(cfg["mr_adx_hard"]):
            return strategy, score, False, f"MR ADX quality warning {adx} >= {cfg['mr_adx_hard']}"
        if adx >= float(cfg["mr_adx_soft"]):
            score -= 8
            if h1_bias_aligned and m15_structure_aligned:
                return "trend_pullback", score, False, f"MR rerouted to trend_pullback ADX {adx}"
            return strategy, score, False, f"MR ADX soft penalty {adx}"
    if strategy in ["trend_pullback", "trend_continuation", "liquidity_sweep_continuation"]:
        if adx > float(cfg["trend_adx_max"]):
            score -= 10
            return strategy, score, False, f"Trend ADX extended penalty {adx}"
    return strategy, score, False, "ADX OK"


def apply_metals_adx_logic(symbol: str, strategy: str, adx: float, score: float, h1_bias_aligned: bool, m15_structure_aligned: bool):
    cfg = SYMBOL_CONFIG[canonical(symbol)]
    strategy = str(strategy or cfg["strategy"])
    if adx >= float(cfg["adx_hard"]):
        return strategy, score, False, f"Metals ADX quality warning {adx} >= {cfg['adx_hard']}"
    if adx >= float(cfg["adx_soft"]):
        score -= 6
        if h1_bias_aligned and m15_structure_aligned:
            return "trend_pullback", score, False, f"Metals ADX soft penalty + trend reroute {adx}"
        return strategy, score, False, f"Metals ADX soft penalty {adx}"
    if adx > float(cfg["trend_adx_max"]):
        score -= 10
        return strategy, score, False, f"Metals trend ADX extended penalty {adx}"
    return strategy, score, False, "ADX OK"


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def mean_reversion_zscore_gate(symbol: str, zscore: float, zscore_min: float, score: float):
    sym = canonical(symbol)
    cfg = get_symbol_config(sym) or {}
    zscore = abs(_spread_float(zscore))
    zscore_min = _spread_float(zscore_min)
    score = _spread_float(score)
    min_score = float(cfg.get("min_score") or 0.0)
    meta = {
        "symbol": sym,
        "zscore": zscore,
        "zscore_min": zscore_min,
        "score_before_zscore": score,
        "score_after_zscore": score,
        "min_score": min_score,
        "test_mode_gating": test_mode_gating_enabled(),
    }
    if zscore >= zscore_min:
        meta["decision"] = "allow"
        return True, score, "zscore OK", meta
    if test_mode_gating_enabled() and zscore >= zscore_min - 0.10:
        adjusted = max(0.0, score - 4.0)
        meta["decision"] = "soft_penalty" if adjusted >= min_score else "block_after_soft_penalty"
        meta["score_after_zscore"] = adjusted
        return adjusted >= min_score, adjusted, f"zscore soft penalty: {zscore:.2f} < {zscore_min:.2f}", meta
    meta["decision"] = "block"
    return False, score, f"zscore hard block: {zscore:.2f} < {zscore_min:.2f}", meta


def apply_index_adx_logic(symbol: str, strategy: str, adx: float, score: float, h1_bias_aligned: bool = False, m15_structure_aligned: bool = False, regime: str = "", session_score: float | None = None, volatility_expansion: bool = False):
    cfg = SYMBOL_CONFIG[canonical(symbol)]
    strategy = str(strategy or cfg["strategy"])
    regime = str(regime or "").upper()
    score_before = float(score or 0.0)
    trace = {
        "regime": regime,
        "score_before": score_before,
        "regime_penalty": 0.0,
        "adx_penalty": 0.0,
        "session_penalty": 0.0,
        "structure_penalty": 0.0,
        "total_penalty": 0.0,
        "penalty_cap": MAX_INDEX_PENALTY_FROM_REGIME_BLOCK,
        "soft_allow_buffer": float(cfg.get("soft_allow_buffer") or 0.0),
        "soft_allow_eligible": False,
    }
    if adx >= float(cfg["adx_danger"]):
        trace["danger_warning"] = f"Index ADX danger warning {adx} >= {cfg['adx_danger']}"
        trace["adx_penalty"] = -8.0
        trace["total_penalty"] = -8.0
        score = max(0.0, score - 8.0)
        trace["score_after"] = score
        return strategy, score, False, trace["danger_warning"], trace
    if regime == "RANGE":
        trace["regime_penalty"] = -3.0
        if strategy.upper() == "MEAN_REV" and h1_bias_aligned and m15_structure_aligned:
            strategy = "trend_pullback"
        elif not (h1_bias_aligned and m15_structure_aligned and volatility_expansion):
            strategy = "range_rejection"
    if adx < float(cfg["adx_ideal_min"]):
        trace["adx_penalty"] = -5.0 if adx < 12.0 else -3.0
    if session_score is None or float(session_score or 0.0) <= 0.0:
        trace["session_penalty"] = -2.0
    if regime == "RANGE" and "BREAKOUT" in strategy.upper() and not volatility_expansion:
        trace["structure_penalty"] = -3.0
    capped_penalty = max(-MAX_INDEX_PENALTY_FROM_REGIME_BLOCK, sum(
        float(trace[key]) for key in ("regime_penalty", "adx_penalty", "session_penalty", "structure_penalty")
    ))
    if capped_penalty:
        score = max(0.0, score + capped_penalty)
        trace["total_penalty"] = capped_penalty
        trace["score_after"] = score
        return strategy, score, False, f"Index regime/ADX penalty {capped_penalty:.0f} (ADX {adx} < ideal {cfg['adx_ideal_min']})", trace
    if adx > float(cfg["adx_ideal_max"]):
        trace["adx_penalty"] = -8.0
        trace["total_penalty"] = -8.0
        score = max(0.0, score - 8.0)
        trace["score_after"] = score
        return strategy, score, False, f"Index ADX extended penalty {adx} > {cfg['adx_ideal_max']}", trace
    trace["score_after"] = score
    return strategy, score, False, "ADX OK", trace


def apply_engine_policy(symbol: str, market: str, sig: dict[str, Any], details: dict[str, Any] | None = None) -> EngineDecision:
    sym = canonical(symbol)
    route = route_symbol(sym, market)
    if not route.allowed:
        return route
    cfg = SYMBOL_CONFIG[sym]
    details = details or {}
    strategy_before = current_strategy_name(sig)
    strategy_after = str(cfg.get("strategy") or strategy_before)
    score_before = float((sig or {}).get("score") or 0.0)
    score_after = score_before
    adx = float((sig or {}).get("adx") or 0.0)
    h1_bias_aligned = _score_bool((sig or {}).get("h1_bias") or details.get("bias_1h_score") or (sig or {}).get("bias_1h_score"))
    m15_structure_aligned = _score_bool((sig or {}).get("m15_structure") or details.get("setup_15m_score") or (sig or {}).get("setup_15m_score"))
    regime = str((sig or {}).get("regime") or details.get("regime") or "").upper()
    session_score_raw = (sig or {}).get("session_score", details.get("session_score"))
    session_score = None if session_score_raw in {None, ""} else float(session_score_raw or 0.0)
    volatility_expansion = _score_bool(
        (sig or {}).get("volatility_expansion")
        or (sig or {}).get("atr_expansion")
        or details.get("atr_expansion")
        or details.get("atr_expansion_score")
    )
    blocked = False
    engine_trace: dict[str, Any] = {}
    if route.engine == FOREX_ENGINE:
        strategy_after, score_after, blocked, adx_reason = apply_forex_adx_logic(sym, strategy_after, adx, score_after, h1_bias_aligned, m15_structure_aligned)
    elif route.engine == INDEX_ENGINE:
        strategy_after, score_after, blocked, adx_reason, engine_trace = apply_index_adx_logic(
            sym,
            strategy_before or strategy_after,
            adx,
            score_after,
            h1_bias_aligned,
            m15_structure_aligned,
            regime,
            session_score,
            volatility_expansion,
        )
    elif route.engine == METALS_ENGINE:
        strategy_after, score_after, blocked, adx_reason = apply_metals_adx_logic(sym, strategy_after, adx, score_after, h1_bias_aligned, m15_structure_aligned)
    else:
        return EngineDecision(False, "BLOCKED", f"unknown strategy engine {route.engine}", sym, route.asset_class, route.engine)
    min_score = float(cfg.get("min_score") or 0.0)
    if blocked:
        status = "BLOCKED"
        reason = adx_reason
        allowed = False
    elif score_after < min_score:
        buffer = float(cfg.get("soft_allow_buffer") or 0.0)
        soft_allow = (
            route.engine == INDEX_ENGINE
            and _env_bool("MT5_INDEX_SOFT_ALLOW_ENABLE", True)
            and score_before >= 90.0
            and buffer > 0.0
            and score_after >= min_score - buffer
        )
        if soft_allow:
            status = "PENDING_SOFT"
            reason = f"{route.engine} index soft-allow near min score ({score_after:.0f} >= {min_score - buffer:.0f}; min {min_score:.0f})"
            allowed = True
            engine_trace["soft_allow_eligible"] = True
            engine_trace["soft_allow_used"] = True
            engine_trace["soft_allow_reason"] = reason
        else:
            status = "BLOCKED"
            reason = f"{route.engine} score below min_score ({score_after:.0f} < {min_score:.0f})"
            allowed = False
            engine_trace["soft_allow_eligible"] = bool(route.engine == INDEX_ENGINE and score_before >= 90.0 and buffer > 0.0)
            engine_trace["soft_allow_used"] = False
    else:
        status = "RAW"
        reason = "OK"
        allowed = True
    if route.engine == INDEX_ENGINE:
        engine_trace.setdefault("min_score", min_score)
        engine_trace.setdefault("score_before", score_before)
        engine_trace.setdefault("score_after", score_after)
        engine_trace.setdefault("soft_allow_eligible", False)
    return EngineDecision(allowed, status, reason, sym, route.asset_class, route.engine, strategy_before, strategy_after, score_before, score_after, min_score, adx, adx_reason, engine_trace)


ENGINE_RUNTIME_CONTRACTS = {
    "forex": {
        "engine": FOREX_ENGINE,
        "score_function": "calculate_forex_score",
        "score_function_source": "scalping_bot_v4.evaluate_probability",
        "adx_function": "apply_forex_adx_logic",
        "spread_function": "forex_spread_gate",
    },
    "index": {
        "engine": INDEX_ENGINE,
        "score_function": "calculate_index_score",
        "score_function_source": "scalping_bot_v4.evaluate_probability",
        "adx_function": "apply_index_adx_logic",
        "spread_function": "index_spread_gate",
    },
    "metal": {
        "engine": METALS_ENGINE,
        "score_function": "calculate_metals_score",
        "score_function_source": "scalping_bot_v4.evaluate_probability",
        "adx_function": "apply_metals_adx_logic",
        "spread_function": "metals_spread_gate",
    },
}


def engine_contract_for(asset_class: str) -> dict[str, str]:
    return dict(ENGINE_RUNTIME_CONTRACTS.get(str(asset_class or "").lower()) or {})


def assert_engine_isolation(
    symbol: str,
    asset_class: str,
    engine: str,
    score_function: str,
    adx_function: str,
    spread_function: str,
) -> None:
    contract = engine_contract_for(asset_class)
    if not contract:
        raise AssertionError(f"{canonical(symbol)} has unknown asset_class {asset_class!r}")
    checks = {
        "engine": engine,
        "score_function": score_function,
        "adx_function": adx_function,
        "spread_function": spread_function,
    }
    for key, actual in checks.items():
        expected = contract.get(key)
        if actual != expected:
            raise AssertionError(f"{canonical(symbol)} {asset_class} expected {key}={expected}, got {actual}")


def pending_timeout_minutes(symbol: str) -> int:
    cfg = get_symbol_config(symbol) or {}
    return int(float(cfg.get("timeout_minutes") or 5))


def pip_size(symbol: str) -> float:
    return get_symbol_unit_size(symbol, "forex")


def valid_1m_confirmation(df, side: str, engine: str):
    if df is None or len(df) < 3:
        return False, "1M history insufficient", 0.0
    last = df.iloc[-1]
    prev = df.iloc[-2]
    close = float(last.get("Close", 0.0) or 0.0)
    open_ = float(last.get("Open", 0.0) or 0.0)
    prev_close = float(prev.get("Close", 0.0) or 0.0)
    high = float(last.get("High", close) or close)
    low = float(last.get("Low", close) or close)
    side = str(side or "").upper()
    body = abs(close - open_)
    rng = max(abs(high - low), 1e-12)
    body_frac = body / rng
    close_location = (close - low) / rng
    strict_engine = engine in {INDEX_ENGINE, METALS_ENGINE}
    if side == "BUY":
        ok = close > open_ and close >= prev_close
        if strict_engine:
            ok = ok and body_frac >= 0.25 and close_location >= 0.60
    elif side == "SELL":
        ok = close < open_ and close <= prev_close
        if strict_engine:
            ok = ok and body_frac >= 0.25 and close_location <= 0.40
    else:
        return False, "invalid side for 1M confirmation", 0.0
    if body <= 0.0:
        ok = False
    score = 20.0 if ok else 0.0
    reason = "1M confirmation OK" if ok else (
        f"waiting for 1M confirmation: side={side} body_frac={body_frac:.2f} close_location={close_location:.2f}"
    )
    return ok, reason, score


def tick_execution_check(symbol: str, engine: str, bid: float, ask: float, setup_price: float, atr: float = 0.0, score: float = 0.0):
    if bid <= 0 or ask <= 0 or ask < bid:
        return False, "invalid tick before execution", {"bid": bid, "ask": ask}
    sym = canonical(symbol)
    spread = ask - bid
    mid = (bid + ask) / 2.0
    info = {"bid": bid, "ask": ask, "spread": spread, "mid": mid}
    spread_ok, adjusted_score, spread_reason, spread_info = spread_gate_for_engine(sym, engine, bid, ask, atr, score)
    info.update(spread_info)
    info["score_after_spread"] = adjusted_score
    info["spread_reason"] = spread_reason
    if not spread_ok:
        return False, spread_reason.replace("spread block", "spread block at execution"), info
    if setup_price > 0 and atr > 0:
        slippage_atr = abs(mid - setup_price) / atr
        info["slippage_atr"] = slippage_atr
        if slippage_atr > 0.35:
            return False, f"slippage too high ({slippage_atr:.2f} ATR > 0.35)", info
    return True, "tick execution OK", info


def can_place_trade(
    symbol: str,
    daily_loss_usd: float,
    trades_today: int,
    open_trades_total: int,
    open_trades_for_symbol: int,
    losses_for_symbol_today: int,
    consecutive_losses_today: int,
):
    return True, "OK; legacy helper risk limits are observe-only and not part of live execution"


def calculate_test_risk_usd(daily_loss_usd: float, consecutive_losses_today: int) -> float:
    # Loss streak is observe-only. The daily loss cap and portfolio exposure
    # gates remain the hard controls; it must not silently shrink lots to
    # broker-invalid cent volumes.
    return min(float(GLOBAL_SETTINGS["BASE_RISK_PER_TRADE_USD"]), float(GLOBAL_SETTINGS["MAX_RISK_PER_TRADE_USD"]))
