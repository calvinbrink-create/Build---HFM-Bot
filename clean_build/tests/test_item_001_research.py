from datetime import datetime, timezone

from cipherfx_clean.intelligence.decision import create_decision
from cipherfx_clean.intelligence.knowledge import KnowledgeEntry, KnowledgeLibrary
from cipherfx_clean.intelligence.research import (
    EdgeCandidate,
    EdgeRegistry,
    ForwardOutcome,
    calculate_statistics,
)


def test_research_statistics_are_cost_adjusted_and_versioned():
    outcomes = [
        ForwardOutcome("s1", "edge", "BUY", 1.0, 0.1, 300, "TREND_UP", "LONDON"),
        ForwardOutcome("s2", "edge", "BUY", -1.0, 0.1, 300, "RANGE", "LONDON"),
        ForwardOutcome("s3", "edge", "BUY", 0.5, 0.1, 300, "TREND_UP", "NY"),
    ]
    stats = calculate_statistics("edge", outcomes)
    assert stats.sample_size == 3
    assert stats.expectancy_r < 1.0
    assert stats.total_cost == 0.30000000000000004

    registry = EdgeRegistry()
    candidate = EdgeCandidate("edge", 1, "CANDIDATE", evidence=("historical",))
    registry.register(candidate)
    historical = registry.transition("edge", 1, "HISTORICAL_VALIDATED", ("oos-report",))
    assert historical.status == "HISTORICAL_VALIDATED"
    shadow = registry.transition("edge", 1, "SHADOW", ("shadow-report",))
    assert shadow.status == "SHADOW"
    approved = registry.promote("edge", 1, stats)
    decision = create_decision(
        decision_id="d-1",
        symbol="XAUUSD",
        action="BUY",
        created_at=datetime.now(timezone.utc),
        edge=approved,
        evidence=("sample=3",),
    )
    assert decision.decision.edge_id == "edge"


def test_knowledge_entries_are_testable_vocabulary_not_active_rules():
    library = KnowledgeLibrary().add(KnowledgeEntry("hammer", "candlestick", "lower wick rejection"))
    assert library.by_family("candlestick")[0].testable is True
