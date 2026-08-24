from datetime import datetime, timedelta, timezone

from cipherfx_clean.intelligence.engine import IntelligenceEngine
from cipherfx_clean.market import Candle, MarketSnapshot, RawTick


def test_intelligence_builds_chart_structure_zones_liquidity_and_regime():
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    candles = tuple(
        Candle("XAUUSD", "M5", base + timedelta(minutes=i * 5), base + timedelta(minutes=(i + 1) * 5), 10 + i, 11 + i, 9 + i, 10.5 + i, 3)
        for i in range(6)
    )
    ticks = tuple(RawTick("XAUUSD", base + timedelta(seconds=i), 10 + i * 0.1, 10.2 + i * 0.1) for i in range(3))
    report = IntelligenceEngine().analyse(MarketSnapshot("XAUUSD", base, ticks, {"M5": candles}))
    assert report.symbol == "XAUUSD"
    assert report.structure["M5"].direction in {"UP", "DOWN", "RANGE", "UNKNOWN"}
    assert report.chart.annotations["M5"]
    assert "M5" in report.regimes
    assert report.evidence
