#!/usr/bin/env python3
from pathlib import Path

import pandas as pd

import mt5_strategy_engines as e


def bullish_m1():
    return pd.DataFrame([
        {"Open": 1.0000, "High": 1.0010, "Low": 0.9995, "Close": 1.0004},
        {"Open": 1.0004, "High": 1.0012, "Low": 1.0000, "Close": 1.0008},
        {"Open": 1.0008, "High": 1.0020, "Low": 1.0006, "Close": 1.0016},
    ])


def main():
    strategy, score, blocked, reason = e.apply_forex_adx_logic(
        "NZDUSD", "mean_reversion_pullback", 28.6, 90.0, False, False
    )
    assert not blocked, reason
    assert score == 90.0, score

    low = e.apply_engine_policy("EURUSD", "forex", {"strategy": "MEAN_REV", "score": 70, "adx": 20}, {})
    assert not low.allowed and "score below min_score" in low.reason, low

    high = e.apply_engine_policy(
        "EURUSD", "forex", {"strategy": "MEAN_REV", "score": 90, "adx": 20}, {"bias_1h_score": 15, "setup_15m_score": 15}
    )
    assert high.allowed and high.engine == e.FOREX_ENGINE and high.status == "RAW", high

    ok, reason, score = e.valid_1m_confirmation(bullish_m1(), "BUY", e.FOREX_ENGINE)
    assert ok and score == 20.0, reason

    ok, reason = e.can_place_trade("EURUSD", -1000.0, 0, 0, 0, 0, 0)
    assert not ok and reason == "daily loss limit reached", reason

    ok, reason = e.can_place_trade("EURUSD", 0.0, 59, 0, 0, 0, 0)
    assert ok, reason
    ok, reason = e.can_place_trade("EURUSD", 0.0, 60, 0, 0, 0, 0)
    assert not ok and reason == "max trades per day reached", reason

    assert e.route_symbol("EURUSD", "forex").engine == e.FOREX_ENGINE
    assert e.route_symbol("NAS100", "index_cfd").engine == e.INDEX_ENGINE
    assert e.route_symbol("XAUUSD", "metal").engine == e.METALS_ENGINE

    aud_spread_ok, aud_score, aud_spread_reason, aud_info = e.forex_spread_gate("AUDUSD", 0.65000, 0.65024, 0.000245, 100.0)
    assert aud_spread_ok, aud_spread_reason
    assert round(aud_info["spread_pips"], 2) == 2.4 and aud_info["used_ratio_gate"] is False, aud_info

    eur_spread_ok, eur_score, eur_spread_reason, eur_info = e.forex_spread_gate("EURUSD", 1.10000, 1.10019, 0.000351, 95.0)
    assert eur_spread_ok, eur_spread_reason
    assert round(eur_info["spread_pips"], 2) == 1.9 and eur_info["used_ratio_gate"] is False, eur_info

    z_ok, z_score, z_reason, z_meta = e.mean_reversion_zscore_gate("EURUSD", 1.19, 1.20, 95.0)
    assert z_ok and z_score == 91.0 and z_meta["decision"] == "soft_penalty", (z_reason, z_meta)

    us30 = e.apply_engine_policy(
        "US30",
        "index_cfd",
        {"strategy": "MEAN_REV", "score": 90, "adx": 16.3, "regime": "RANGE"},
        {"bias_1h_score": 20, "setup_15m_score": 15, "session_score": 5},
    )
    assert us30.allowed and us30.status == "PENDING_SOFT", us30
    assert us30.score_after == 84.0, us30
    assert us30.engine_trace["total_penalty"] == -6.0, us30.engine_trace
    assert us30.engine_trace["soft_allow_eligible"] is True, us30.engine_trace

    nas100 = e.apply_engine_policy(
        "NAS100",
        "index_cfd",
        {"strategy": "MEAN_REV", "score": 90, "adx": 14.0, "regime": "RANGE"},
        {"bias_1h_score": 20, "setup_15m_score": 15, "session_score": 5},
    )
    assert not nas100.allowed and nas100.status == "BLOCKED", nas100
    assert nas100.score_after == 84.0, nas100
    assert nas100.engine_trace["total_penalty"] == -6.0, nas100.engine_trace

    capped = e.apply_engine_policy(
        "NAS100",
        "index_cfd",
        {"strategy": "BREAKOUT", "score": 90, "adx": 10.0, "regime": "RANGE"},
        {},
    )
    assert capped.engine_trace["total_penalty"] == -6.0, capped.engine_trace

    jp_ok, jp_score, jp_reason, jp_info = e.index_spread_gate("JP225", 40000.0, 40008.0, 93.75, 92.0)
    assert jp_ok, jp_reason
    assert jp_info["max_spread_points"] == 8.5, jp_info

    tick_ok, tick_reason, info = e.tick_execution_check("EURUSD", e.FOREX_ENGINE, 1.10000, 1.10010, 1.10000, 0.0010)
    assert tick_ok, tick_reason
    tick_ok, tick_reason, info = e.tick_execution_check("EURUSD", e.FOREX_ENGINE, 1.10000, 1.10030, 1.10000, 0.0010)
    assert not tick_ok and "spread hard block" in tick_reason, tick_reason

    source = Path(__file__).with_name("mt5_bot.py").read_text()
    pending_idx = source.index("pending_ok, pending_reason = self._pending_setup_allows_execution")
    place_idx = source.index("self._place_trade(sym, market, sig, volume, sl, tp, risk_meta)")
    assert pending_idx < place_idx, "pending setup gate must run before order placement"
    assert 'MT5_ALLOW_DIRECT_5M_ENTRY", False' in source

    print("mt5 two-engine model checks passed")


if __name__ == "__main__":
    main()
