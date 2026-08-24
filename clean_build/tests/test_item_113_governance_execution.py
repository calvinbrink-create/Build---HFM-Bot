from datetime import datetime, timedelta, timezone

from cipherfx_clean.cost_evidence import ExecutionCostSample, build_cost_profile
from cipherfx_clean.intelligence.edge_validation import monte_carlo_expectancy
from cipherfx_clean.intelligence.execution_model import Quote, replay_executable_prices
from cipherfx_clean.intelligence.research import ForwardOutcome, overfit_diagnostic


def _sample(index: int) -> ExecutionCostSample:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=index)
    return ExecutionCostSample(
        sample_id=f"sample-{index}",
        decision_id=f"decision-{index}",
        symbol="UK100",
        side="BUY",
        observed_at=start,
        reference_bid=100.0,
        reference_ask=100.2,
        requested_price=100.2,
        fill_price=100.21,
        volume=1.0,
        contract_size=1.0,
        commission_account=0.1,
        swap_account=0.0,
        submitted_at=start,
        filled_at=start + timedelta(milliseconds=100),
        source_ids=(f"source-{index}",),
    )


def test_cost_profile_requires_measured_samples_and_replay_uses_executable_side():
    profile = build_cost_profile("UK100", tuple(_sample(index) for index in range(10)), minimum_samples=10)
    assert profile.status == "COMPLETE"
    fills = replay_executable_prices(
        "BUY",
        (Quote(0, 100.0, 100.2), Quote(1, 100.1, 100.3)),
        100.2,
    )
    assert tuple(fill.requested for fill in fills) == (100.2, 100.2)
    assert tuple(fill.executed for fill in fills) == (100.2, 100.3)


def test_overfit_and_monte_carlo_are_deterministic_research_outputs():
    diagnostic = overfit_diagnostic("edge", (0.1, 0.11, 0.12, 0.13))
    assert diagnostic.tested_variants == 4
    outcomes = tuple(
        ForwardOutcome(f"state-{index}", "edge", "BUY", 0.2 if index % 2 else -0.1, 0.01, 60, "TREND", "LONDON")
        for index in range(20)
    )
    simulation = monte_carlo_expectancy(outcomes, iterations=100, seed=113)
    assert len(simulation) == 100
    assert simulation == monte_carlo_expectancy(outcomes, iterations=100, seed=113)
