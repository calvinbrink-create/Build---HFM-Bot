#!/usr/bin/env python3
from __future__ import annotations

import tempfile
from pathlib import Path

import mt5_strategy_engines as engines
from mt5_systematic_engine import (
    AnalyticsEngine,
    CostModel,
    METALS_ENGINE,
    MonteCarloRiskEngine,
    PortfolioRiskManager,
    StrategyScoreExplainer,
    SymbolHealthManager,
    WalkForwardValidator,
)


def assert_true(value, msg):
    if not value:
        raise AssertionError(msg)


def assert_false(value, msg):
    if value:
        raise AssertionError(msg)


def test_daily_loss_cap_blocks():
    risk = PortfolioRiskManager()
    ok, reason, _details = risk.can_trade("EURUSD", "FOREX_ENGINE", "BUY", daily_loss_usd=-1000.0)
    assert_false(ok, "daily loss cap must block at -1000")
    assert_true("daily loss cap" in reason, reason)


def test_5m_source_never_direct_and_1m_required():
    source = Path("mt5_bot.py").read_text(encoding="utf-8")
    assert_true('MT5_ALLOW_DIRECT_5M_ENTRY", False' in source, "direct 5M entry must default false")
    assert_true('MT5_REQUIRE_1M_CONFIRMATION", True' in source, "1M confirmation must default true")
    assert_true("_pending_setup_allows_execution" in source, "pending setup gate missing")
    assert_true("_place_trade(sym, market, sig, volume, sl, tp" in source, "place trade call missing")
    assert_true(source.index("_pending_setup_allows_execution") < source.rindex("_place_trade(sym, market, sig, volume, sl, tp"), "pending setup gate must appear before order placement")


def test_engine_routing():
    assert_true(engines.route_symbol("EURUSD", "forex").engine == engines.FOREX_ENGINE, "EURUSD must use FOREX_ENGINE")
    assert_true(engines.route_symbol("NAS100", "index_cfd").engine == engines.INDEX_ENGINE, "NAS100 must use INDEX_ENGINE")
    assert_true(engines.route_symbol("XAUUSD", "metal").engine == engines.METALS_ENGINE, "XAUUSD must use METALS_ENGINE")
    assert_true(engines.route_symbol("XAGUSD", "metal").engine == engines.METALS_ENGINE, "XAGUSD must use METALS_ENGINE")
    assert_false(engines.route_symbol("XAUUSD", "forex").allowed, "XAUUSD must not be treated as forex")


def test_high_cost_blocks():
    model = CostModel()
    cost = model.estimate_trade_cost("EURUSD", "BUY", 1.1000, 1.0990, 1.1010, 1.0, spread_price=0.0004, contract_size=100000)
    ok, reason, _score, _details = model.cost_gate(cost, 90)
    assert_false(ok, "high cost-to-target must block")
    assert_true("cost-to-target" in reason, reason)


def test_cost_to_target_missing_is_not_zero():
    model = CostModel()
    cost = model.estimate_trade_cost("XAGUSD", "SELL", 25.0, 25.2, 0.0, 1.0, spread_price=0.069, contract_size=1)
    assert_true(cost["cost_to_target_pct"] is None, "missing target must not be reported as 0%")
    assert_true(cost["cost_to_target_reason"] == "target missing", cost)
    ok, reason, score, details = model.cost_gate(cost, 90)
    assert_true(ok, reason)
    assert_true(details["cost_to_target_status"] == "unavailable", details)
    assert_true(score == 90, "unavailable cost-to-target must not change score")


def test_negative_expectancy_disables_symbol():
    with tempfile.TemporaryDirectory() as td:
        health = SymbolHealthManager(Path(td) / "disabled.json")
        disabled, reason = health.evaluate_symbol("EURUSD", {"trade_count": 50, "expectancy": -0.01, "profit_factor": 1.2, "max_drawdown": 10, "win_rate": 50})
        assert_true(disabled, "negative expectancy after 50 trades must disable symbol")
        assert_true("negative expectancy" in reason, reason)
        is_disabled, _ = health.is_symbol_disabled("EURUSD")
        assert_true(is_disabled, "symbol disable state must persist")


def test_bad_engine_reduces_risk():
    with tempfile.TemporaryDirectory() as td:
        health = SymbolHealthManager(Path(td) / "disabled.json")
        action, reason = health.evaluate_engine("FOREX_ENGINE", {"trade_count": 100, "expectancy": -0.05, "profit_factor": 1.2})
        assert_true(action == "reduce_risk_50", f"expected reduce_risk_50, got {action}")
        assert_true("expectancy" in reason, reason)


def test_monte_carlo_returns_drawdown():
    result = MonteCarloRiskEngine().simulate([1, -1, 0.5, -0.5, 1.2], iterations=100)
    assert_true("max_drawdown_estimate_r" in result, "Monte Carlo must return drawdown estimate")
    assert_true(result["iterations"] == 100, "Monte Carlo iteration count mismatch")


def test_walk_forward_flags_overfit():
    result = WalkForwardValidator().evaluate({"win_rate": 70, "profit_factor": 2.0, "expectancy": 0.4}, {"win_rate": 30, "profit_factor": 0.8, "expectancy": 0.1})
    assert_true(result["overfit"], "walk-forward must flag >40% OOS degradation")


def test_dashboard_csv_files_created():
    with tempfile.TemporaryDirectory() as td:
        analytics = AnalyticsEngine(td)
        kwargs = {"symbol":"EURUSD", "engine":"FOREX_ENGINE", "strategy":"trend", "side":"BUY", "score":90, "status":"BLOCKED", "block_reason":"exact reason", "spread":0.1, "slippage":0, "cost":1, "R_result":0, "profit_usd":0, "session":"NY", "regime":"TREND"}
        analytics.record_signal(**kwargs)
        analytics.record_pending_setup(**kwargs)
        analytics.record_execution(**kwargs)
        analytics.record_closed_trade(**kwargs)
        analytics.record_blocked_signal(**kwargs)
        analytics.write_summary([{"metric":"expectancy", "value":0.1}])
        day_dirs = list(Path(td).iterdir())
        assert_true(day_dirs, "CSV day folder must be created")
        names = {path.name for path in day_dirs[0].iterdir()}
        expected = {"signals.csv", "pending_setups.csv", "executions.csv", "closed_trades.csv", "blocked_signals.csv", "analytics_summary.csv"}
        assert_true(expected.issubset(names), f"missing CSVs: {expected - names}")


def test_blocked_trade_exact_reason():
    text = StrategyScoreExplainer().explain({"symbol":"NZDUSD", "direction":"SELL", "engine":"FOREX_ENGINE", "strategy":"MEAN_REV", "score":91, "adx":28.6}, "BLOCKED", "cost-to-target too high")
    assert_true("Raw" not in text and "Blocked" not in text, "generic Raw/Blocked wording must not be used")
    assert_true("cost-to-target too high" in text, "exact block reason missing")


def test_nzdusd_adx_286_not_hard_blocked():
    sig = {"strategy":"MEAN_REV", "score":95, "adx":28.6, "h1_bias":1, "m15_structure":1}
    decision = engines.apply_engine_policy("NZDUSD", "forex", sig, {})
    assert_true(decision.allowed, decision.reason)


def main():
    tests = [name for name in globals() if name.startswith("test_")]
    for name in sorted(tests):
        globals()[name]()
        print(f"PASS {name}")
    print(f"PASS {len(tests)} systematic engine tests")


if __name__ == "__main__":
    main()
