from datetime import datetime, timedelta, timezone

from cipherfx_clean.contracts import Candle, MarketSnapshot, RawTick
from cipherfx_clean.dashboard import intelligence_view
from cipherfx_clean.intelligence.engine import IntelligenceEngine
from cipherfx_clean.intelligence.pattern_outcomes import (
    PatternOutcomeLibrary,
    PatternOutcomeRecord,
)


def _record(failed: bool) -> PatternOutcomeRecord:
    return PatternOutcomeRecord(
        "outcome-failed" if failed else "outcome-confirmed",
        "DOUBLE_TOP",
        "USA100",
        "M5",
        datetime(2026, 8, 24, tzinfo=timezone.utc),
        "BEARISH",
        100.0,
        -2.0 if failed else 2.0,
        0.0 if failed else 2.0,
        -2.0 if failed else 0.0,
        "FAILED_PATTERN" if failed else "CONFIRMED_PATTERN",
        not failed,
        failed,
        ("c1", "c2"),
    )


def test_pattern_outcome_library_separates_confirmed_and_failed_records():
    library = PatternOutcomeLibrary((_record(False), _record(True)))
    assert len(library.records_for_pattern("DOUBLE_TOP")) == 2
    assert len(library.failed_patterns()) == 1
    assert library.failed_patterns()[0].outcome_kind == "FAILED_PATTERN"


def test_engine_and_dashboard_expose_pattern_outcomes_without_deciding():
    observed_at = datetime(2026, 8, 24, tzinfo=timezone.utc)
    candles = tuple(
        Candle(
            "USA100",
            "M1",
            observed_at + timedelta(minutes=index),
            observed_at + timedelta(minutes=index + 1),
            100.0,
            101.0,
            99.0,
            100.5,
        )
        for index in range(30)
    )
    snapshot = MarketSnapshot(
        "USA100",
        observed_at,
        (RawTick("USA100", observed_at, 100.0, 100.1),),
        {"M1": candles},
        {"M1": "COMPLETED"},
        tuple(f"c{index}" for index in range(30)),
    )
    report = IntelligenceEngine(
        pattern_outcome_library=PatternOutcomeLibrary((_record(False), _record(True)))
    ).analyse(snapshot)
    view = intelligence_view(report)
    assert len(report.pattern_outcome_context) == 2
    assert len(report.failed_pattern_context) == 1
    assert len(view["failed_pattern_context"]) == 1
    assert "PATTERN_OUTCOMES_OBSERVED" in report.evidence
    assert "FAILED_PATTERN_LIBRARY_OBSERVED" in report.evidence
