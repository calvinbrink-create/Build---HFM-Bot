from datetime import datetime, timedelta, timezone

from cipherfx_clean.contracts import Candle
from cipherfx_clean.intelligence.cross_asset import aligned_closes, compare_assets, contexts_at
from cipherfx_clean.intelligence.knowledge import KnowledgeLibrary
from cipherfx_clean.intelligence.library import MarketStateLibrary, OutcomeRecord, StateRecord
from cipherfx_clean.intelligence.strategies import default_knowledge
from cipherfx_clean.features import MarketFingerprint


def test_knowledge_separates_observations_inferences_and_hypotheses():
    rows = KnowledgeLibrary.complete_vocabulary().all()
    kinds = {item.epistemic_type for item in rows}
    assert kinds == {"OBSERVABLE", "INFERENCE", "HYPOTHESIS"}
    participant = next(item for item in rows if item.name == "participant_intent")
    assert participant.testable is False
    assert "cannot be directly observed" in participant.observation
    proxy = next(item for item in rows if item.name == "broker_activity_proxy")
    assert "not centralized traded volume" in proxy.observation


def test_every_framework_is_versioned_bidirectional_and_contains_no_trade():
    rows = default_knowledge()
    assert {item.family for item in rows} >= {
        "PRICE_ACTION", "SMC", "WYCKOFF", "BREAKOUT", "MEAN_REVERSION",
        "MOMENTUM", "PATTERN", "LIQUIDITY", "SESSION", "VOLATILITY",
    }
    assert all(item.version == 1 and item.status == "CANDIDATE" for item in rows)
    assert all(item.directions == ("BUY", "SELL", "NO_TRADE") for item in rows)
    assert all(item.required_observations for item in rows)


def _frames():
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    result = {}
    for symbol, multiplier in (("USA100", 1.0), ("USA500", 0.8), ("XAUUSD", -0.3)):
        result[symbol] = tuple(
            Candle(symbol, "M5", base + timedelta(minutes=5 * i), base + timedelta(minutes=5 * (i + 1)), 100, 101, 99, 100 + i * multiplier)
            for i in range(200)
        )
    return result


def test_cross_asset_series_are_timestamp_aligned_and_use_returns_with_baseline():
    closes = aligned_closes(_frames())
    observations = compare_assets(closes, window=30, baseline_window=120)
    assert len(observations) == 3
    us = next(item for item in observations if {item.left, item.right} == {"USA100", "USA500"})
    assert us.sample_size == 30
    assert us.baseline_sample_size == 120
    assert us.source == "ALIGNED_CLOSE_RETURNS"
    assert us.coefficient > .99


def test_cross_asset_context_is_frozen_at_observation_and_explicitly_available():
    frames = _frames()
    observed_at = frames["USA100"][150].end
    context = contexts_at(frames, observed_at=observed_at)

    assert set(context) == set(frames)
    assert context["USA100"]["available"] == 1.0
    assert context["USA100"]["count"] == 2.0
    assert context["USA100"]["max_absolute_correlation"] > 0.9

    insufficient = contexts_at(
        {"USA100": frames["USA100"][:1], "USA500": frames["USA500"][:1]},
        observed_at=frames["USA100"][0].end,
    )
    assert insufficient["USA100"]["available"] == 0.0


def test_whole_market_library_accepts_negative_and_no_trade_examples_immutably():
    library = MarketStateLibrary()
    fingerprint = MarketFingerprint("XAUUSD", 10, .1, .2, 1, 2, .5, 3, 1, 4)
    observed_at = datetime.now(timezone.utc)
    kinds = ("WIN", "LOSS", "BREAKEVEN", "REJECTED", "EXPIRED", "MISSED", "FAILED_PATTERN", "NO_TRADE")
    for index, kind in enumerate(kinds):
        library.add_state(StateRecord(f"s{index}", "XAUUSD", observed_at, fingerprint, kind, kind, {}, "d1", ("source",)))
    assert {item.kind for item in library.states()} == set(kinds)
    outcome = OutcomeRecord("s0", "WIN", 1.2, 1.5, -.3, 60, 5, 120, .01)
    library.add_outcome(outcome)
    library.add_outcome(outcome)
    assert library.outcome("s0") == outcome
