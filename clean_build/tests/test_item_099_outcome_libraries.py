from datetime import datetime, timedelta, timezone

from cipherfx_clean.contracts import Candle, RawTick
from cipherfx_clean.features import build_fingerprint
from cipherfx_clean.feedback import ClosedTrade, LearningFeedback
from cipherfx_clean.intelligence.data_library import MarketStateRecord, WholeMarketLibrary
from cipherfx_clean.intelligence.hypothesis import compare_counterfactuals
from cipherfx_clean.intelligence.outcomes import future_path_outcome


def test_whole_market_library_persists_traded_and_non_traded_outcomes():
    observed = datetime(2026, 1, 1, tzinfo=timezone.utc)
    candle = Candle("XAUUSD", "M1", observed, observed + timedelta(minutes=1), 100.0, 101.0, 99.5, 100.8)
    tick = RawTick("XAUUSD", candle.end, 100.8, 100.9)
    fingerprint = build_fingerprint((tick,), (candle,))
    library = WholeMarketLibrary()
    for state_id, kind, outcome_kind in (("win", "TRADED", "WINNER"), ("ordinary", "NO_TRADE", "NO_TRADE")):
        record = MarketStateRecord(state_id, "XAUUSD", candle.end, fingerprint, kind, (kind,), (candle.source_id,), {})
        library.add_state(record)
        outcome = future_path_outcome(
            state_id,
            "BUY",
            100.0,
            ((60, 101.0, 101.1), (120, 101.5, 101.6)),
            0.5,
            1.0,
            outcome_kind=outcome_kind,
        )
        library.add_outcome(outcome)
    assert len(library.states("TRADED")) == 1
    assert library.outcome("win").outcome_kind == "WINNER"
    assert library.outcome("ordinary").outcome_kind == "NO_TRADE"
    assert len(compare_counterfactuals((LearningFeedback().to_outcome(ClosedTrade("win", "e", "BUY", 1.0, 0.0, observed, "RANGE", "LONDON")),), ()).traded) == 1
