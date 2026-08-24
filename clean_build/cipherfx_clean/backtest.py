"""Deterministic executable-price backtest primitives for research only."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Literal, Sequence


@dataclass(frozen=True)
class HistoricalQuote:
    timestamp: datetime
    bid: float
    ask: float


@dataclass(frozen=True)
class BacktestConfig:
    edge_id: str
    side: Literal["BUY", "SELL"]
    stop_distance: float
    target_distance: float
    commission_per_trade: float
    swap_per_period: float
    latency_seconds: int
    slippage_points: float = 0.0
    max_holding_seconds: int | None = None


@dataclass(frozen=True)
class BacktestTrade:
    entry_time: datetime
    exit_time: datetime
    side: str
    entry_price: float
    exit_price: float
    gross_r: float
    costs_r: float
    net_r: float
    exit_reason: str


@dataclass(frozen=True)
class BacktestReport:
    edge_id: str
    trades: tuple[BacktestTrade, ...]
    total_net_r: float
    win_rate: float
    max_drawdown_r: float
    missed_signals: int


def run_backtest(
    quotes: Sequence[HistoricalQuote],
    config: BacktestConfig,
    setup: Callable[[Sequence[HistoricalQuote], int], bool],
) -> BacktestReport:
    rows = tuple(sorted(quotes, key=lambda item: item.timestamp))
    trades: list[BacktestTrade] = []
    missed = 0
    index = 0
    equity = 0.0
    peak = 0.0
    drawdown = 0.0
    while index < len(rows) - 1:
        visible = rows[:index + 1]
        if not setup(visible, len(visible) - 1):
            index += 1
            continue
        signal_quote = rows[index]
        entry_index = next(
            (
                future_index
                for future_index in range(index, len(rows))
                if (rows[future_index].timestamp - signal_quote.timestamp).total_seconds() >= config.latency_seconds
            ),
            None,
        )
        if entry_index is None:
            missed += 1
            break
        entry_quote = rows[entry_index]
        entry = (entry_quote.ask + config.slippage_points) if config.side == "BUY" else (entry_quote.bid - config.slippage_points)
        end = None
        reason = "DATA_END"
        for future_index, future in enumerate(rows[entry_index + 1:], entry_index + 1):
            if config.max_holding_seconds is not None and (future.timestamp - entry_quote.timestamp).total_seconds() > config.max_holding_seconds:
                end, reason = rows[future_index - 1], "TIME_EXIT"
                break
            price = future.bid if config.side == "BUY" else future.ask
            move = price - entry if config.side == "BUY" else entry - price
            if move <= -config.stop_distance:
                end, reason = future, "STOP"
                break
            if move >= config.target_distance:
                end, reason = future, "TARGET"
                break
        if end is None:
            end = rows[-1]
        exit_price = (end.bid - config.slippage_points) if config.side == "BUY" else (end.ask + config.slippage_points)
        gross = (exit_price - entry) / config.stop_distance if config.side == "BUY" else (entry - exit_price) / config.stop_distance
        costs = config.commission_per_trade + config.swap_per_period
        net = gross - costs
        trade = BacktestTrade(entry_quote.timestamp, end.timestamp, config.side, entry, exit_price, gross, costs, net, reason)
        trades.append(trade)
        equity += net
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
        index = next(item for item in range(entry_index, len(rows)) if rows[item] is end) + 1
    return BacktestReport(config.edge_id, tuple(trades), sum(item.net_r for item in trades), sum(item.net_r > 0 for item in trades) / len(trades) if trades else 0.0, drawdown, missed)
