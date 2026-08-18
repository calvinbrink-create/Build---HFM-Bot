from datetime import datetime, timedelta, timezone

from cipherfx_platform.contracts import Candle, Frame, MarketSnapshot, Tick
from cipherfx_platform.engines import forex, indices, metals

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def chart_snapshot(asset):
    candles = []
    for i in range(55):
        price = 100 + i * 0.03
        candles.append(Candle(BASE + timedelta(minutes=5 * i), price, price + 1.0, price - 1.0, price + 0.1, 1000))
    candles.append(Candle(BASE + timedelta(minutes=275), 100.5, 101.0, 97.5, 100.7, 1300))
    candles.append(Candle(BASE + timedelta(minutes=280), 100.7, 103.5, 100.4, 103.0, 1800))
    last = candles[-1]
    symbol = "XAUUSD" if asset == "metal" else "USA100" if asset == "index" else "EURUSD"
    return MarketSnapshot(symbol, asset, {"M5": Frame("M5", tuple(candles))}, Tick(symbol, last.close, last.close + 0.1, last.timestamp, last.timestamp), last.timestamp, {"source": "websocket"})


def test_chart_setup_and_memory_are_the_entry_preparation_path():
    for module, asset in ((forex, "forex"), (indices, "index"), (metals, "metal")):
        result = module.build_setup(chart_snapshot(asset))
        assert result["valid"] is True
        assert result["decision_role"] == "CHART_SETUP_WITH_MEMORY"
        assert result["frames"]["M5"]["type"] != "NONE"
        assert result["memory"]["role"] == "SETUP_RECOGNITION"
        assert result["memory"]["shape"] is not None


def test_engines_do_not_cross_import_or_use_score_gates():
    for module in (forex, indices, metals):
        source = open(module.__file__).read().lower()
        assert "from ..engines" not in source
        assert "import ..engines" not in source
        assert "threshold" not in source
        assert "score" not in source


def test_each_engine_keeps_asset_specific_identity():
    assert forex.build_setup(chart_snapshot("forex"))["engine"] == "FOREX_ENGINE"
    assert indices.build_setup(chart_snapshot("index"))["engine"] == "INDICES_ENGINE"
    assert metals.build_setup(chart_snapshot("metal"))["engine"] == "METALS_ENGINE"



def test_gold_uses_live_tick_break_before_completed_candle_close():
    from dataclasses import replace

    snapshot = chart_snapshot("metal")
    last = snapshot.frames["M5"].last
    live_time = last.timestamp + timedelta(minutes=5, seconds=15)
    live_tick = Tick(
        snapshot.symbol,
        last.close + 1.0,
        last.close + 1.1,
        live_time,
        live_time,
    )
    result = metals.build_setup(replace(snapshot, tick=live_tick))
    assert result["valid"] is True
    assert result["side"] == "BUY"
    assert result["chart_setup"]["trigger_source"] == "LIVE_TICK"
    assert result["entry_model"] == "LIVE_M5_TICK_BREAK"
    assert result["entry_timing"]["status"] == "LIVE_TRIGGER"


def test_all_active_engines_use_directional_live_entry_candles_without_score_veto():
    candles = tuple(
        Candle(
            BASE + timedelta(minutes=5 * i),
            100.0,
            101.0,
            99.0,
            100.1,
            1000,
        )
        for i in range(40)
    )
    for direction, price in (("BUY", 101.9), ("SELL", 98.0)):
        for module, asset, symbol in (
            (forex, "forex", "EURUSD"),
            (indices, "index", "USA100"),
            (metals, "metal", "XAUUSD"),
        ):
            live_time = candles[-1].timestamp + timedelta(minutes=5, seconds=15)
            tick = Tick(symbol, price - 0.1, price, live_time, live_time)
            snapshot = MarketSnapshot(
                symbol,
                asset,
                {"M5": Frame("M5", candles)},
                tick,
                live_time,
                {"source": "websocket"},
            )
            result = module.build_setup(snapshot)
            assert result["valid"] is True
            assert result["side"] == direction
            assert result["chart_setup"]["trigger_source"] == "LIVE_TICK"
            assert result["chart_setup"]["forming_direction"] == direction
            assert result["entry_model"] == "LIVE_M5_TICK_BREAK"
