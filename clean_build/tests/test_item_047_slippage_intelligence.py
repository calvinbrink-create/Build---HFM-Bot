from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from cipherfx_clean.contracts import TradeDecision
from cipherfx_clean.execution import (
    BotExecutionHandoff,
    BotExecutionResponse,
    InMemoryExecutionJournal,
)
from cipherfx_clean.slippage import BrokerFill, build_slippage_observation
from cipherfx_clean.store import EvidenceStore


NOW = datetime(2026, 8, 24, 8, 30, tzinfo=timezone.utc)


def decision(decision_id: str = "decision-c032", side: str = "BUY") -> TradeDecision:
    return TradeDecision(
        decision_id=decision_id,
        symbol="XAUUSD",
        action=side,
        created_at=NOW,
        evidence=("chart:exact", "edge:validated"),
        edge_id="edge-c032-v1",
        entry=5_000.0,
        stop=4_990.0 if side == "BUY" else 5_010.0,
        target=5_020.0 if side == "BUY" else 4_980.0,
        edge_version=1,
        confidence=0.7,
        probability=0.6,
        expected_value=0.1,
        setup_type="BROKER_EVIDENCE_TEST",
        entry_area=(4_999.0, 5_001.0),
        stop_concept="STRUCTURE_INVALIDATION",
        target_concept="OPPOSING_LIQUIDITY",
        reason_codes=("EDGE_APPROVED",),
    )


def fill(
    order: str,
    deal: str,
    requested: float,
    actual: float,
    *,
    volume: float = 1.0,
    seconds: int = 1,
) -> BrokerFill:
    return BrokerFill(
        order_ticket=order,
        deal_ticket=deal,
        requested_price=requested,
        fill_price=actual,
        volume=volume,
        filled_at=NOW + timedelta(seconds=seconds),
        source_id=f"hfm-mt5-deals:{deal}",
        requested_at=NOW,
    )


@pytest.mark.parametrize(
    ("side", "requested", "actual", "adverse", "favorable"),
    (
        ("BUY", 100.0, 100.5, 0.5, 0.0),
        ("BUY", 100.0, 99.5, 0.0, 0.5),
        ("SELL", 100.0, 99.5, 0.5, 0.0),
        ("SELL", 100.0, 100.5, 0.0, 0.5),
    ),
)
def test_slippage_is_side_aware(side, requested, actual, adverse, favorable):
    observation = build_slippage_observation(
        decision_id="decision",
        request_hash="request-hash",
        edge_id="edge-v1",
        symbol="USA100",
        side=side,
        fill=fill("100", "200", requested, actual),
    )
    assert observation.adverse_slippage == adverse
    assert observation.favorable_slippage == favorable
    assert observation.side_adjusted_price_difference == adverse - favorable
    with pytest.raises(FrozenInstanceError):
        observation.fill_price = 0.0


def test_every_broker_deal_is_persisted_atomically_and_survives_restart(tmp_path):
    path = tmp_path / "slippage.sqlite3"

    class TwoFillBot:
        def __init__(self):
            self.calls = 0

        def submit(self, instruction):
            self.calls += 1
            return BotExecutionResponse(
                instruction.decision_id,
                "FILLED",
                "hfm:batch:1",
                fills=(
                    fill("101", "201", 5_001.25, 5_001.50, volume=0.6),
                    fill("102", "202", 5_001.00, 5_000.75, volume=0.4, seconds=2),
                ),
                reason="MT5_RETCODE_10009",
            )

    bot = TwoFillBot()
    first_store = EvidenceStore(path)
    receipt = BotExecutionHandoff(bot, first_store).submit(decision())
    observations = first_store.load_slippage_observations("decision-c032")
    assert receipt.status == "FILLED"
    assert len(observations) == 2
    assert {row.deal_ticket for row in observations} == {"201", "202"}
    assert {row.requested_price for row in observations} == {5_001.25, 5_001.00}
    assert all(row.requested_price != decision().entry for row in observations)
    assert tuple(receipt.details["slippage_observation_ids"]) == tuple(
        row.observation_id for row in observations
    )
    assert first_store.integrity()
    first_store.close()

    second_store = EvidenceStore(path)
    repeated = BotExecutionHandoff(bot, second_store).submit(decision())
    assert repeated == receipt
    assert bot.calls == 1
    assert len(second_store.load_slippage_observations("decision-c032")) == 2
    assert second_store.integrity()
    second_store.close()


def test_filled_response_without_exact_request_and_fill_evidence_is_an_error():
    class IncompleteBot:
        def submit(self, instruction):
            return BotExecutionResponse(
                instruction.decision_id,
                "FILLED",
                "hfm:missing-evidence",
                reason="MT5_RETCODE_10009",
            )

    journal = InMemoryExecutionJournal()
    receipt = BotExecutionHandoff(IncompleteBot(), journal).submit(decision())
    assert receipt.status == "ERROR"
    assert receipt.details["reason"] == "BROKER_FILL_EVIDENCE_INCOMPLETE"
    assert journal.slippage_observations() == ()


def test_duplicate_broker_deal_evidence_cannot_be_counted_twice():
    duplicate = fill("101", "201", 5_001.25, 5_001.50)

    class DuplicateFillBot:
        def submit(self, instruction):
            return BotExecutionResponse(
                instruction.decision_id,
                "FILLED",
                "hfm:duplicate",
                fills=(duplicate, duplicate),
            )

    journal = InMemoryExecutionJournal()
    receipt = BotExecutionHandoff(DuplicateFillBot(), journal).submit(decision())
    assert receipt.status == "ERROR"
    assert receipt.details["reason"] == "BROKER_FILL_EVIDENCE_DUPLICATE"
    assert journal.slippage_observations() == ()


def test_fill_requires_timezone_aware_broker_time():
    with pytest.raises(ValueError, match="timezone-aware"):
        BrokerFill("1", "2", 100.0, 100.1, 1.0, datetime(2026, 8, 24), "broker:2")
