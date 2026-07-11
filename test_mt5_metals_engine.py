#!/usr/bin/env python3
from pathlib import Path

import pandas as pd

import mt5_strategy_engines as e


def bullish_m1():
    return pd.DataFrame([
        {"Open": 2000.0, "High": 2001.0, "Low": 1999.0, "Close": 2000.4},
        {"Open": 2000.4, "High": 2002.0, "Low": 2000.0, "Close": 2001.1},
        {"Open": 2001.1, "High": 2004.0, "Low": 2000.8, "Close": 2003.5},
    ])


def main():
    assert e.route_symbol("XAUUSD", "metal").engine == e.METALS_ENGINE
    assert e.route_symbol("XAGUSD", "metal").engine == e.METALS_ENGINE
    assert e.route_symbol("GOLD", "metal").symbol == "XAUUSD"
    assert e.route_symbol("SILVER", "metal").symbol == "XAGUSD"
    assert e.route_symbol("XAUUSD", "metal").engine != e.FOREX_ENGINE
    assert e.route_symbol("XAGUSD", "metal").engine != e.FOREX_ENGINE

    strategy, score, blocked, reason = e.apply_metals_adx_logic("XAUUSD", "trend_pullback_breakout_retest", 32.0, 92.0, True, True)
    assert not blocked and strategy == "trend_pullback" and score == 86.0, reason
    strategy, score, blocked, reason = e.apply_metals_adx_logic("XAUUSD", "trend_pullback_breakout_retest", 38.0, 92.0, True, True)
    assert blocked and "Metals ADX hard block" in reason, reason

    high = e.apply_engine_policy("XAUUSD", "metal", {"strategy": "MEAN_REV", "score": 90, "adx": 24}, {"bias_1h_score": 18, "setup_15m_score": 15})
    assert high.allowed and high.engine == e.METALS_ENGINE and high.status == "RAW", high
    low = e.apply_engine_policy("XAGUSD", "metal", {"strategy": "MEAN_REV", "score": 82, "adx": 24}, {})
    assert not low.allowed and "score below min_score" in low.reason, low

    ok, reason, score = e.valid_1m_confirmation(bullish_m1(), "BUY", e.METALS_ENGINE)
    assert ok and score == 20.0, reason

    tick_ok, tick_reason, info = e.tick_execution_check("XAUUSD", e.METALS_ENGINE, 2000.0, 2000.4, 2000.1, 5.0)
    assert tick_ok, tick_reason
    tick_ok, tick_reason, info = e.tick_execution_check("XAGUSD", e.METALS_ENGINE, 25.000, 25.069, 25.020, 0.14, 94.0)
    assert tick_ok and "metal spread soft penalty" in info["spread_reason"], info
    assert info["score_after_spread"] == 88.0, info

    tick_ok, tick_reason, info = e.tick_execution_check("XAGUSD", e.METALS_ENGINE, 25.000, 25.100, 25.020, 0.14, 94.0)
    assert not tick_ok and "absolute spread too wide" in tick_reason, tick_reason

    tick_ok, tick_reason, info = e.tick_execution_check("XAGUSD", e.METALS_ENGINE, 25.000, 25.080, 25.020, 0.10, 94.0)
    assert not tick_ok and "spread extreme" in tick_reason, tick_reason

    xau_ok, xau_reason, xau_info = e.tick_execution_check("XAUUSD", e.METALS_ENGINE, 2000.0, 2000.4, 2000.1, 1.0, 94.0)
    assert xau_ok and xau_info["spread_atr_extreme"] == 0.60, xau_info

    ok, reason = e.can_place_trade("XAUUSD", -1000.0, 0, 0, 0, 0, 0)
    assert not ok and reason == "daily loss limit reached", reason
    ok, reason = e.can_place_trade("XAUUSD", 0.0, 59, 0, 0, 0, 0)
    assert ok, reason

    source = Path(__file__).with_name("mt5_bot.py").read_text()
    pending_idx = source.index("pending_ok, pending_reason = self._pending_setup_allows_execution")
    place_idx = source.index("self._place_trade(sym, market, sig, volume, sl, tp, risk_meta)")
    assert pending_idx < place_idx, "metals must use pending setup before order placement"
    assert "METALS_ENGINE" in Path(__file__).with_name("mt5_strategy_engines.py").read_text()
    print("mt5 metals engine checks passed")


if __name__ == "__main__":
    main()
