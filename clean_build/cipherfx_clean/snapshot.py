"""Provenance-aware frozen market snapshots from validated source data."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Mapping, Sequence

from .contracts import Candle, EventMarketSnapshot, MarketSnapshot, MarketState, RawTick
from .hfm_data import SourceDataset, timeframe_end
from .market import build_completed_candles


RESEARCH_TIMEFRAMES = ("MN1", "W1", "D1", "H4", "H1", "M30", "M15", "M5", "M3", "M1")
MICRO_TIMEFRAMES = ("15s", "5s", "1s")
COMPLETE_TIMEFRAMES = RESEARCH_TIMEFRAMES + MICRO_TIMEFRAMES
MARKET_STATE_TIMEFRAMES = ("M1", "M5", "M15", "H1", "H4", "D1")
EVENT_SNAPSHOT_PHASES = ("PRE_EVENT", "ENTRY", "POST_EVENT")


def snapshot_from_dataset(
    dataset: SourceDataset,
    *,
    observed_at: datetime,
    required_timeframes: Sequence[str] = RESEARCH_TIMEFRAMES,
    include_micro: bool = True,
    max_bars_per_frame: int = 500,
) -> MarketSnapshot:
    observed = observed_at.astimezone(timezone.utc)
    if max_bars_per_frame <= 0:
        raise ValueError("max_bars_per_frame must be positive")
    candles: dict[str, tuple[Candle, ...]] = {key: tuple(value[-max_bars_per_frame:]) for key, value in dataset.bars.items()}
    status: dict[str, str] = {}
    for timeframe in required_timeframes:
        rows = candles.get(timeframe, ())
        if not rows:
            status[timeframe] = "MISSING"
        elif rows[-1].end > observed:
            status[timeframe] = "FORMING"
        else:
            status[timeframe] = "COMPLETED"
    if include_micro:
        for timeframe in MICRO_TIMEFRAMES:
            rows = build_completed_candles(dataset.ticks, timeframe, now=observed)
            candles[timeframe] = tuple(_with_derived_source(row, dataset.dataset_id) for row in rows)
            status[timeframe] = "COMPLETED" if rows else "MISSING"
    source_ids = (dataset.dataset_id,) + tuple(
        item for item in sorted(set(dataset.source_hashes.values())) if item != dataset.dataset_id
    )
    return MarketSnapshot(
        dataset.symbol,
        observed,
        dataset.ticks,
        candles,
        status,
        source_ids,
        dict(dataset.contract),
    )


def market_state_from_snapshot(
    snapshot: MarketSnapshot,
    *,
    payload: Mapping[str, object] | None = None,
) -> MarketState:
    """Freeze the current tick and completed higher-timeframe context."""

    if not snapshot.ticks:
        raise ValueError("multi-timeframe market state requires a current tick")
    latest_tick = max(snapshot.ticks, key=lambda row: row.timestamp)
    if latest_tick.symbol != snapshot.symbol or latest_tick.timestamp > snapshot.observed_at:
        raise ValueError("multi-timeframe market state has an invalid current tick")
    frames: dict[str, Candle] = {}
    for timeframe in MARKET_STATE_TIMEFRAMES:
        rows = tuple(snapshot.candles.get(timeframe, ()))
        if snapshot.frame_status.get(timeframe) != "COMPLETED" or not rows:
            raise ValueError(f"multi-timeframe market state missing completed {timeframe}")
        candle = rows[-1]
        if candle.symbol != snapshot.symbol or candle.end > snapshot.observed_at:
            raise ValueError(f"multi-timeframe market state has invalid {timeframe}")
        if not candle.source or not candle.source_id:
            raise ValueError(f"multi-timeframe market state lacks {timeframe} provenance")
        frames[timeframe] = candle
    state_id = snapshot_state_id(snapshot)
    return MarketState(
        symbol=snapshot.symbol,
        observed_at=snapshot.observed_at,
        payload=dict(payload or {}),
        state_id=state_id,
        latest_tick=latest_tick,
        timeframes=frames,
        source_ids=tuple(snapshot.source_ids),
    )


def snapshot_state_id(snapshot: MarketSnapshot) -> str:
    source = tuple(snapshot.source_ids) or tuple(
        row.source_id
        for timeframe in sorted(snapshot.candles)
        for row in snapshot.candles[timeframe]
        if row.source_id
    )
    value = (snapshot.symbol, snapshot.observed_at.isoformat(), source)
    return sha256(
        json.dumps(value, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def freeze_event_snapshot(
    *,
    event_id: str,
    phase: str,
    state: MarketState,
    captured_at: datetime,
    decision_id: str | None = None,
) -> EventMarketSnapshot:
    if not event_id or phase not in EVENT_SNAPSHOT_PHASES:
        raise ValueError("event snapshot requires an event id and valid phase")
    if not state.state_id or state.latest_tick is None or not state.timeframes:
        raise ValueError("event snapshot requires a complete multi-timeframe state")
    if captured_at.tzinfo is None or captured_at.utcoffset() is None:
        raise ValueError("event snapshot capture time must be timezone-aware")
    if phase in {"ENTRY", "POST_EVENT"} and not decision_id:
        raise ValueError(f"{phase} event snapshot requires a decision id")
    canonical_state = json.dumps(
        state,
        default=_snapshot_json_default,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    snapshot_id = _event_snapshot_id(
        event_id=event_id,
        phase=phase,
        state_id=state.state_id,
        observed_at=state.observed_at,
        captured_at=captured_at,
        decision_id=decision_id,
        canonical_state=canonical_state,
    )
    return EventMarketSnapshot(
        snapshot_id=snapshot_id,
        event_id=event_id,
        phase=phase,
        symbol=state.symbol,
        observed_at=state.observed_at,
        captured_at=captured_at,
        state_id=state.state_id,
        decision_id=decision_id,
        source_ids=state.source_ids,
        canonical_state=canonical_state,
    )


def validate_event_snapshot_sequence(
    snapshots: Sequence[EventMarketSnapshot],
) -> tuple[EventMarketSnapshot, EventMarketSnapshot, EventMarketSnapshot]:
    rows = tuple(snapshots)
    if tuple(item.phase for item in rows) != EVENT_SNAPSHOT_PHASES:
        raise ValueError("event snapshot sequence must be PRE_EVENT, ENTRY, POST_EVENT")
    if len({item.event_id for item in rows}) != 1 or len({item.symbol for item in rows}) != 1:
        raise ValueError("event snapshot sequence identity mismatch")
    if not rows[1].decision_id or rows[1].decision_id != rows[2].decision_id:
        raise ValueError("entry and post-event snapshots must share one decision id")
    observed = tuple(item.observed_at for item in rows)
    captured = tuple(item.captured_at for item in rows)
    if observed != tuple(sorted(observed)) or captured != tuple(sorted(captured)):
        raise ValueError("event snapshot sequence is not chronological")
    if len({item.snapshot_id for item in rows}) != len(rows):
        raise ValueError("event snapshot sequence contains duplicate snapshots")
    for item in rows:
        expected = _event_snapshot_id(
            event_id=item.event_id,
            phase=item.phase,
            state_id=item.state_id,
            observed_at=item.observed_at,
            captured_at=item.captured_at,
            decision_id=item.decision_id,
            canonical_state=item.canonical_state,
        )
        if item.snapshot_id != expected:
            raise ValueError(f"event snapshot identity mismatch for {item.phase}")
    return rows


def _event_snapshot_id(
    *,
    event_id: str,
    phase: str,
    state_id: str,
    observed_at: datetime,
    captured_at: datetime,
    decision_id: str | None,
    canonical_state: str,
) -> str:
    return sha256(
        json.dumps(
            (
                event_id,
                phase,
                state_id,
                observed_at.isoformat(),
                captured_at.isoformat(),
                decision_id,
                canonical_state,
            ),
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _snapshot_json_default(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "__dataclass_fields__"):
        return {
            key: getattr(value, key)
            for key in value.__dataclass_fields__
        }
    raise TypeError(f"unsupported event snapshot value: {type(value)!r}")


def _with_derived_source(candle: Candle, dataset_id: str) -> Candle:
    return Candle(
        symbol=candle.symbol,
        timeframe=candle.timeframe,
        start=candle.start,
        end=candle.end,
        open=candle.open,
        high=candle.high,
        low=candle.low,
        close=candle.close,
        tick_count=candle.tick_count,
        tick_volume=float(candle.tick_count),
        source="DERIVED_FROM_HFM_TICKS",
        source_id=f"{dataset_id}:{candle.timeframe}:{int(candle.start.timestamp())}",
    )
