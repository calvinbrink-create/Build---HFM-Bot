from cipherfx_clean.intelligence.edge_validation import (
    GraveyardEntry,
    ValidationSlice,
    monte_carlo_expectancy,
    validate_by_slice,
)
from cipherfx_clean.intelligence.research import ForwardOutcome


def test_edge_validation_keeps_instrument_session_regime_and_oos_data():
    rows = tuple(ForwardOutcome(f"s{i}", "e", "BUY", 1 if i % 2 else -0.5, 0.1, 300, "TREND_UP", "LONDON") for i in range(6))
    report = validate_by_slice("e", (ValidationSlice("XAUUSD", "LONDON", "TREND_UP", rows),))
    assert report.statistics.sample_size == 6
    assert report.out_of_sample.sample_size == 3
    assert len(monte_carlo_expectancy(rows, 20)) == 20
    assert GraveyardEntry("e", 1, "negative_oos", report.statistics).reason == "negative_oos"
