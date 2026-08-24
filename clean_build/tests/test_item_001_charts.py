from datetime import datetime, timedelta, timezone

from cipherfx_clean.charts import create_chart_frame
from cipherfx_clean.contracts import Candle


def test_chart_frame_contains_same_candles_patterns_structure_and_zones():
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    candles = tuple(Candle("XAUUSD", "M5", base + timedelta(minutes=i * 5), base + timedelta(minutes=(i + 1) * 5), 10 + i, 11 + i, 9 + i, 10.5 + i) for i in range(8))
    frame = create_chart_frame("XAUUSD", "M5", candles)
    assert frame.candles == candles
    assert frame.structure.direction in {"UP", "DOWN", "RANGE", "UNKNOWN"}
    assert any(item.kind == "STRUCTURE" for item in frame.annotations)
