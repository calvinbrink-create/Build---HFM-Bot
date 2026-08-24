"""Canonical clean runtime from HFM observations to the existing bot boundary.

This is the only orchestration path in the clean build.  It owns no strategy,
broker, or position-management logic.  It freezes one source dataset, builds
one market state, supplies historical evidence to the approved research
provider, validates the resulting immutable decision, and hands an executable
decision to the existing bot exactly once.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from math import sqrt
from statistics import mean
from typing import Callable, Mapping

from .calendar import TradingCalendar
from .chart_history import build_persistent_chart
from .contracts import TradeDecision, TradeOutcome
from .data_quality import DataQualityPolicy, validate_dataset
from .execution import ExecutionPort, ExecutionReceipt, outcome_from_receipt
from .hfm_data import SourceDataset
from .historical_memory import HistoricalAnalogueIndex, SimilarState, feature_vector
from .intelligence.engine import IntelligenceEngine
from .intelligence.chart import with_trade_annotations
from .intelligence.orchestrator import IntelligenceOrchestrator
from .intelligence.latency import (
    RuntimeLatencyContext,
    build_execution_latency_observation,
)
from .intelligence.provider import StructuredResearchProvider
from .intelligence.renderer import render_svg
from .intelligence.spread_intelligence import SpreadProfileBook
from .runtime_trace import RuntimeTraceEvent
from .slippage import BrokerFill
from .snapshot import (
    RESEARCH_TIMEFRAMES,
    freeze_event_snapshot,
    market_state_from_snapshot,
    snapshot_from_dataset,
)
from .store import EvidenceStore


@dataclass(frozen=True)
class RuntimeConfiguration:
    research_timeframes: tuple[str, ...] = RESEARCH_TIMEFRAMES
    max_bars_per_frame: int = 500
    analogue_limit: int = 50
    analogue_horizon_seconds: int = 300
    # This affects only what the evidence store serializes.  The complete
    # report remains intact for the in-process decision path.
    persisted_context_record_limit: int | None = None


@dataclass(frozen=True)
class RuntimeResult:
    state_id: str
    decision: TradeDecision
    receipt: ExecutionReceipt | None
    outcome: TradeOutcome
    analogue_ids: tuple[str, ...]
    lineage_id: str
    trace_id: str


class CleanRuntime:
    """One auditable clean-build runtime; all external effects are injected."""

    def __init__(
        self,
        *,
        store: EvidenceStore,
        provider: StructuredResearchProvider,
        execution: ExecutionPort,
        memory: HistoricalAnalogueIndex,
        calendar: TradingCalendar,
        quality_policy: DataQualityPolicy,
        spread_profiles: SpreadProfileBook | None = None,
        edge_statistics: Mapping[str, object] | None = None,
        configuration: RuntimeConfiguration | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self._store = store
        self._intelligence = IntelligenceOrchestrator(
            IntelligenceEngine(spread_profiles=spread_profiles),
            provider,
        )
        self._execution = execution
        self._memory = memory
        self._calendar = calendar
        self._quality_policy = quality_policy
        self._edge_statistics = dict(edge_statistics or {})
        self._configuration = configuration or RuntimeConfiguration()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def process(
        self,
        dataset: SourceDataset,
        *,
        observed_at: datetime,
        cross_asset_context: Mapping[str, float] | None = None,
    ) -> RuntimeResult:
        if dataset.symbol == "":
            raise ValueError("source dataset symbol is required")
        data_received_at = self._now()

        trace_id = _digest(("clean-runtime", dataset.dataset_id, observed_at))
        trace_sequence = 0

        def trace(stage: str, artifact_id: str | None, details: Mapping[str, object]) -> None:
            nonlocal trace_sequence
            trace_sequence += 1
            self._store.write_runtime_trace_event(
                RuntimeTraceEvent(
                    trace_id,
                    trace_sequence,
                    stage,
                    observed_at,
                    artifact_id,
                    dict(details),
                )
            )

        self._store.write_dataset(dataset)
        trace(
            "SOURCE_DATASET_STORED",
            dataset.dataset_id,
            {"symbol": dataset.symbol, "source": "HFM_MT5"},
        )
        quality = validate_dataset(
            dataset,
            observed_at=observed_at,
            policy=self._quality_policy,
            calendar=self._calendar,
        )
        quality_id = _digest((dataset.dataset_id, quality.observed_at, quality.status, quality.issues))
        self._store.write_dataset_quality(quality_id, dataset.dataset_id, quality.status, observed_at, quality)
        trace(
            "DATA_QUALITY_VALIDATED",
            quality_id,
            {"status": quality.status, "issues": quality.issues},
        )
        if quality.status != "PASS":
            self._store.write_dataset_rejection(quality_id, dataset, observed_at, quality)
            raise ValueError("source dataset failed explicit quality validation")

        snapshot = snapshot_from_dataset(
            dataset,
            observed_at=observed_at,
            required_timeframes=self._configuration.research_timeframes,
            max_bars_per_frame=self._configuration.max_bars_per_frame,
        )
        missing = tuple(
            timeframe
            for timeframe in self._configuration.research_timeframes
            if snapshot.frame_status.get(timeframe) != "COMPLETED"
        )
        if missing:
            raise ValueError(f"incomplete market snapshot: {','.join(missing)}")
        trace(
            "MARKET_SNAPSHOT_FROZEN",
            dataset.dataset_id,
            {
                "symbol": snapshot.symbol,
                "timeframes": tuple(snapshot.candles),
                "source_ids": snapshot.source_ids,
            },
        )
        market_state = market_state_from_snapshot(
            snapshot,
            payload={"dataset_id": dataset.dataset_id},
        )
        trace(
            "MULTI_TIMEFRAME_STATE_BUILT",
            market_state.state_id,
            {
                "symbol": market_state.symbol,
                "tick_at": market_state.latest_tick.timestamp.isoformat(),
                "timeframes": tuple(market_state.timeframes),
                "source_ids": market_state.source_ids,
            },
        )
        pre_event_snapshot = freeze_event_snapshot(
            event_id=market_state.state_id,
            phase="PRE_EVENT",
            state=market_state,
            captured_at=observed_at,
        )
        self._store.write_event_market_snapshot(pre_event_snapshot)
        trace(
            "PRE_EVENT_SNAPSHOT_FROZEN",
            pre_event_snapshot.snapshot_id,
            {"event_id": pre_event_snapshot.event_id, "state_id": market_state.state_id},
        )

        analysis_started_at = self._now()
        report = self._intelligence.analyse(
            snapshot,
            cross_asset_context=cross_asset_context,
        )
        analysis_completed_at = self._now()
        trace(
            "MARKET_INTELLIGENCE_BUILT",
            report.state_id,
            {"symbol": report.symbol, "evidence_count": len(report.evidence)},
        )
        self._store.write_market_state(
            snapshot,
            report,
            context_record_limit=self._configuration.persisted_context_record_limit,
        )
        trace(
            "MARKET_STATE_STORED",
            report.state_id,
            {"snapshot_frames": sum(bool(rows) for rows in snapshot.candles.values())},
        )

        current = feature_vector(
            snapshot,
            self._configuration.research_timeframes,
            cross_asset_context=cross_asset_context,
        )
        self._store.write_feature(report.state_id, "ALL", 1, current)
        trace(
            "FEATURE_VECTOR_STORED",
            report.state_id,
            {
                "feature_count": len(current.vector),
                "feature_version": current.feature_schema_version,
            },
        )
        analogues = self._memory.search(current, limit=self._configuration.analogue_limit)
        trace(
            "HISTORICAL_ANALOGUES_RETRIEVED",
            report.state_id,
            {"analogue_ids": tuple(row.state_id for row in analogues)},
        )
        statistics = {
            "historical_analogues": analogue_statistics(
                analogues,
                horizon_seconds=self._configuration.analogue_horizon_seconds,
            ),
            "validated_edges": self._edge_statistics,
        }
        try:
            intelligence = self._intelligence.decide(
                snapshot,
                report,
                dataset_id=dataset.dataset_id,
                analogue_ids=tuple(row.state_id for row in analogues),
                statistics=statistics,
                market_state=market_state,
            )
        except Exception as exc:
            self._store.audit(
                "DECISION_PROVIDER_REJECTED",
                snapshot.observed_at,
                {
                    "state_id": report.state_id,
                    "symbol": snapshot.symbol,
                    "error_type": type(exc).__name__,
                    "reason": str(exc),
                },
            )
            raise

        decision = intelligence.decision
        decision_created_at = self._now()
        request = intelligence.request
        self._store.write_decision(decision)
        trace(
            "STRUCTURED_DECISION_CREATED",
            decision.decision_id,
            {"action": decision.action, "symbol": decision.symbol},
        )
        annotated_chart = with_trade_annotations(report.chart, decision)
        stored_charts = 0
        for timeframe, rows in snapshot.candles.items():
            if not rows:
                continue
            rendered_candles = tuple(rows[-200:])
            svg = render_svg(
                rendered_candles,
                annotations=annotated_chart.annotations.get(timeframe, ()),
            )
            chart = build_persistent_chart(
                annotated_chart,
                decision,
                timeframe,
                rendered_candles,
                svg,
            )
            self._store.write_chart(
                chart.chart_id,
                chart.symbol,
                chart.observed_at,
                chart.payload,
            )
            stored_charts += 1
        trace(
            "ANNOTATED_CHARTS_STORED",
            annotated_chart.pack_id,
            {"chart_count": stored_charts, "decision_id": decision.decision_id},
        )
        validation = intelligence.validation
        trace(
            "DECISION_VALIDATED",
            decision.decision_id,
            {"valid": validation.valid, "reason": validation.reason},
        )
        receipt: ExecutionReceipt | None = None
        if not validation.valid:
            outcome = TradeOutcome(
                decision.decision_id,
                "INVALID_DECISION",
                details={"reason": validation.reason},
            )
        elif decision.action == "NO_TRADE":
            outcome = TradeOutcome(decision.decision_id, "NO_TRADE")
        else:
            if market_state.latest_tick is None:
                raise ValueError("latency instrumentation requires an exact source tick")
            latency_context = RuntimeLatencyContext(
                trace_id=trace_id,
                decision_id=decision.decision_id,
                symbol=decision.symbol,
                source_data_at=market_state.latest_tick.timestamp,
                data_received_at=data_received_at,
                analysis_started_at=analysis_started_at,
                analysis_completed_at=analysis_completed_at,
                decision_created_at=decision_created_at,
                source_ids=tuple(
                    dict.fromkeys((dataset.dataset_id, *snapshot.source_ids))
                ),
            )
            order_handoff_at = self._now()
            receipt = self._execution.submit(decision)
            fill_received_at = self._now()
            self._persist_execution_latency(
                context=latency_context,
                receipt=receipt,
                order_handoff_at=order_handoff_at,
                fill_received_at=fill_received_at,
            )
            trace(
                "EXECUTION_HANDOFF_SUBMITTED",
                decision.decision_id,
                {"action": decision.action},
            )
            outcome = outcome_from_receipt(receipt)
            self._store.write_broker_event(
                _digest((decision.decision_id, receipt.status, receipt.broker_reference, receipt.details)),
                decision.decision_id,
                receipt.status,
                snapshot.observed_at,
                receipt,
            )
            trace(
                "BROKER_RECEIPT_RECORDED",
                receipt.broker_reference,
                {"status": receipt.status, "decision_id": decision.decision_id},
            )
        if receipt is None:
            trace(
                "EXECUTION_NOT_REQUIRED",
                decision.decision_id,
                {"action": decision.action, "outcome": outcome.status},
            )
        self._store.write_outcome(outcome)
        trace(
            "OUTCOME_STORED",
            decision.decision_id,
            {"status": outcome.status},
        )

        lineage_id = _digest((dataset.dataset_id, report.state_id, decision.decision_id, outcome.status))
        self._store.write_lineage(
            lineage_id,
            report.state_id,
            decision.decision_id,
            decision.decision_id,
            {
                "dataset_id": dataset.dataset_id,
                "state_id": report.state_id,
                "decision": decision,
                "outcome": outcome,
                "source_ids": snapshot.source_ids,
                "analogue_ids": request.analogue_ids,
            },
        )
        trace(
            "LINEAGE_STORED",
            lineage_id,
            {"state_id": report.state_id, "decision_id": decision.decision_id},
        )
        self._memory.add(current)
        trace(
            "MEMORY_UPDATED",
            report.state_id,
            {"feature_version": current.feature_schema_version},
        )
        trace(
            "PROCESS_COMPLETED",
            lineage_id,
            {"outcome": outcome.status},
        )
        return RuntimeResult(
            report.state_id,
            decision,
            receipt,
            outcome,
            request.analogue_ids,
            lineage_id,
            trace_id,
        )

    def _persist_execution_latency(
        self,
        *,
        context: RuntimeLatencyContext,
        receipt: ExecutionReceipt,
        order_handoff_at: datetime,
        fill_received_at: datetime,
    ) -> None:
        if receipt.status != "FILLED":
            return
        fills = tuple(
            BrokerFill.from_payload(value)
            for value in receipt.details.get("fills", ())
            if isinstance(value, Mapping)
        )
        if not fills or any(fill.requested_at is None for fill in fills):
            raise ValueError("FILLED receipt lacks exact broker latency evidence")
        request_hash = str(receipt.details.get("request_hash") or "")
        edge_id = str(receipt.details.get("edge_id") or "")
        if not request_hash or not edge_id:
            raise ValueError("FILLED receipt lacks immutable request or edge identity")
        expected_keys = {(fill.order_ticket, fill.deal_ticket) for fill in fills}
        existing = self._store.load_execution_latency_observations(context.decision_id)
        existing_keys = {(row.order_ticket, row.deal_ticket) for row in existing}
        if existing:
            if existing_keys != expected_keys:
                raise ValueError("existing latency evidence conflicts with broker fills")
            return
        for fill in fills:
            self._store.write_execution_latency_observation(
                build_execution_latency_observation(
                    context=context,
                    request_hash=request_hash,
                    edge_id=edge_id,
                    order_ticket=fill.order_ticket,
                    deal_ticket=fill.deal_ticket,
                    order_handoff_at=order_handoff_at,
                    fill_received_at=fill_received_at,
                    broker_requested_at=fill.requested_at,
                    broker_filled_at=fill.filled_at,
                    broker_source_id=fill.source_id,
                )
            )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("runtime clock must return timezone-aware timestamps")
        return value.astimezone(timezone.utc)


def analogue_statistics(
    analogues: tuple[SimilarState, ...],
    *,
    horizon_seconds: int,
) -> Mapping[str, object]:
    """Summarise matching historical paths without inventing fill evidence."""

    selected = tuple(
        item
        for analogue in analogues
        if analogue.path is not None
        for item in analogue.path.outcomes
        if item.horizon_seconds == horizon_seconds
    )
    if not selected:
        return {
            "horizon_seconds": horizon_seconds,
            "sample_size": 0,
            "status": "INSUFFICIENT_HISTORICAL_PATHS",
            "fidelity": "BAR_OHLC_NO_EXECUTABLE_TICKS",
        }
    returns = tuple(item.close_return for item in selected)
    average = mean(returns)
    standard_error = 0.0
    if len(returns) > 1:
        variance = sum((value - average) ** 2 for value in returns) / (len(returns) - 1)
        standard_error = sqrt(variance / len(returns))
    return {
        "horizon_seconds": horizon_seconds,
        "sample_size": len(returns),
        "mean_close_return": average,
        "positive_probability": sum(value > 0 for value in returns) / len(returns),
        "mean_mfe_return": mean(item.mfe_return for item in selected),
        "mean_mae_return": mean(item.mae_return for item in selected),
        "confidence_interval_95": (average - 1.96 * standard_error, average + 1.96 * standard_error),
        "status": "OBSERVED_HISTORICAL_ANALOGUES",
        "fidelity": "BAR_OHLC_NO_EXECUTABLE_TICKS",
    }


def _digest(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")).hexdigest()
