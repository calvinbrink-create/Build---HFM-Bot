from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

SIDES = frozenset({"BUY", "SELL"})
TERMINAL_STATES = frozenset({"FILLED", "REJECTED", "EXPIRED", "CLOSED"})


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Candle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass(frozen=True)
class Frame:
    timeframe: str
    candles: tuple[Candle, ...]

    @property
    def last(self) -> Candle | None:
        return self.candles[-1] if self.candles else None


@dataclass(frozen=True)
class Tick:
    symbol: str
    bid: float
    ask: float
    timestamp: datetime
    received_at: datetime

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    asset_class: str
    frames: dict[str, Frame]
    tick: Tick
    captured_at: datetime
    freshness: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TimeframeAnalysis:
    timeframe: str
    side: str
    bar_open_time: datetime
    bar_close_time: datetime
    features: dict[str, bool]
    metrics: dict[str, float | str]
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class MarketAnalysis:
    symbol: str
    asset_class: str
    side: str | None
    h4: TimeframeAnalysis
    h1: TimeframeAnalysis
    m15: TimeframeAnalysis
    m5: TimeframeAnalysis
    stop_distance: float
    target_r: float
    strategy_name: str
    analyzed_at: datetime
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ScoreResult:
    symbol: str
    asset_class: str
    total: float
    minimum: float
    passed: bool
    components: dict[str, float]
    timeframe_scores: dict[str, float]
    reasons: tuple[str, ...] = ()
    scored_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True)
class TradeProposal:
    proposal_id: str
    symbol: str
    asset_class: str
    side: str
    entry_price: float
    stop_loss: float
    take_profit: float
    volume_hint: float
    strategy_name: str
    score: float
    score_components: dict[str, float]
    created_at: datetime
    expires_at: datetime
    context: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    probability: float = 0.0
    reasoning: tuple[str, ...] = ()
    risk_amount: float = 0.0


@dataclass(frozen=True)
class ExecutionResult:
    proposal_id: str
    status: str
    symbol: str
    side: str
    volume: float
    order_ticket: int = 0
    deal_ticket: int = 0
    position_ticket: int = 0
    fill_price: float = 0.0
    reason: str = ""
    submitted_at: datetime = field(default_factory=utc_now)
    filled_at: datetime | None = None


@dataclass(frozen=True)
class FeedbackRecord:
    trade_id: str
    symbol: str
    side: str
    result_r: float
    realized_pnl: float
    closed_at: datetime
    exit_reason: str
