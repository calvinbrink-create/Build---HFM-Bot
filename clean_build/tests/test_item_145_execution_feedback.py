from datetime import datetime, timedelta, timezone

from cipherfx_clean.contracts import TradeDecision, TradeOutcome
from cipherfx_clean.execution import (
    BotExecutionHandoff,
    BotExecutionResponse,
    InMemoryExecutionJournal,
    instruction_from_decision,
)
from cipherfx_clean.feedback import ClosedTrade, LearningFeedback, PostTradeReview
from cipherfx_clean.intelligence.runtime_contracts import StructuredOutput
from cipherfx_clean.management import Position, PositionManager, PositionMark
from cipherfx_clean.operations import BrokerContract, OperationalRequest, validate_operational
from cipherfx_clean.replay import chart_replay
from cipherfx_clean.slippage import BrokerFill


def _decision(at):
    return TradeDecision(
        "d1", "UK100", "BUY", at, evidence=("chart-1",), edge_id="edge-1",
        entry=100.0, stop=99.0, target=102.0, edge_version=1, confidence=.7,
        probability=.6, expected_value=.1, setup_type="SETUP", entry_area=(99.9, 100.1),
        stop_concept="RANGE", target_concept="LIQUIDITY", reason_codes=("EVIDENCE",),
    )


def test_instruction_is_idempotent_and_management_stays_on_position():
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    decision = _decision(at)
    instruction = instruction_from_decision(decision)
    calls = []

    class Bot:
        def submit(self, item):
            calls.append(item.decision_id)
            fill = BrokerFill("o1", "deal1", 100.0, 100.1, 1.0, at + timedelta(seconds=2), "test", at + timedelta(seconds=1))
            return BotExecutionResponse(item.decision_id, "FILLED", "ref1", (fill,))

    handoff = BotExecutionHandoff(Bot(), InMemoryExecutionJournal())
    first = handoff.submit(decision)
    second = handoff.submit(decision)
    assert instruction.side == "BUY"
    assert first.status == "FILLED" and second == first and calls == ["d1"]

    position = Position("p1", "d1", "UK100", "BUY", 1.0, 100.0, 99.0, 102.0)
    action = PositionManager(type("Broker", (), {})()).observe(position, 102.0)
    assert action.action == "CLOSE" and action.position_id == "p1"


def test_operational_feedback_review_and_replay_contracts():
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    contract = BrokerContract("UK100", .1, 1.0, 1.0, .01, 100.0, .01, 10.0)
    request = OperationalRequest("UK100", 1.0, .1, .2, 10.0, 100.0, False, True, True)
    assert validate_operational(request, contract).status == "PASS"
    assert validate_operational(OperationalRequest("UK100", 1.005, 1.0, .2, 110.0, 100.0, True, True, True), contract).status == "EXECUTION_BLOCKED"

    trade = ClosedTrade("d1", "edge-1", "BUY", .8, .05, at, "TREND", "LONDON", ("EVIDENCE",), 1.1, -.2, 180, .1)
    feedback = LearningFeedback()
    assert feedback.accept(TradeOutcome("d1", "CLOSED"))
    assert feedback.to_state_outcome(trade, outcome_kind="WINNER").r_multiple == .8
    assert PostTradeReview().review(trade, exit_reason="TARGET_REACHED", mfe=1.1, mae=-.2, slippage=.1, state_id="s1").state_id == "s1"

    replay = chart_replay("d1", "before", "entry", "after")
    output = StructuredOutput("d1", "BUY", .7, "SETUP", (99.9, 100.1), "RANGE", "LIQUIDITY", ("chart-1",), "edge-1", ("EVIDENCE",))
    assert replay.after_chart == "after" and output.edge_id == "edge-1"
