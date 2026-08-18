from datetime import datetime, time, timedelta, timezone

from cipherfx_platform.contracts import Candle, Frame, MarketSnapshot, Tick
from cipherfx_platform.engines.session_breakout import (
    SessionBreakoutEngine, SessionBreakoutProfile, default_profiles,
)


def make_snapshot(side="BUY", symbol="XAUUSD", now=datetime(2026, 8, 17, 7, 5, tzinfo=timezone.utc)):
    m15 = []
    for i in range(20):
        t = datetime(2026, 8, 17, tzinfo=timezone.utc) + timedelta(minutes=i * 15)
        m15.append(Candle(t, 100.0, 100.8 if i == 3 else 100.4, 99.2 if i == 6 else 99.6, 100.1, 100))
    close = 101.0 if side == "BUY" else 99.0 if side == "SELL" else 100.0
    m5 = [Candle(now - timedelta(minutes=5), 100.0, 100.2, 99.8, 100.0, 100),
           Candle(now, close, close + 0.1, close - 0.1, close, 100)]
    tick = Tick(symbol, 101.1 if side == "BUY" else 98.9, 101.2 if side == "BUY" else 99.0, now, now)
    return MarketSnapshot(symbol, "metal", {"M15": Frame("M15", tuple(m15)), "M5": Frame("M5", tuple(m5))}, tick, now)


def test_profiles_are_asset_specific():
    profiles = default_profiles()
    assert profiles["XAUUSD"].box_start_utc == time(0)
    assert profiles["UK100"].box_start_utc == time(5)
    assert profiles["USA100"].box_start_utc == time(11, 30)


def test_closed_breakout_builds_buy_proposal():
    result = SessionBreakoutEngine(default_profiles()["XAUUSD"]).evaluate(make_snapshot("BUY"))
    assert result["valid"] is True
    assert result["side"] == "BUY"
    assert "CLOSED_M5_BREAKOUT" in result["reasons"]
    assert result["take_profit"] > result["entry"]


def test_closed_breakout_builds_sell_proposal():
    result = SessionBreakoutEngine(default_profiles()["XAUUSD"]).evaluate(make_snapshot("SELL"))
    assert result["valid"] is True
    assert result["side"] == "SELL"
    assert result["take_profit"] < result["entry"]


def test_no_breakout_is_not_a_setup():
    result = SessionBreakoutEngine(default_profiles()["XAUUSD"]).evaluate(make_snapshot("NONE"))
    assert result["valid"] is False
    assert result["reason"] == "NO_CLOSED_BREAKOUT"


def test_box_building_does_not_trigger():
    snap = make_snapshot("BUY", now=datetime(2026, 8, 17, 4, 55, tzinfo=timezone.utc))
    result = SessionBreakoutEngine(default_profiles()["XAUUSD"]).evaluate(snap)
    assert result["reason"] == "BUILDING_SESSION_BOX"


def test_one_trade_per_day_is_stateful():
    engine = SessionBreakoutEngine(default_profiles()["XAUUSD"])
    result = engine.evaluate(make_snapshot("BUY"))
    engine.mark_submitted(result)
    second = engine.evaluate(make_snapshot("SELL"))
    assert second["reason"] == "ONE_TRADE_PER_DAY"


def test_range_too_wide_is_rejected():
    profile = SessionBreakoutProfile("XAUUSD", "metal", time(0), time(5), time(7), time(16), time(21), max_box_atr=0.1)
    result = SessionBreakoutEngine(profile).evaluate(make_snapshot("BUY"))
    assert result["reason"] == "RANGE_TOO_WIDE"


def test_missing_frames_are_explicit():
    now = datetime(2026, 8, 17, 7, 5, tzinfo=timezone.utc)
    tick = Tick("XAUUSD", 1, 1.1, now, now)
    snap = MarketSnapshot("XAUUSD", "metal", {}, tick, now)
    assert SessionBreakoutEngine(default_profiles()["XAUUSD"]).evaluate(snap)["reason"] == "MISSING_M15_OR_M5"
