from datetime import datetime, timedelta, timezone

from cipherfx_clean.contracts import MarketSnapshot, RawTick
from cipherfx_clean.dashboard import intelligence_view
from cipherfx_clean.intelligence.engine import IntelligenceEngine
from cipherfx_clean.intelligence.market_regime_engine import build_market_regime_library
from cipherfx_clean.intelligence.router import build_strategy_router
from test_item_079_regime_probability import _days


UTC = timezone.utc


def test_default_router_maps_regimes_without_approval_authority():
    router = build_strategy_router()
    for regime in ("TREND_UP", "TREND_DOWN", "RANGE", "BREAKOUT", "UNKNOWN"):
        route = router.candidates(regime, "LONDON")
        assert route.candidate_ids
        assert route.informational_only is True
        assert route.reason in {"REGIME_ROUTE", "DEFAULT_ROUTE"}


def test_engine_and_dashboard_expose_strategy_routing_context():
    start = datetime(2026, 6, 1, tzinfo=UTC)
    observed_at = start + timedelta(days=49, hours=8)
    rows = _days(start, 50)
    regime_library = build_market_regime_library(
        {"USA100": {"M1": rows}}, observed_at=observed_at
    )
    snapshot = MarketSnapshot(
        "USA100",
        observed_at,
        (RawTick("USA100", observed_at, rows[-1].close, rows[-1].close + 0.1),),
        {"M1": rows[-30:]},
        {"M1": "COMPLETED"},
        tuple(item.source_id for item in rows[-30:]),
    )
    report = IntelligenceEngine(
        market_regime_library=regime_library,
        strategy_router=build_strategy_router(),
    ).analyse(snapshot)
    view = intelligence_view(report)

    assert report.strategy_routing_context is not None
    assert report.strategy_routing_context.candidate_ids
    assert report.strategy_routing_context.informational_only is True
    assert view["strategy_routing_context"]["candidate_ids"]
    assert "STRATEGY_ROUTER_OBSERVED" in report.evidence
