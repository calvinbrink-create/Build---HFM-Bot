from datetime import datetime, timedelta, timezone

from cipherfx_clean.contracts import Candle, MarketSnapshot, RawTick
from cipherfx_clean.dashboard import intelligence_view
from cipherfx_clean.intelligence.engine import IntelligenceEngine
from cipherfx_clean.intelligence.session_vwap import build_session_vwap_library


UTC = timezone.utc


def _day(day: datetime) -> tuple[Candle, ...]:
    rows = []
    for minute in range(24 * 60):
        start = day + timedelta(minutes=minute)
        price = 100.0 + minute / 100.0
        rows.append(
            Candle(
                "USA100", "M1", start, start + timedelta(minutes=1),
                price, price + 0.2, price - 0.1, price + 0.1,
                tick_count=minute + 1, tick_volume=float(minute + 1), spread_points=2.0,
                source="TEST", source_id=f"{day.date()}:{minute}",
            )
        )
    return tuple(rows)


def test_session_vwap_uses_only_elapsed_session_bars_and_broker_activity_weight():
    monday = datetime(2026, 7, 13, tzinfo=UTC)
    observed_at = monday + timedelta(hours=8)
    rows = _day(monday)
    library = build_session_vwap_library({"USA100": rows}, observed_at=observed_at)
    current = library.current_for_symbol("USA100", observed_at)
    selected = rows[7 * 60:8 * 60]
    weight = sum(item.tick_volume for item in selected)
    expected = sum(
        ((item.high + item.low + item.close) / 3.0) * item.tick_volume
        for item in selected
    ) / weight

    assert current is not None
    assert current.session == "LONDON"
    assert current.complete_session is False
    assert current.candle_count == 60
    assert current.expected_candle_count == 60
    assert current.coverage_ratio == 1.0
    assert current.value == expected
    assert current.last_price == selected[-1].close
    assert current.deviation == current.last_price - current.value
    assert current.position == "ABOVE"
    assert current.observed_through_utc == observed_at.isoformat()


def test_engine_and_dashboard_use_session_anchored_vwap_when_available():
    monday = datetime(2026, 7, 13, tzinfo=UTC)
    observed_at = monday + timedelta(hours=8)
    rows = _day(monday)
    library = build_session_vwap_library({"USA100": rows}, observed_at=observed_at)
    current = library.current_for_symbol("USA100", observed_at)
    snapshot = MarketSnapshot(
        "USA100",
        observed_at,
        (RawTick("USA100", observed_at, rows[479].close, rows[479].close + 0.1),),
        {"M1": rows[450:480]},
        {"M1": "COMPLETED"},
        ("test:session-vwap",),
    )
    report = IntelligenceEngine(session_vwap_library=library).analyse(snapshot)
    view = intelligence_view(report)

    assert report.current_session_vwap == current
    assert report.vwap.value == current.value
    assert report.vwap.source == "HFM_M1_TICK_VOLUME_WEIGHTED_TYPICAL_PRICE_IANA_SESSION"
    assert view["current_session_vwap"]["position"] == "ABOVE"
    assert view["vwap"]["value"] == current.value
    assert "SESSION_VWAP_OBSERVED" in report.evidence
