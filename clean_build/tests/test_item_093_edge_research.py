from datetime import datetime, timedelta, timezone

from cipherfx_clean.intelligence.edge_miner import EdgeMiner, HypothesisProposal, run_hypothesis
from cipherfx_clean.intelligence.hypothesis import Hypothesis, evaluate_hypothesis
from cipherfx_clean.intelligence.research import ForwardOutcome, calculate_statistics
from cipherfx_clean.intelligence.validation_full import TimedObservation, validate_candidate


def test_edge_miner_and_hypothesis_keep_observed_evidence():
    proposal = HypothesisProposal("body", "positive body", ("body_return",), "test", 1)
    mined = EdgeMiner(lambda context: (proposal,)).propose({"sample_size": 2})
    states = (("s1", {"body_return": 0.1}, 1.0), ("s2", {"body_return": -0.1}, -1.0))
    result = run_hypothesis(mined[0], states, lambda features: features["body_return"] > 0)
    assert result.sample_size == 1
    assert result.evidence_ids == ("s1",)
    outcomes = tuple(
        ForwardOutcome(state_id, "body", "BUY", value, 0.01, 60, "RANGE", "LONDON")
        for state_id, _, value in states
    )
    evaluated = evaluate_hypothesis(Hypothesis("body", "positive body", ("body_return",)), outcomes, lambda row: row.r_multiple > 0)
    assert evaluated.statistics is not None
    assert evaluated.statistics.sample_size == 1


def test_validation_exposes_confidence_and_rejects_tiny_samples():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = tuple(
        TimedObservation(
            start + timedelta(minutes=index),
            0.1 if index % 2 == 0 else -0.05,
            "XAUUSD",
            "LONDON",
            "TREND_UP",
            start + timedelta(minutes=index, seconds=30),
            "M1",
            "BUY",
            source_id=f"s{index}",
        )
        for index in range(30)
    )
    report = validate_candidate(
        "body",
        rows,
        train_size=10,
        test_size=5,
        embargo=timedelta(seconds=60),
        multiple_test_count=3,
        holdout_size=5,
        minimum_oos_samples=5,
        minimum_holdout_samples=5,
    )
    assert report.multiple_test_count == 3
    assert 0.0 <= report.out_of_sample.lower_bound <= report.out_of_sample.upper_bound <= 1.0
    tiny = validate_candidate(
        "tiny",
        rows[:3],
        train_size=2,
        test_size=1,
        embargo=timedelta(seconds=60),
        multiple_test_count=1,
        holdout_size=0,
        minimum_oos_samples=5,
        minimum_holdout_samples=5,
    )
    assert tiny.status != "PASS"
    assert any("INSUFFICIENT" in reason for reason in tiny.reasons)
    assert calculate_statistics("body", tuple(
        ForwardOutcome(f"s{index}", "body", "BUY", row.value, 0.0, 60, "RANGE", "LONDON")
        for index, row in enumerate(rows)
    )).lower_win_rate >= 0.0
