from datetime import datetime, timedelta, timezone

from cipherfx_clean.contracts import TradeDecision
from cipherfx_clean.execution import (
    BotExecutionHandoff,
    BotExecutionResponse,
    InMemoryExecutionJournal,
)
from cipherfx_clean.slippage import BrokerFill


class ExistingBot:
    def __init__(self):
        self.instructions = []

    def submit(self, instruction):
        self.instructions.append(instruction)
        return BotExecutionResponse(
            decision_id=instruction.decision_id,
            status="FILLED",
            broker_reference="broker-1",
            fills=(
                BrokerFill(
                    "100",
                    "200",
                    10.0,
                    10.1,
                    1.0,
                    datetime.now(timezone.utc),
                    "hfm-deals:200",
                    requested_at=datetime.now(timezone.utc) - timedelta(seconds=1),
                ),
            ),
        )


def decision(action="BUY"):
    return TradeDecision(
        "d-1",
        "XAUUSD",
        action,
        datetime.now(timezone.utc),
        evidence=("chart-1",),
        edge_id="edge-1",
        entry=10.0,
        stop=9.0 if action == "BUY" else 11.0,
        target=12.0 if action == "BUY" else 8.0,
        edge_version=1,
        confidence=.7,
        probability=.6,
        setup_type="TEST_SETUP",
        reason_codes=("APPROVED_EDGE",),
    )


def test_execution_forwards_immutable_decision_to_existing_bot():
    bot = ExistingBot()
    receipt = BotExecutionHandoff(bot, InMemoryExecutionJournal()).submit(decision())
    assert receipt.status == "FILLED"
    assert bot.instructions[0].side == "BUY"
    assert bot.instructions[0].symbol == "XAUUSD"
    assert bot.instructions[0].entry == 10.0
    assert receipt.details["fills"][0]["order_ticket"] == "100"


def test_no_trade_never_reaches_existing_bot():
    bot = ExistingBot()
    no_trade = TradeDecision("d-1", "XAUUSD", "NO_TRADE", datetime.now(timezone.utc))
    receipt = BotExecutionHandoff(bot, InMemoryExecutionJournal()).submit(no_trade)
    assert receipt.status == "NO_TRADE"
    assert bot.instructions == []
