"""Deterministic replay of a supplied market-state sequence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

from .contracts import MarketState, TradeDecision


@dataclass(frozen=True)
class ChartReplay:
    state_id: str
    before_chart: str
    entry_chart: str
    after_chart: str


@dataclass(frozen=True)
class ReplayResult:
    decisions: tuple[TradeDecision, ...]


def replay(states: Sequence[MarketState], decide: Callable[[MarketState], TradeDecision]) -> ReplayResult:
    ordered = tuple(sorted(states, key=lambda state: (state.observed_at, state.state_id)))
    return ReplayResult(tuple(decide(state) for state in ordered))


def chart_replay(state_id: str, before_chart: str, entry_chart: str, after_chart: str) -> ChartReplay:
    if not all((before_chart, entry_chart, after_chart)):
        raise ValueError("chart replay requires before, entry, and after evidence")
    return ChartReplay(state_id, before_chart, entry_chart, after_chart)
