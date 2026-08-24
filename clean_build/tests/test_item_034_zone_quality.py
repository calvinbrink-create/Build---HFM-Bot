from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from cipherfx_clean.contracts import Candle
from cipherfx_clean.intelligence.advanced import zone_quality
from cipherfx_clean.intelligence.zones import identify_zones
from cipherfx_clean.requirement_runtime import verify_c019_zone_quality


BASE = datetime(2026, 8, 24, tzinfo=timezone.utc)


def _candle(index, open_, high, low, close):
    start = BASE + timedelta(minutes=5 * index)
    return Candle("XAUUSD", "M5", start, start + timedelta(minutes=5), open_, high, low, close)


def _quality_rows():
    return (
        _candle(0, 10, 11, 9, 10),
        _candle(1, 10, 10.2, 7, 8),
        _candle(2, 8, 9.5, 7.5, 8.5),
        _candle(3, 8.5, 9, 6.5, 7),
    )


def test_zone_quality_uses_lifecycle_displacement_normalized_penetration_and_reaction():
    rows = _quality_rows()
    zone = next(item for item in identify_zones(rows) if item.kind == "SUPPLY" and item.origin_index == 0)
    quality = zone_quality(
        rows,
        zone.low,
        zone.high,
        zone.origin_index,
        kind=zone.kind,
        displacement_ratio=zone.displacement,
        lifecycle=zone.freshness,
        activation_index=zone.origin_index + 1,
        invalidated_at=zone.invalidated_at,
    )
    assert quality.lifecycle == zone.freshness
    assert quality.displacement_ratio == zone.displacement
    assert quality.touches == zone.touches
    assert quality.penetration == pytest.approx(zone.penetration_ratio)
    assert quality.reaction > 0
    assert all(
        0.0 <= value <= 1.0
        for value in (
            quality.freshness,
            quality.displacement,
            quality.penetration,
            quality.reaction,
            quality.touch_quality,
            quality.score,
        )
    )


def test_c019_zone_quality_contract_is_descriptive_and_linked_to_each_zone():
    rows = _quality_rows()
    zones = identify_zones(rows)
    qualities = tuple(
        zone_quality(
            rows,
            zone.low,
            zone.high,
            zone.origin_index,
            kind=zone.kind,
            displacement_ratio=zone.displacement,
            lifecycle=zone.freshness,
            activation_index=zone.origin_index + 1,
            invalidated_at=zone.invalidated_at,
        )
        for zone in zones
    )
    report = SimpleNamespace(
        state_id="state-1",
        symbol="XAUUSD",
        observed_at=BASE.isoformat(),
        zones={"M5": zones},
        zone_quality={"M5": qualities},
    )
    result = verify_c019_zone_quality(report)
    assert result["zone_count"] == len(zones)
    assert 0.0 <= result["mean_score"] <= 1.0
