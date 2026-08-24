from datetime import datetime, timedelta, timezone

from cipherfx_clean.contracts import Candle, MarketSnapshot, RawTick
from cipherfx_clean.dashboard import intelligence_view
from cipherfx_clean.intelligence.engine import IntelligenceEngine
from cipherfx_clean.intelligence.session_profiles import build_session_profile_library


UTC = timezone.utc


def _full_day() -> tuple[Candle, ...]:
    day = datetime(2026, 7, 13, tzinfo=UTC)
    rows = []
    for minute in range(24 * 60):
        start = day + timedelta(minutes=minute)
        price = 100.0 + minute * 0.01
        rows.append(
            Candle(
                "XAUUSD", "M1", start, start + timedelta(minutes=1),
                price, price + 0.2, price - 0.1, price + 0.05,
                tick_count=10, tick_volume=10.0, spread_points=2.0,
                source="TEST", source_id=f"asia:{minute}",
            )
        )
    return tuple(rows)


def test_asia_range_stores_high_low_midpoint_and_range_size_exactly():
    rows = _full_day()
    observed_at = rows[-1].end
    library = build_session_profile_library(
        {"XAUUSD": rows},
        observed_at=observed_at,
    )
    ranges = library.observations_for_symbol_session("XAUUSD", "ASIA")

    assert len(ranges) == 1
    asia = ranges[0]
    assert asia.complete is True
    assert asia.candle_count == 420
    assert asia.expected_candle_count == 420
    assert asia.high_price == max(item.high for item in rows[:420])
    assert asia.low_price == min(item.low for item in rows[:420])
    assert asia.midpoint_price == (asia.high_price + asia.low_price) / 2.0
    assert asia.range_value == asia.high_price - asia.low_price
    assert asia.source_digest


def test_asia_range_history_is_exposed_without_becoming_a_decision_gate():
    rows = _full_day()
    observed_at = rows[-1].end
    library = build_session_profile_library({"XAUUSD": rows}, observed_at=observed_at)
    snapshot = MarketSnapshot(
        "XAUUSD",
        observed_at,
        (RawTick("XAUUSD", observed_at, rows[-1].close, rows[-1].close + 0.1),),
        {"M1": rows[-30:]},
        {"M1": "COMPLETED"},
        ("test:asia-range",),
    )
    report = IntelligenceEngine(session_profiles=library).analyse(snapshot)
    view = intelligence_view(report)

    assert len(report.asia_ranges) == 1
    assert view["asia_ranges"][0]["range_value"] == report.asia_ranges[0].range_value
    assert "ASIA_RANGE_HISTORY_OBSERVED" in report.evidence
