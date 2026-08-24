from dataclasses import FrozenInstanceError, asdict
from datetime import datetime, timedelta, timezone

import pytest

from cipherfx_clean.contracts import Candle, MarketSnapshot, RawTick
from cipherfx_clean.intelligence.engine import IntelligenceEngine
from cipherfx_clean.intelligence.regime import classify_regime
from cipherfx_clean.intelligence.session import session_label
from cipherfx_clean.intelligence.spread_intelligence import (
    HistoricalSpreadSample,
    build_spread_profile_book,
    historical_spread_samples,
)


BASE = datetime(2026, 8, 24, 8, tzinfo=timezone.utc)


def _sample(index, *, session="LONDON", regime="TREND_UP", spread=None):
    return HistoricalSpreadSample(
        symbol="UK100",
        observed_at=BASE + timedelta(minutes=index),
        session=session,
        regime=regime,
        spread_price=spread if spread is not None else 0.1 + index * 0.01,
        source_id=f"source:{session}:{regime}:{index}",
    )


def test_spread_profile_matches_exact_symbol_session_and_regime_without_a_gate():
    matching = tuple(_sample(index) for index in range(40))
    other_session = tuple(
        _sample(index, session="ASIA", regime="RANGE", spread=5.0)
        for index in range(40)
    )
    book = build_spread_profile_book((*matching, *other_session), minimum_samples=30)
    result = book.observe(
        symbol="UK100",
        observed_at=BASE + timedelta(hours=1),
        session="LONDON",
        regime="TREND_UP",
        current_spread=0.25,
    )

    assert result.status == "MATCHED"
    assert result.sample_size == 40
    assert result.historical_mean < 1.0
    assert result.historical_p95 < 1.0
    assert 0.0 <= result.percentile <= 1.0
    assert result.ratio_to_median > 0
    assert result.role == "DESCRIPTIVE_RESEARCH_EVIDENCE_NOT_EXECUTION_GATE"
    assert not set(asdict(result)).intersection(
        {"approved", "allowed", "blocked", "qualified", "trade_action"}
    )
    with pytest.raises(FrozenInstanceError):
        result.status = "APPROVED"

    absent = book.observe(
        symbol="USA500",
        observed_at=BASE,
        session="LONDON",
        regime="TREND_UP",
        current_spread=0.5,
    )
    assert absent.status == "NO_MATCHING_PROFILE"
    assert absent.sample_size == 0


def test_broker_m1_spread_points_are_converted_with_contract_point_and_sourced():
    candles = tuple(
        Candle(
            "XAUUSD",
            "M1",
            BASE + timedelta(minutes=index),
            BASE + timedelta(minutes=index + 1),
            100 + index,
            101 + index,
            99 + index,
            100.5 + index,
            spread_points=34 + index,
            source="HFM_MT5_CSV_UTC_NORMALIZED",
            source_id=f"m1:{index}",
        )
        for index in range(5)
    )
    samples = tuple(historical_spread_samples("XAUUSD", candles, point_size=0.01))
    assert samples[0].spread_price == pytest.approx(0.34)
    assert samples[-1].spread_price == pytest.approx(0.38)
    assert samples[-1].source_id == "m1:4"
    assert all(item.session == "LONDON" for item in samples)

    wide = Candle(
        "USA30",
        "M1",
        BASE,
        BASE + timedelta(minutes=1),
        50_000,
        50_010,
        49_990,
        50_005,
        spread_points=380.0,
        source="HFM_MT5_CSV_UTC_NORMALIZED",
        source_id="usa30:exact-spread",
    )
    converted = tuple(
        historical_spread_samples("USA30", (wide,), point_size=0.01)
    )
    assert converted[0].spread_price == 3.8


def test_intelligence_report_uses_injected_historical_spread_profile():
    candles = tuple(
        Candle(
            "UK100",
            "M1",
            BASE + timedelta(minutes=index),
            BASE + timedelta(minutes=index + 1),
            100 + index * 0.2,
            101 + index * 0.2,
            99.5 + index * 0.2,
            100.5 + index * 0.2,
            tick_volume=100 + index,
            source="HFM_MT5_CSV_UTC_NORMALIZED",
            source_id=f"bar:{index}",
        )
        for index in range(30)
    )
    ticks = tuple(
        RawTick(
            "UK100",
            BASE + timedelta(minutes=29, seconds=index),
            106 + index * 0.01,
            106.2 + index * 0.01,
        )
        for index in range(10)
    )
    session = session_label(ticks[-1].timestamp)
    regime = classify_regime(candles).label
    samples = tuple(
        HistoricalSpreadSample(
            "UK100",
            BASE - timedelta(days=index + 1),
            session,
            regime,
            0.15 + index * 0.002,
            f"historical:{index}",
        )
        for index in range(40)
    )
    book = build_spread_profile_book(samples)
    snapshot = MarketSnapshot(
        "UK100",
        BASE + timedelta(minutes=31),
        ticks,
        {"M1": candles},
        {"M1": "COMPLETED"},
        ("dataset:spread",),
    )

    report = IntelligenceEngine(spread_profiles=book).analyse(snapshot)
    assert report.spread_context.status == "MATCHED"
    assert report.spread_context.sample_size == 40
    assert report.spread_context.current_spread == pytest.approx(0.2)
    assert "SPREAD_CONTEXT_MATCHED" in report.evidence
