from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from cipherfx_clean.contracts import Candle
from cipherfx_clean.intelligence.zones import identify_zones
from cipherfx_clean.requirement_runtime import verify_c018_demand_zones


BASE = datetime(2026, 8, 24, tzinfo=timezone.utc)


def _candle(index, open_, high, low, close):
    start = BASE + timedelta(minutes=5 * index)
    return Candle("XAUUSD", "M5", start, start + timedelta(minutes=5), open_, high, low, close)


def _demand(*future):
    rows = (
        _candle(0, 10, 11, 9, 10),
        _candle(1, 10, 13, 9.8, 12),
        *future,
    )
    return next(zone for zone in identify_zones(rows) if zone.kind == "DEMAND" and zone.origin_index == 0)


def test_demand_zone_lifecycle_reaches_all_four_states_and_later_failure_wins():
    fresh = _demand()
    tested = _demand(_candle(2, 12, 13, 10.5, 11.5))
    mitigated = _demand(_candle(2, 10, 11.5, 8.8, 10))
    failed = _demand(
        _candle(2, 10, 11.5, 8.8, 10),
        _candle(3, 10, 10.2, 8.4, 8.8),
    )
    assert (fresh.freshness, fresh.touches) == ("FRESH", 0)
    assert (tested.freshness, tested.touches) == ("TESTED", 1)
    assert (mitigated.freshness, mitigated.touches) == ("MITIGATED", 1)
    assert failed.freshness == "FAILED"
    assert failed.touches == 2
    assert failed.invalidated_at == 3


def test_c018_demand_zone_contract_reports_exact_lifecycle():
    zones = (
        _demand(),
        _demand(_candle(2, 12, 13, 10.5, 11.5)),
        _demand(_candle(2, 10, 11.5, 8.8, 10)),
        _demand(_candle(2, 10, 10.2, 8.4, 8.8)),
    )
    report = SimpleNamespace(
        state_id="state-1",
        symbol="XAUUSD",
        observed_at=BASE.isoformat(),
        zones={"M5": zones},
    )
    result = verify_c018_demand_zones(report)
    assert result["demand_zone_count"] == 4
    assert set(result["observed_states"]) == {"FRESH", "TESTED", "MITIGATED", "FAILED"}
