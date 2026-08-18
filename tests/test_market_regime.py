from datetime import datetime, timedelta, timezone

from cipherfx_platform.contracts import Candle, Frame
from cipherfx_platform.market_regime import classify_market

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def make_frame(values, name):
    candles = tuple(
        Candle(
            BASE + timedelta(minutes=i),
            close - 0.2,
            close + 0.4,
            close - 0.4,
            close,
            100,
        )
        for i, close in enumerate(values)
    )
    return Frame(name, candles)


def test_uptrend_context_is_described():
    values = [100 + i * 0.5 for i in range(60)]
    context = classify_market({"H4": make_frame(values, "H4"), "M15": make_frame(values, "M15")})
    assert context["label"] == "TREND_UP"
    assert context["direction"] == "BUY"
    assert context["role"] == "MARKET_CONTEXT_OBSERVATION_ONLY"


def test_flat_context_is_described_without_veto():
    values = [100 + (0.2 if i % 2 else -0.2) for i in range(60)]
    context = classify_market({"H4": make_frame(values, "H4"), "M15": make_frame(values, "M15")})
    assert context["label"] == "RANGE"
    assert context["direction"] == "NONE"
    assert context["role"] == "MARKET_CONTEXT_OBSERVATION_ONLY"
