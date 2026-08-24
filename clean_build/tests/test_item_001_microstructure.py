from datetime import datetime, timedelta, timezone

from cipherfx_clean.intelligence.hypothesis import Hypothesis, compare_counterfactuals, evaluate_hypothesis
from cipherfx_clean.intelligence.microstructure import measure_microstructure
from cipherfx_clean.intelligence.session import session_label
from cipherfx_clean.intelligence.research import ForwardOutcome
from cipherfx_clean.market import RawTick


def test_microstructure_session_and_hypothesis_are_observable():
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ticks = tuple(RawTick("XAUUSD", base + timedelta(seconds=i), 10 + i, 10.1 + i) for i in range(3))
    micro = measure_microstructure(ticks)
    assert micro.directional_imbalance == 1.0
    assert session_label(base.replace(hour=8)) == "LONDON"
    rows = (ForwardOutcome("s", "e", "BUY", 1, 0.1, 300, "TREND_UP", "LONDON"),)
    result = evaluate_hypothesis(Hypothesis("h", "positive outcome", ("r",)), rows, lambda row: row.r_multiple > 0)
    assert result.statistics.sample_size == 1
    assert compare_counterfactuals(rows, ()).not_traded == ()
