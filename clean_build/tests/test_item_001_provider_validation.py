from datetime import datetime, timedelta, timezone

from cipherfx_clean.contracts import MarketState
from cipherfx_clean.intelligence.provider import ResearchRequest, StructuredResearchProvider
from cipherfx_clean.intelligence.renderer import render_svg
from cipherfx_clean.intelligence.validation import Observation, purged_walk_forward
from cipherfx_clean.market import Candle


def test_structured_provider_and_renderer_are_real_boundaries():
    now = datetime.now(timezone.utc)
    state = MarketState("XAUUSD", now, {})
    provider = StructuredResearchProvider(
        lambda request: {
            "decision_id": "d-1", "action": "BUY", "edge_id": "e-1", "edge_version": 2,
            "evidence": ["sample=100"], "entry": 10, "stop": 9, "target": 12,
            "confidence": .7, "probability": .6, "setup_type": "TEST_SETUP",
            "reason_codes": ["APPROVED_EDGE"],
        },
        {"e-1": 2},
    )
    decision = provider.decide(state, ResearchRequest("XAUUSD", now, {}, (), {}))
    assert decision.action == "BUY"
    candle = Candle("XAUUSD", "M5", now, now + timedelta(minutes=5), 10, 12, 9, 11, 2)
    assert render_svg((candle,)).startswith("<svg")


def test_renderer_embeds_zone_liquidity_and_structure_annotations():
    now = datetime.now(timezone.utc)
    candle = Candle("XAUUSD", "M5", now, now + timedelta(minutes=5), 100, 105, 95, 103)
    svg = render_svg(
        (candle,),
        annotations=(
            {"type": "DEMAND", "low": 96.0, "high": 98.0},
            {"type": "SWING_HIGH", "price": 104.0},
            {"type": "STRUCTURE", "direction": "UP"},
        ),
    )

    assert 'data-kind="DEMAND"' in svg
    assert 'data-kind="SWING_HIGH"' in svg
    assert "STRUCTURE UP" in svg


def test_purged_walk_forward_removes_embargo_overlap():
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    observations = tuple(Observation(base + timedelta(minutes=i), float(i)) for i in range(8))
    folds = purged_walk_forward(observations, train_size=4, test_size=2, embargo=timedelta(minutes=1))
    assert len(folds) == 2
    assert max(item.timestamp for item in folds[0].train) < min(item.timestamp for item in folds[0].test) - timedelta(minutes=1)
