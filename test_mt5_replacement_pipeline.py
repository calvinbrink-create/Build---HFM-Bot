#!/usr/bin/env python3
from __future__ import annotations

import os
from datetime import datetime, timedelta
from types import SimpleNamespace

import pandas as pd

import mt5_bot
import strategy_architecture_v1 as strategy
from dashboard.backend import state_store
from mt5_xm_config import MT5RuntimeConfig
from mt5_xm_gateway import MT5SymbolSpec


def candles(side: str, count: int = 100, start: float = 100.0):
    step = 0.25 if side == "BUY" else -0.25
    rows = []
    price = start
    for idx in range(count):
        open_price = price
        close = open_price + step
        high = max(open_price, close) + 0.08
        low = min(open_price, close) - 0.08
        rows.append(strategy.Candle(
            f"2026-07-01T00:{idx:03d}:00", open_price, high, low, close, 100.0 + idx
        ))
        price = close
    return tuple(rows)


def test_manifest_and_strategy_sides():
    runtime = MT5RuntimeConfig()
    symbols = [mt5_bot.canonical_symbol(value).upper() for value in runtime.all_symbols()]
    assert len(symbols) == 17
    assert not [symbol for symbol in symbols if mt5_bot.get_symbol_config(symbol) is None]
    expected = {
        "forex": "FOREX_TREND_PULLBACK",
        "index": "INDEX_SESSION_BREAKOUT",
        "metal": "METAL_VOLATILITY_TREND",
    }
    samples = {"forex": "EURUSD", "index": "NAS100", "metal": "XAUUSD"}
    for asset, symbol in samples.items():
        for side in ("BUY", "SELL"):
            frames = {
                label: strategy.Frame(label, candles(side))
                for label in ("H4", "H1", "M15", "M5", "M1")
            }
            profile = strategy.Profile(symbol, asset, 1.5, 1.8, 0.35, 0.01)
            decision = strategy.evaluate(symbol, profile, frames)
            assert decision.state == "PASS", (asset, side, decision.reasons)
            assert decision.side == side
            assert decision.engine == expected[asset]
            consistency = decision.evidence["side_consistency"]
            assert consistency["setup_side"] == consistency["final_order_side"] == side
            assert consistency["confirmation_side"] == "M5_TRIGGER"


def test_m1_confirmation_and_close_time():
    index = pd.date_range("2026-07-11T10:00:00", periods=25, freq="min")
    close = [100.0 + idx * 0.1 for idx in range(25)]
    frame = pd.DataFrame({
        "Open": [value - 0.05 for value in close],
        "High": [value + 0.02 for value in close],
        "Low": [value - 0.08 for value in close],
        "Close": close,
        "Volume": [100.0] * 25,
    }, index=index)
    ok, _reason, _score = mt5_bot.valid_1m_confirmation(frame, "BUY", "FOREX_TREND_PULLBACK")
    assert ok
    sell = frame.copy()
    sell_close = [103.0 - idx * 0.1 for idx in range(25)]
    sell["Close"] = sell_close
    sell["Open"] = [value + 0.05 for value in sell_close]
    sell["High"] = [value + 0.08 for value in sell_close]
    sell["Low"] = [value - 0.02 for value in sell_close]
    ok, _reason, _score = mt5_bot.valid_1m_confirmation(sell, "SELL", "FOREX_TREND_PULLBACK")
    assert ok
    no_breakout = frame.copy()
    no_breakout.loc[no_breakout.index[-2], "Close"] = no_breakout.iloc[-3]["Close"] - 0.01
    no_breakout.loc[no_breakout.index[-2], "High"] = no_breakout.iloc[-3]["Close"]
    ok, reason, _score = mt5_bot.valid_1m_confirmation(no_breakout, "BUY", "FOREX_TREND_PULLBACK")
    assert not ok
    assert "M1_BREAKOUT_RETEST_WAITING" in reason
    closed_at, candle_id = mt5_bot._completed_m1_candle_id(frame)
    assert datetime.fromisoformat(closed_at) == frame.index[-2].to_pydatetime() + timedelta(seconds=60)
    assert candle_id.startswith("M1:")
    short_probe = frame.tail(3)
    closed_at, candle_id = mt5_bot._completed_candle_identity(short_probe, "M5", 300)
    assert datetime.fromisoformat(closed_at) == short_probe.index[-2].to_pydatetime() + timedelta(seconds=300)
    assert candle_id.startswith("M5:")


def test_tick_engine_routing():
    calls = []
    originals = (mt5_bot.forex_spread_gate, mt5_bot.index_spread_gate, mt5_bot.metals_spread_gate)
    mt5_bot.forex_spread_gate = lambda *args: (calls.append("forex") or True, 90.0, "OK", {})
    mt5_bot.index_spread_gate = lambda *args: (calls.append("index") or True, 90.0, "OK", {})
    mt5_bot.metals_spread_gate = lambda *args: (calls.append("metal") or True, 90.0, "OK", {})
    try:
        for engine in ("FOREX_TREND_PULLBACK", "INDEX_SESSION_BREAKOUT", "METAL_VOLATILITY_TREND"):
            ok, reason, _meta = mt5_bot.tick_execution_check("TEST", engine, 100.0, 100.1, 100.05, 10.0, 90.0)
            assert ok, reason
        assert calls == ["forex", "index", "metal"], calls
    finally:
        mt5_bot.forex_spread_gate, mt5_bot.index_spread_gate, mt5_bot.metals_spread_gate = originals


def test_completed_m5_price_displacement_is_observe_only():
    original = mt5_bot.forex_spread_gate
    mt5_bot.forex_spread_gate = lambda *args: (True, 90.0, "spread OK", {})
    try:
        ok, reason, meta = mt5_bot.tick_execution_check(
            "USDJPY", "FOREX_TREND_PULLBACK", 162.342, 162.344, 162.331, 0.030, 90.5
        )
        assert ok, reason
        assert meta["slippage_atr"] > 0.35
        assert meta["entry_displacement_observe_only"] is True
        assert "displaced" in meta["entry_displacement_reason"]
    finally:
        mt5_bot.forex_spread_gate = original


def test_closed_campaign_identity_cannot_bleed_into_new_trade():
    source=open("/opt/cipherfx_mt5/mt5_bot.py", encoding="utf-8").read()
    assert 'campaign_is_current' in source
    assert 'CAMPAIGN_IDENTITY_REFRESHED' in source
    assert 'self.campaigns.setdefault(key, {' not in source

def test_pyramid_uses_replacement_engine_rr_context():
    source=open("/opt/cipherfx_mt5/mt5_bot.py", encoding="utf-8").read()
    assert 'rr_context = {' in source
    assert 'self._entry_rr_for_symbol(canonical, market, rr_context, configured_rr=cfg.get("tp_r"))' in source
    assert 'self._entry_rr_for_symbol(canonical, market, {}, configured_rr=cfg.get("tp_r"))' not in source

def test_execution_reuses_broker_geometry_without_double_cost_penalty():
    source=open("/opt/cipherfx_mt5/mt5_bot.py", encoding="utf-8").read()
    assert 'reused_at_engine_policy_recheck' in source
    assert 'profit_source = "sizing_broker_geometry"' in source
    assert 'same_cost_geometry' in source

def test_cost_policy():
    bot = SimpleNamespace(
        gateway=SimpleNamespace(symbol_tick=lambda symbol: (symbol, SimpleNamespace(bid=100.0, ask=100.1))),
        _canonical_from_resolved=lambda symbol: symbol,
        _audit_decision=lambda payload: None,
    )
    model = mt5_bot.CostModel(bot)
    base_risk = {"spec": {"contract_size": 999999.0}, "profit_per_lot": 100.0}
    warning_sig = {"price": 100.0, "direction": "BUY", "engine": "INDEX_SESSION_BREAKOUT", "score": 80.0, "spread_signal": 0.40}
    allowed, _reason, _score, details = model.check("NAS100", "index_cfd", warning_sig, 1.0, 99.0, 101.0, base_risk, "pre_order")
    assert allowed and details["decision"] == "quality_filter"
    assert details["calculation_source"] == "broker_geometry"
    extreme_sig = {"price": 100.0, "direction": "BUY", "engine": "INDEX_SESSION_BREAKOUT", "score": 80.0, "spread_signal": 0.60}
    allowed, _reason, _score, details = model.check("NAS100", "index_cfd", extreme_sig, 1.0, 99.0, 101.0, base_risk, "pre_order")
    assert not allowed and details["decision"] == "block"


def test_broker_sizing_batch():
    calls = []
    spec = MT5SymbolSpec("NAS100", volume_min=0.1, volume_max=100.0, volume_step=0.1)
    gateway = SimpleNamespace(
        symbol_info=lambda symbol: spec,
        order_calc_trade_geometry=lambda *args: calls.append(args) or {
            "sl_loss_usd": 100.0, "tp_profit_usd": 180.0, "margin_usd": 250.0, "currency": "USD"
        },
        account_info=lambda: SimpleNamespace(free_margin=10000.0),
        normalize_volume=lambda _spec, value: max(0.0, round(value, 1)),
    )
    bot = object.__new__(mt5_bot.XM_MT5_Bot)
    bot.gateway = gateway
    bot._canonical_from_resolved = lambda value: value
    bot._risk_pct_for_symbol = lambda symbol, market: 2.0
    bot._two_engine_test_risk_usd = lambda symbol: None
    bot._entry_rr_for_symbol = lambda *args, **kwargs: 1.8
    bot._max_lot_for_symbol = lambda symbol, market: 100.0
    bot._max_trade_risk_limit_for_symbol = lambda symbol, market: 100.0
    bot._aggressive_scalp_effective = lambda: False
    sig = {"direction": "BUY", "score": 80.0, "replacement_strategy_v1": True, "asset_class": "index", "_mt5_thresholds": {}}
    volume, reason, meta = bot._mt5_volume_for_trade("NAS100", "index_cfd", 100.0, 1.0, 5000.0, sig)
    assert reason == "OK" and volume == 1.0
    assert len(calls) == 1
    assert meta["planned_risk"] == 100.0 and meta["profit_per_lot"] == 180.0


def test_campaign_stop_target_and_add():
    def make_bot(profit):
        position = SimpleNamespace(
            ticket=11, symbol="NAS100", direction="BUY", volume=1.0,
            price_open=100.0, price_current=101.0, sl=99.0, tp=102.0, profit=profit,
        )
        gateway = SimpleNamespace(
            positions=lambda: [position],
            symbol_tick=lambda symbol: (symbol, SimpleNamespace(bid=100.9, ask=101.0)),
            account_info=lambda: SimpleNamespace(equity=5000.0),
        )
        bot = object.__new__(mt5_bot.XM_MT5_Bot)
        bot.state = {"campaigns": {}, "open_trades": {"11": {
            "trade_id": "direct-1", "opened_at": datetime.utcnow().isoformat(),
            "risk_meta": {"planned_risk": 100.0},
            "trade_audit": {"engine": "INDEX_SESSION_BREAKOUT", "strategy": "SESSION_BREAKOUT_RETEST"},
        }}}
        bot.gateway = gateway
        bot.symbol_markets = {"NAS100": "index_cfd"}
        bot.runtime = SimpleNamespace(capital_cap_usd=5000)
        bot._save_state = lambda: None
        bot._pyramiding_enabled = lambda: True
        bot._canonical_from_resolved = lambda value: value
        bot._entry_window_open = lambda market: True
        bot._daily_risk_allows_entry = lambda: True
        bot._daily_trade_count_allows_entry = lambda: True
        bot._open_position_count = lambda: 1
        bot._effective_max_open_trades = lambda: 30
        bot._market_for_symbol = lambda symbol: "index_cfd"
        bot._recent_atr = lambda symbol: 1.0
        bot._cap_stop_distance_for_symbol = lambda symbol, market, distance, sig: distance
        bot._entry_rr_for_symbol = lambda *args, **kwargs: 1.8
        bot._strategy_equity = lambda account: 5000.0
        bot._mt5_volume_for_trade = lambda *args, **kwargs: (1.0, "OK", {"planned_risk": 100.0})
        bot.cost_model = SimpleNamespace(check=lambda *args, **kwargs: (True, "OK", 100.0, {}))
        bot.portfolio_risk_manager = SimpleNamespace(check=lambda *args, **kwargs: (True, "OK"))
        bot._execution_gate_allows_entry = lambda *args, **kwargs: (True, "OK")
        bot.execution_quality_engine = SimpleNamespace(
            pre_order_check=lambda *args, **kwargs: (True, "OK", {}),
            confirm_order_result=lambda *args, **kwargs: (True, "OK", {}),
        )
        placed, closed = [], []
        bot._place_trade = lambda *args, **kwargs: placed.append(args) or SimpleNamespace(retcode=10009)
        bot._close_position_for_profit = lambda pos, symbol, reason: closed.append(("profit", reason)) or True
        bot._close_position_for_risk = lambda pos, symbol, reason: closed.append(("risk", reason)) or True
        bot._audit_decision = lambda payload: None
        return bot, placed, closed

    bot, placed, closed = make_bot(-100.0)
    bot._campaign_cycle()
    assert closed and closed[0][0] == "risk" and not placed
    bot, placed, closed = make_bot(180.0)
    bot._campaign_cycle()
    assert closed and closed[0][0] == "profit" and not placed
    bot, placed, closed = make_bot(20.0)
    bot._campaign_cycle()
    assert len(placed) == 1 and not closed
    assert bot.campaigns["NAS100:BUY"]["accepted_legs"] == 2
    campaign = bot.campaigns["NAS100:BUY"]
    assert campaign["pyramid_add_status"] == "active_same_setup_burst"
    assert campaign["next_add_required_r"] == 0.01
    source = open("/opt/cipherfx_mt5/mt5_bot.py", encoding="utf-8").read()
    assert "_campaign_alignment_allows_add" not in source
    assert "MT5_PYRAMID_STEP_R" not in source
    assert "PYRAMID_BURST_WINDOW_CLOSED" in source
    assert "self.open_trades[str(live_pos.ticket)] = meta" not in source
    bot, placed, closed = make_bot(20.0)
    bot.open_trades["11"]["opened_at"] = (datetime.utcnow() - timedelta(seconds=61)).isoformat()
    bot._campaign_cycle()
    assert not placed and not closed
    assert bot.campaigns["NAS100:BUY"]["pyramid_add_status"] == "closed_after_burst"



def test_fixed_controls_and_mql_defaults():
    bot = object.__new__(mt5_bot.XM_MT5_Bot)
    bot._daily_trade_count = lambda: 60
    bot._effective_max_daily_trades = lambda: 60
    bot._audit_decision = lambda payload: None
    original = mt5_bot._ss.set_status
    mt5_bot._ss.set_status = lambda *args, **kwargs: None
    try:
        assert not bot._daily_trade_count_allows_entry()
    finally:
        mt5_bot._ss.set_status = original
    bot_source = open("/opt/cipherfx_mt5/mt5_bot.py", encoding="utf-8").read()
    assert 'name="mt5-fast-m1-discovery"' not in bot_source
    assert "self._start_m5_scan_worker(m5_trigger_interval)" in bot_source
    source = open("/opt/cipherfx_mt5/mt5_bridge/CipherFxBridge.mq5", encoding="utf-8").read()
    for expected in (
        "input double DailyLossLimitUSD = 1000.00;",
        "input int MaxPyramidTrades = 10;",
        "input int MaxPyramidTradesPerSignal = 10;",
        "input bool AllowSameCandlePyramids = true;",
        "input int MaxOpenTradesTotal = 30;",
        "input int MaxTradesPerDay = 60;",
        "export_count = MathMin(export_count, 32);",
    ):
        assert expected in source
    assert state_store.broker_trade_date_for("2026-07-10T21:00:00+00:00") == "2026-07-11"


def main():
    tests = [
        test_manifest_and_strategy_sides,
        test_m1_confirmation_and_close_time,
        test_tick_engine_routing,
        test_completed_m5_price_displacement_is_observe_only,
        test_closed_campaign_identity_cannot_bleed_into_new_trade,
        test_pyramid_uses_replacement_engine_rr_context,
        test_execution_reuses_broker_geometry_without_double_cost_penalty,
        test_cost_policy,
        test_broker_sizing_batch,
        test_campaign_stop_target_and_add,
        test_fixed_controls_and_mql_defaults,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"PASS replacement pipeline tests={len(tests)}")


if __name__ == "__main__":
    main()

