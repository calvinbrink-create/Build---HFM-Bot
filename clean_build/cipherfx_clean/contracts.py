"""Fresh immutable contracts for the clean Friday build.

These are the only objects allowed to cross the clean build boundaries.  The
contracts deliberately contain no broker, strategy, or legacy-runtime code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Mapping, Tuple

Side = Literal["BUY", "SELL"]


@dataclass(frozen=True)
class RawTick:
    symbol: str
    timestamp: datetime
    bid: float
    ask: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        return self.ask - self.bid


@dataclass(frozen=True)
class Candle:
    symbol: str
    timeframe: str
    start: datetime
    end: datetime
    open: float
    high: float
    low: float
    close: float
    tick_count: int = 0
    tick_volume: float = 0.0
    real_volume: float = 0.0
    spread_points: float = 0.0
    source: str = "TICKS"
    source_id: str = ""


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    observed_at: datetime
    ticks: tuple[RawTick, ...]
    candles: Mapping[str, tuple[Candle, ...]] = field(default_factory=dict)
    frame_status: Mapping[str, str] = field(default_factory=dict)
    source_ids: Tuple[str, ...] = ()
    contract: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class MarketState:
    symbol: str
    observed_at: datetime
    payload: Mapping[str, object] = field(default_factory=dict)
    state_id: str = ""
    latest_tick: RawTick | None = None
    timeframes: Mapping[str, Candle] = field(default_factory=dict)
    source_ids: Tuple[str, ...] = ()


@dataclass(frozen=True)
class EventMarketSnapshot:
    snapshot_id: str
    event_id: str
    phase: Literal["PRE_EVENT", "ENTRY", "POST_EVENT"]
    symbol: str
    observed_at: datetime
    captured_at: datetime
    state_id: str
    decision_id: str | None
    source_ids: Tuple[str, ...]
    canonical_state: str


@dataclass(frozen=True)
class TradeDecision:
    decision_id: str
    symbol: str
    action: Literal["BUY", "SELL", "NO_TRADE"]
    created_at: datetime
    evidence: Tuple[str, ...] = ()
    edge_id: str = ""
    entry: float | None = None
    stop: float | None = None
    target: float | None = None
    edge_version: int | None = None
    confidence: float | None = None
    probability: float | None = None
    expected_value: float | None = None
    setup_type: str = ""
    entry_area: tuple[float, float] | None = None
    stop_concept: str = ""
    target_concept: str = ""
    reason_codes: Tuple[str, ...] = ()
    schema_version: int = 1


@dataclass(frozen=True)
class TradeOutcome:
    decision_id: str
    status: str
    broker_reference: str | None = None
    details: Mapping[str, object] = field(default_factory=dict)
