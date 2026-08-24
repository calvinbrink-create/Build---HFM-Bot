from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cipherfx_clean.contracts import TradeDecision
from cipherfx_clean.execution import (
    BotExecutionHandoff,
    BotExecutionResponse,
    InMemoryExecutionJournal,
    instruction_from_decision,
)
from cipherfx_clean.slippage import BrokerFill
from cipherfx_clean.store import EvidenceStore


def approved(decision_id="decision-1", side="BUY", target=None):
    stop = 9.0 if side == "BUY" else 11.0
    target = (12.0 if side == "BUY" else 8.0) if target is None else target
    return TradeDecision(
        decision_id=decision_id,
        symbol="XAUUSD",
        action=side,
        created_at=datetime(2026, 8, 23, 9, 0, tzinfo=timezone.utc),
        evidence=("chart:1", "edge-statistics:1"),
        edge_id="edge-7-v3",
        entry=10.0,
        stop=stop,
        target=target,
        edge_version=3,
        confidence=.7,
        probability=.62,
        expected_value=.2,
        setup_type="TESTED_SETUP",
        entry_area=(9.9, 10.1),
        stop_concept="STRUCTURE_INVALIDATION",
        target_concept="OPPOSING_LIQUIDITY",
        reason_codes=("EDGE_APPROVED",),
    )


class RecordingBot:
    def __init__(self, status="FILLED"):
        self.status = status
        self.calls = []

    def submit(self, instruction):
        self.calls.append(instruction)
        return BotExecutionResponse(
            decision_id=instruction.decision_id,
            status=self.status,
            broker_reference="mt5:200",
            fills=(
                BrokerFill(
                    "100",
                    "200",
                    10.0,
                    10.1,
                    1.0,
                    datetime(2026, 8, 23, 9, 0, 1, tzinfo=timezone.utc),
                    "hfm-deals:200",
                    requested_at=datetime(2026, 8, 23, 9, 0, tzinfo=timezone.utc),
                ),
            ) if self.status == "FILLED" else (),
            reason="BROKER_ACCEPTED",
        )


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_buy_and_sell_are_forwarded_without_rescoring(side):
    bot = RecordingBot()
    decision = approved(side=side)
    receipt = BotExecutionHandoff(bot, InMemoryExecutionJournal()).submit(decision)
    assert receipt.status == "FILLED"
    assert len(bot.calls) == 1
    assert bot.calls[0].side == side
    assert bot.calls[0].decision_id == decision.decision_id
    assert bot.calls[0].edge_id == decision.edge_id
    assert bot.calls[0].evidence_ids == decision.evidence
    assert (bot.calls[0].entry, bot.calls[0].stop, bot.calls[0].target) == (
        decision.entry,
        decision.stop,
        decision.target,
    )


def test_instruction_is_frozen_and_has_a_stable_content_hash():
    first = instruction_from_decision(approved())
    second = instruction_from_decision(approved())
    assert first == second
    assert len(first.request_hash) == 64
    with pytest.raises(FrozenInstanceError):
        first.symbol = "US30"


def test_same_decision_is_submitted_exactly_once():
    bot = RecordingBot()
    handoff = BotExecutionHandoff(bot, InMemoryExecutionJournal())
    first = handoff.submit(approved())
    second = handoff.submit(approved())
    assert first == second
    assert len(bot.calls) == 1


def test_reused_decision_id_with_different_content_is_rejected():
    bot = RecordingBot()
    handoff = BotExecutionHandoff(bot, InMemoryExecutionJournal())
    assert handoff.submit(approved()).status == "FILLED"
    conflict = handoff.submit(approved(target=13.0))
    assert conflict.status == "REJECTED"
    assert conflict.details["reason"] == "DECISION_ID_CONFLICT"
    assert len(bot.calls) == 1


def test_invalid_decision_never_reaches_bot():
    bot = RecordingBot()
    invalid = TradeDecision("", "XAUUSD", "BUY", datetime.now(timezone.utc))
    receipt = BotExecutionHandoff(bot, InMemoryExecutionJournal()).submit(invalid)
    assert receipt.status == "REJECTED"
    assert receipt.details["reason"] == "MISSING_ID_OR_SYMBOL"
    assert bot.calls == []


def test_bot_failure_is_recorded_and_not_resubmitted():
    class FailingBot:
        def __init__(self):
            self.calls = 0

        def submit(self, instruction):
            self.calls += 1
            raise TimeoutError("test failure")

    bot = FailingBot()
    handoff = BotExecutionHandoff(bot, InMemoryExecutionJournal())
    first = handoff.submit(approved())
    second = handoff.submit(approved())
    assert first.status == "ERROR"
    assert first.details["reason"] == "BOT_SUBMISSION_ERROR"
    assert first.details["error_type"] == "TimeoutError"
    assert second == first
    assert bot.calls == 1


def test_mismatched_bot_response_is_not_accepted():
    class WrongBot:
        def submit(self, instruction):
            return BotExecutionResponse("another-decision", "FILLED")

    receipt = BotExecutionHandoff(WrongBot(), InMemoryExecutionJournal()).submit(approved())
    assert receipt.status == "ERROR"
    assert receipt.details["reason"] == "BOT_RESPONSE_ID_MISMATCH"


def test_durable_journal_prevents_resubmission_after_restart(tmp_path):
    path = tmp_path / "execution.db"
    bot = RecordingBot()
    first_store = EvidenceStore(path)
    first = BotExecutionHandoff(bot, first_store).submit(approved())
    first_store.close()
    second_store = EvidenceStore(path)
    second = BotExecutionHandoff(bot, second_store).submit(approved())
    assert second == first
    assert len(bot.calls) == 1
    assert second_store._conn.execute("SELECT state FROM execution_handoffs").fetchone()[0] == "COMPLETED"
    second_store.close()


def test_clean_execution_path_has_no_direct_broker_or_legacy_import():
    root = Path(__file__).parents[1] / "cipherfx_clean"
    execution_path = ("execution.py", "runtime.py", "mt5_boundary.py")
    forbidden = (
        "import MetaTrader5",
        "from MetaTrader5",
        "mt5.order_send",
        "cipherfx_platform",
        "mt5_xm_gateway",
        "CipherFxBridge",
        "BrokerExecution",
    )
    for name in execution_path:
        text = (root / name).read_text()
        assert not any(token in text for token in forbidden), name
