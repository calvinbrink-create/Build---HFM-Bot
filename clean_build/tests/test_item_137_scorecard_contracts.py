from datetime import datetime, timezone

from cipherfx_clean.contracts import TradeDecision
from cipherfx_clean.intelligence.governance import (
    EdgeScorecard,
    GovernancePolicy,
    eligible_for_promotion,
)
from cipherfx_clean.intelligence.runtime_contracts import StructuredInput, StructuredOutput
from cipherfx_clean.validation import validate_against_active_edges


def test_scorecard_policy_blocks_small_samples():
    scorecard = EdgeScorecard(
        "edge", 1, 8, .88, .75, 4.0, .1, 2.0, .1, (.50, .99),
        oos_sample_size=8, holdout_sample_size=8, shadow_sample_size=8,
        holdout_expectancy=.7, shadow_expectancy=.65,
    )
    assert not eligible_for_promotion(scorecard, GovernancePolicy(30, 0.0, 15, 2.0, 10, 10))


def test_structured_contract_and_active_edge_validation():
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    structured_input = StructuredInput(
        "UK100", at, "state-1", {"ticks": {}, "features": {}, "state": "LIVE"},
        ("chart-1",), ("analogue-1",), {"edge": {"sample_size": 100}},
    )
    structured_output = StructuredOutput(
        "decision-1", "BUY", .7, "SETUP", (100.0, 101.0), "RANGE", "LIQUIDITY",
        ("chart-1", "analogue-1"), "edge", ("EVIDENCE",),
    )
    assert structured_input.chart_ids == structured_output.evidence_ids[:1]

    decision = TradeDecision(
        "decision-1", "UK100", "BUY", at, evidence=("chart-1",), edge_id="edge",
        entry=100.0, stop=99.0, target=102.0, edge_version=1, confidence=.7,
        probability=.6, expected_value=.1, setup_type="SETUP", entry_area=(100.0, 101.0),
        stop_concept="RANGE", target_concept="LIQUIDITY", reason_codes=("EVIDENCE",),
    )
    assert validate_against_active_edges(decision, {"edge": 1}).reason == "ACTIVE_EDGE_VALID"
    assert validate_against_active_edges(decision, {"other-edge": 1}).reason == "UNAPPROVED_EDGE"
