from datetime import datetime, timedelta, timezone

from cipherfx_clean.contracts import Candle, MarketSnapshot, RawTick
from cipherfx_clean.dashboard import intelligence_view
from cipherfx_clean.intelligence.engine import IntelligenceEngine


def _snapshot():
    start = datetime(2026, 8, 21, 8, tzinfo=timezone.utc)
    candles = tuple(
        Candle(
            "UK100", "M1", start + timedelta(minutes=index), start + timedelta(minutes=index + 1),
            100 + index * .2, 101 + index * .2, 99.5 + index * .2, 100.5 + index * .2,
            tick_count=100 + index, tick_volume=100 + index, source="HFM_MT5_CSV", source_id=f"m1:{index}",
        )
        for index in range(30)
    )
    ticks = tuple(
        RawTick("UK100", start + timedelta(minutes=29, seconds=index), 106 + index * .01, 106.2 + index * .01)
        for index in range(10)
    )
    return MarketSnapshot(
        "UK100", start + timedelta(minutes=31), ticks, {"M1": candles, "M5": candles[::5]},
        {"M1": "COMPLETED", "M5": "COMPLETED"}, ("dataset:1",),
    )


def test_complete_intelligence_report_wires_every_observation_family():
    snapshot = _snapshot()
    report = IntelligenceEngine().analyse(snapshot)

    assert report.state_id
    assert report.source_ids == ("dataset:1",)
    assert report.microstructure.tick_count == 10
    assert report.spread_context.sample_size == 0
    assert report.spread_context.status == "NO_MATCHING_PROFILE"
    assert report.vwap.sample_size == 30
    assert report.vwap.source == "HFM_TICK_VOLUME_WEIGHTED_TYPICAL_PRICE"
    assert set(report.session_ranges) == {"ASIA", "LONDON", "LONDON_NY_OVERLAP", "NEW_YORK"}
    assert report.geometric_patterns.keys() == report.structure.keys()
    assert report.zone_quality.keys() == report.zones.keys()
    assert report.liquidity_sweeps.keys() == report.liquidity.keys()
    assert report.momentum.keys() == report.structure.keys()
    assert report.relative_activity.keys() == report.structure.keys()
    assert report.trend.keys() == report.structure.keys()
    assert report.breakouts.keys() == report.structure.keys()
    assert report.mean_reversion.keys() == report.structure.keys()
    assert report.reversals.keys() == report.structure.keys()
    assert report.continuations.keys() == report.structure.keys()
    assert report.regime_probability.sample_size == 2
    assert set(report.framework_hypotheses) >= {
        "price_action_rejection", "smc_sweep_reclaim", "wyckoff_spring",
        "session_breakout", "value_reversion", "impulse_continuation",
    }
    assert {item.name for item in report.knowledge_records} >= {
        "price_discovery", "spread", "participant_intent", "broker_activity_proxy",
    }
    assert next(item for item in report.knowledge_records if item.name == "participant_intent").epistemic_type == "INFERENCE"
    assert report.chart_hashes["M1"]
    assert report.metadata["fingerprint"]["symbol"] == "UK100"
    assert report.session_context is not None
    assert report.session_context.canonical_utc == snapshot.ticks[-1].timestamp.isoformat()
    assert report.session_context.primary_session == "LONDON"
    dashboard = intelligence_view(report)
    assert dashboard["session_context"]["canonical_utc"] == snapshot.ticks[-1].timestamp.isoformat()
    assert dashboard["session_context"]["source"] == "IANA_EXCHANGE_CLOCKS_FROM_CANONICAL_UTC"
    assert "SOURCE_PROVENANCE" in report.evidence


def test_state_id_is_deterministic_for_the_same_frozen_snapshot():
    engine = IntelligenceEngine()
    snapshot = _snapshot()
    assert engine.analyse(snapshot).state_id == engine.analyse(snapshot).state_id
