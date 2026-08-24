from datetime import datetime, timedelta, timezone

from cipherfx_clean.market import (
    RawTick,
    TickQuality,
    build_completed_candles,
    build_snapshot,
    validate_tick,
)


def tick(second, bid):
    return RawTick(
        "XAUUSD",
        datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=second),
        bid,
        bid + 0.1,
    )


def test_tick_validation_rejects_duplicate_and_stale_data():
    now = datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc)
    assert validate_tick(
        tick(50, 10), now=now, max_age=timedelta(seconds=30), previous=tick(50, 9)
    ).quality is TickQuality.DUPLICATE
    assert validate_tick(
        tick(0, 10), now=now, max_age=timedelta(seconds=30)
    ).quality is TickQuality.STALE
    assert validate_tick(
        tick(0, 10), now=now, max_age=timedelta(seconds=30), previous=tick(0, 9)
    ).quality is TickQuality.STALE


def test_tick_validation_rejects_malformed_quotes():
    now = datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc)
    malformed = RawTick("XAUUSD", now, 10.0, 9.0)

    result = validate_tick(malformed, now=now, max_age=timedelta(seconds=30))

    assert result.quality is TickQuality.MALFORMED
    assert result.reason == "INVALID_SYMBOL_OR_QUOTE"


def test_completed_candle_builder_excludes_forming_bar():
    now = datetime(2026, 1, 1, 0, 1, 1, tzinfo=timezone.utc)
    ticks = [tick(0, 10), tick(30, 12), tick(60, 11)]
    candles = build_completed_candles(ticks, "M1", now=now)
    assert len(candles) == 1
    assert candles[0].open == 10
    assert candles[0].high == 12
    assert candles[0].close == 12


def test_snapshot_contains_requested_timeframes_only():
    now = datetime(2026, 1, 1, 0, 2, tzinfo=timezone.utc)
    snapshot = build_snapshot(
        "XAUUSD",
        [tick(0, 10), tick(30, 12), tick(60, 11)],
        observed_at=now,
        timeframes=("M1", "M5"),
    )
    assert tuple(snapshot.candles) == ("M1", "M5")
    assert snapshot.candles["M1"][-1].close == 11
