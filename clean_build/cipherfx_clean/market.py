"""Fresh completed-bar market snapshot builder for the Friday scope."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Iterable, Sequence

from .contracts import Candle, MarketSnapshot, RawTick


TIMEFRAMES = ("1s", "5s", "15s", "M1", "M3", "M5", "M15", "M30", "H1", "H4", "D1")
SECONDS = {"1s": 1, "5s": 5, "15s": 15, "M1": 60, "M3": 180, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600, "H4": 14400, "D1": 86400}


class TickQuality(str, Enum):
    VALID = "VALID"
    MISSING = "MISSING"
    STALE = "STALE"
    DUPLICATE = "DUPLICATE"
    MALFORMED = "MALFORMED"


class TickValidation(tuple):
    __slots__ = ()

    def __new__(cls, quality: TickQuality, reason: str):
        return tuple.__new__(cls, (quality, reason))

    @property
    def quality(self) -> TickQuality:
        return self[0]

    @property
    def reason(self) -> str:
        return self[1]


class SnapshotQuality(str, Enum):
    FRESH = "FRESH"
    STALE = "STALE"
    MISSING = "MISSING"


def exact_tick_mid(tick: RawTick) -> float:
    return float((Decimal(str(tick.bid)) + Decimal(str(tick.ask))) / Decimal("2"))


def exact_tick_change(previous: RawTick, current: RawTick) -> float:
    previous_mid = (Decimal(str(previous.bid)) + Decimal(str(previous.ask))) / Decimal("2")
    current_mid = (Decimal(str(current.bid)) + Decimal(str(current.ask))) / Decimal("2")
    return float(current_mid - previous_mid)


def exact_tick_direction(previous: RawTick, current: RawTick) -> str:
    change = exact_tick_change(previous, current)
    return "UP" if change > 0 else "DOWN" if change < 0 else "UNCHANGED"


def assess_snapshot(snapshot: MarketSnapshot, *, max_age: timedelta) -> dict[str, SnapshotQuality]:
    """Describe freshness per timeframe without turning it into a trade veto."""
    result: dict[str, SnapshotQuality] = {}
    observed = snapshot.observed_at.astimezone(timezone.utc)
    for timeframe, rows in snapshot.candles.items():
        if not rows:
            result[timeframe] = SnapshotQuality.MISSING
            continue
        age = observed - rows[-1].end.astimezone(timezone.utc)
        result[timeframe] = SnapshotQuality.FRESH if age <= max_age else SnapshotQuality.STALE
    return result


def _floor(timestamp: datetime, seconds: int) -> datetime:
    utc = timestamp.astimezone(timezone.utc)
    epoch = int(utc.timestamp())
    return datetime.fromtimestamp(epoch - epoch % seconds, timezone.utc)


def validate_tick(
    tick: RawTick,
    *,
    now: datetime,
    max_age: timedelta,
    previous: RawTick | None = None,
) -> TickValidation:
    if not tick.symbol or tick.bid <= 0 or tick.ask <= 0 or tick.ask < tick.bid:
        return TickValidation(TickQuality.MALFORMED, "INVALID_SYMBOL_OR_QUOTE")
    age = now.astimezone(timezone.utc) - tick.timestamp.astimezone(timezone.utc)
    if age > max_age or age.total_seconds() < -max_age.total_seconds():
        return TickValidation(TickQuality.STALE, "OUTSIDE_FRESHNESS_WINDOW")
    if previous is not None and tick.timestamp <= previous.timestamp:
        return TickValidation(TickQuality.DUPLICATE, "NON_INCREASING_TIMESTAMP")
    return TickValidation(TickQuality.VALID, "VALID_TICK")


def build_completed_candles(
    ticks: Iterable[RawTick], timeframe: str, *, now: datetime
) -> tuple[Candle, ...]:
    if timeframe not in SECONDS:
        raise ValueError(f"unsupported timeframe: {timeframe}")
    seconds = SECONDS[timeframe]
    buckets: dict[datetime, list[RawTick]] = {}
    for tick in sorted(ticks, key=lambda item: item.timestamp):
        start = _floor(tick.timestamp, seconds)
        if start + timedelta(seconds=seconds) <= now.astimezone(timezone.utc):
            buckets.setdefault(start, []).append(tick)
    result: list[Candle] = []
    for start, group in sorted(buckets.items()):
        prices = [item.bid for item in group]
        result.append(Candle(group[0].symbol, timeframe, start, start + timedelta(seconds=seconds), prices[0], max(prices), min(prices), prices[-1], len(group)))
    return tuple(result)


def build_snapshot(symbol: str, ticks: Sequence[RawTick], observed_at: datetime, timeframes: Sequence[str] = TIMEFRAMES) -> MarketSnapshot:
    ordered = tuple(sorted((item for item in ticks if item.symbol == symbol), key=lambda item: item.timestamp))
    now = observed_at.astimezone(timezone.utc)
    output: dict[str, tuple[Candle, ...]] = {}
    for timeframe in timeframes:
        output[timeframe] = build_completed_candles(ordered, timeframe, now=now)
    return MarketSnapshot(symbol, now, ordered, output)
