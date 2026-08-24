from datetime import datetime, timedelta, timezone

from cipherfx_clean.contracts import RawTick, TradeDecision
from cipherfx_clean.intelligence.news import NewsEvent, news_regime
from cipherfx_clean.intelligence.planning import ConditionalPlan, PlanReality, PlanningLedger
from cipherfx_clean.shadow import advance_shadow, create_shadow_position


def test_news_tags_plans_and_shadow_have_separate_contracts():
    at = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    event = NewsEvent("n1", "USD", at, "high")
    assert news_regime(at, (event,), timedelta(minutes=15)) == "NEWS_WINDOW"

    plan = ConditionalPlan("plan-1", "USA100", "NEW_YORK", at, ("CONTINUE", "REVERSE"), "STALE", ("tick-1",))
    ledger = PlanningLedger()
    ledger.add(plan)
    ledger.reconcile(PlanReality("plan-1", "PARTIAL", "state-1", "OBSERVED"))
    assert ledger.plan("plan-1") == plan

    decision = TradeDecision(
        "decision-1", "USA100", "BUY", at, evidence=("tick-1",), edge_id="shadow", entry=100.0,
        stop=99.0, target=101.0, edge_version=1, confidence=.5, probability=.5,
        expected_value=0.0, setup_type="SHADOW", entry_area=(99.9, 100.1),
        stop_concept="shadow", target_concept="shadow", reason_codes=("SHADOW_ONLY",),
    )
    position = create_shadow_position(decision, state_id="state-1", expires_at=at + timedelta(minutes=5))
    tick = RawTick("USA100", at + timedelta(seconds=1), 99.9, 100.0)
    updated, outcome = advance_shadow(position, tick)
    assert updated.status == "OPEN"
    assert outcome is None
