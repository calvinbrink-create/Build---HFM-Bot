"""Canonical intelligence path: snapshot -> analysis -> research -> decision."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Mapping

from ..contracts import MarketState, TradeDecision
from ..market import MarketSnapshot
from .engine import IntelligenceEngine
from .model import IntelligenceReport
from .provider import ResearchRequest, StructuredResearchProvider
from ..validation import ValidationResult, validate_decision


@dataclass(frozen=True)
class IntelligenceResult:
    report: IntelligenceReport
    state: MarketState
    request: ResearchRequest
    decision: TradeDecision
    validation: ValidationResult


class IntelligenceOrchestrator:
    """Owns analysis and immutable decision creation, never execution."""

    def __init__(self, engine: IntelligenceEngine, provider: StructuredResearchProvider):
        self._engine = engine
        self._provider = provider

    def analyse(
        self,
        snapshot: MarketSnapshot,
        *,
        cross_asset_context: Mapping[str, float] | None = None,
    ) -> IntelligenceReport:
        return self._engine.analyse(
            snapshot,
            cross_asset_context=cross_asset_context,
        )

    def decide(
        self,
        snapshot: MarketSnapshot,
        report: IntelligenceReport,
        *,
        dataset_id: str,
        analogue_ids: tuple[str, ...],
        statistics: Mapping[str, object],
        market_state: MarketState | None = None,
    ) -> IntelligenceResult:
        if report.symbol != snapshot.symbol or report.observed_at != snapshot.observed_at.isoformat():
            raise ValueError("snapshot and intelligence identity mismatch")
        request = ResearchRequest(
            symbol=snapshot.symbol,
            observed_at=snapshot.observed_at,
            report=asdict(report),
            analogue_ids=analogue_ids,
            statistics=statistics,
        )
        state_payload = {
            "state_id": report.state_id,
            "dataset_id": dataset_id,
            "report": report,
            "analogue_ids": analogue_ids,
            "statistics": statistics,
        }
        if market_state is None:
            state = MarketState(snapshot.symbol, snapshot.observed_at, state_payload)
        else:
            if (
                market_state.symbol != snapshot.symbol
                or market_state.observed_at != snapshot.observed_at
                or market_state.state_id != report.state_id
            ):
                raise ValueError("multi-timeframe state and intelligence identity mismatch")
            state = MarketState(
                symbol=market_state.symbol,
                observed_at=market_state.observed_at,
                payload=state_payload,
                state_id=market_state.state_id,
                latest_tick=market_state.latest_tick,
                timeframes=market_state.timeframes,
                source_ids=market_state.source_ids,
            )
        decision = self._provider.decide(state, request)
        return IntelligenceResult(
            report,
            state,
            request,
            decision,
            validate_decision(decision),
        )
