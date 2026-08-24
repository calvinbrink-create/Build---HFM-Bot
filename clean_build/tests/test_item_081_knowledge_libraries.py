from cipherfx_clean.intelligence.knowledge import KnowledgeLibrary
from cipherfx_clean.intelligence.strategies import (
    BREAKOUT,
    MEAN_REVERSION,
    MOMENTUM,
    PRICE_ACTION,
    SMC,
    WYCKOFF,
    chart_patterns,
    default_knowledge,
)


def test_methodology_knowledge_contains_distinct_structured_families():
    strategies = default_knowledge()
    assert len(strategies) >= 10
    assert len({item.strategy_id for item in strategies}) == len(strategies)
    assert all(item.conditions and item.required_observations for item in strategies)
    vocabulary = KnowledgeLibrary.complete_vocabulary().all()
    assert {item.strategy_id for item in strategies}.issubset({item.name for item in vocabulary})
    expected = {
        "PRICE_ACTION": PRICE_ACTION,
        "SMC": SMC,
        "WYCKOFF": WYCKOFF,
        "BREAKOUT": BREAKOUT,
        "MEAN_REVERSION": MEAN_REVERSION,
        "MOMENTUM": MOMENTUM,
    }
    for family, conditions in expected.items():
        entries = [item for item in vocabulary if item.family == family]
        assert len(entries) == 1
        assert set(conditions).issubset(set(entries[0].observation.split(",")))


def test_chart_pattern_library_is_machine_readable_and_has_failure_conditions():
    patterns = chart_patterns()
    vocabulary = KnowledgeLibrary.complete_vocabulary().all()
    pattern_names = {item.name for item in vocabulary if item.family == "PATTERN"}
    assert len(patterns) >= 10
    assert {item.pattern_id for item in patterns}.issubset(pattern_names)
    assert all(item.failure_condition for item in patterns)
