from datetime import datetime, timedelta, timezone

from cipherfx_clean.candles import detect_patterns
from cipherfx_clean.contracts import Candle


def candle(i, open_, high, low, close):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=i)
    return Candle("XAUUSD", "M1", start, start + timedelta(minutes=1), open_, high, low, close)


def test_fresh_candle_library_covers_wick_control_and_multi_candle_families():
    rows = (
        candle(0, 10, 10, 9, 9.9),
        candle(1, 9.9, 10, 9.7, 9.75),
        candle(2, 9.7, 10.4, 9.6, 10.3),
        candle(3, 10.3, 10.31, 10.29, 10.3),
    )
    names = {item.name for item in detect_patterns(rows)}
    assert {"HAMMER", "BULLISH_ENGULFING", "DOJI", "INSIDE_BAR"}.issubset(names)
