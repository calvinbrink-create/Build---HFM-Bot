from datetime import datetime, timedelta, timezone

from cipherfx_clean.market import TIMEFRAMES, build_snapshot
from cipherfx_clean.contracts import RawTick


def test_fresh_snapshot_has_requested_completed_timeframes_only():
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ticks = tuple(RawTick("XAUUSD", base + timedelta(seconds=i), 10 + i * 0.1, 10.2 + i * 0.1) for i in range(301))
    snapshot = build_snapshot("XAUUSD", ticks, base + timedelta(seconds=301))
    assert tuple(snapshot.candles) == TIMEFRAMES
    assert snapshot.candles["M1"]
    assert all(bar.end <= snapshot.observed_at for bar in snapshot.candles["M1"])
