from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from cipherfx_clean.contracts import TradeDecision
from cipherfx_clean.execution import (
    BotExecutionHandoff,
    BotExecutionResponse,
    InMemoryExecutionJournal,
)
from cipherfx_clean.intelligence.latency import (
    RuntimeLatencyContext,
    build_execution_latency_observation,
)
from cipherfx_clean.slippage import BrokerFill
from cipherfx_clean.store import EvidenceStore


NOW = datetime(2026, 8, 24, 9, 0, tzinfo=timezone.utc)


def context() -> RuntimeLatencyContext:
    return RuntimeLatencyContext(
        trace_id="trace-c033",
        decision_id="decision-c033",
        symbol="USA500",
        source_data_at=NOW - timedelta(milliseconds=20),
        data_received_at=NOW,
        analysis_started_at=NOW + timedelta(milliseconds=1),
        analysis_completed_at=NOW + timedelta(milliseconds=6),
        decision_created_at=NOW + timedelta(milliseconds=8),
        source_ids=("hfm-tick:1", "dataset:1"),
    )


def observation():
    return build_execution_latency_observation(
        context=context(),
        request_hash="request-c033",
        edge_id="edge-c033-v1",
        order_ticket="100",
        deal_ticket="200",
        order_handoff_at=NOW + timedelta(milliseconds=10),
        fill_received_at=NOW + timedelta(milliseconds=30),
        broker_requested_at=NOW + timedelta(milliseconds=11),
        broker_filled_at=NOW + timedelta(milliseconds=14),
        broker_source_id="hfm-deals:200",
    )


def test_latency_keeps_local_and_broker_clock_domains_separate():
    row = observation()
    assert row.source_age_at_receive_seconds == pytest.approx(0.020)
    assert row.durations_seconds == pytest.approx(
        {
            "receive_to_analysis_start": 0.001,
            "analysis": 0.005,
            "analysis_to_decision": 0.002,
            "decision_to_order_handoff": 0.002,
            "order_handoff_to_fill_received": 0.020,
            "local_data_to_fill_received": 0.030,
            "broker_request_to_fill": 0.003,
        }
    )
    with pytest.raises(FrozenInstanceError):
        row.symbol = "XAUUSD"


def test_latency_persistence_is_immutable_and_survives_restart(tmp_path):
    path = tmp_path / "latency.sqlite3"
    first = EvidenceStore(path)
    first.write_execution_latency_observation(observation())
    first.write_execution_latency_observation(observation())
    first.close()

    second = EvidenceStore(path)
    loaded = second.load_execution_latency_observations("decision-c033")
    assert loaded == (observation(),)
    assert second.integrity()
    second.close()


def test_filled_execution_requires_exact_broker_request_time():
    decision = TradeDecision(
        decision_id="decision-c033",
        symbol="USA500",
        action="BUY",
        created_at=NOW,
        evidence=("edge:approved",),
        edge_id="edge-c033-v1",
        entry=100.0,
        stop=99.0,
        target=102.0,
        edge_version=1,
        confidence=0.7,
        probability=0.6,
        setup_type="LATENCY_TEST",
        reason_codes=("EDGE_APPROVED",),
    )

    class MissingTimingBot:
        def submit(self, instruction):
            return BotExecutionResponse(
                instruction.decision_id,
                "FILLED",
                "mt5:200",
                fills=(
                    BrokerFill(
                        "100",
                        "200",
                        100.0,
                        100.1,
                        1.0,
                        NOW + timedelta(milliseconds=3),
                        "hfm-deals:200",
                    ),
                ),
            )

    receipt = BotExecutionHandoff(
        MissingTimingBot(),
        InMemoryExecutionJournal(),
    ).submit(decision)
    assert receipt.status == "ERROR"
    assert receipt.details["reason"] == "BROKER_LATENCY_EVIDENCE_INCOMPLETE"
