from datetime import datetime, timedelta, timezone

from cipherfx_clean.contracts import Candle, MarketSnapshot, RawTick
from cipherfx_clean.historical_memory import feature_names
from cipherfx_clean.intelligence.fingerprint_engine import SetupFingerprintEngine


def _snapshot() -> MarketSnapshot:
    start = datetime(2026, 8, 24, tzinfo=timezone.utc)
    rows = tuple(
        Candle(
            "USA100",
            "M1",
            start + timedelta(minutes=index),
            start + timedelta(minutes=index + 1),
            100.0 + index * 0.1,
            100.5 + index * 0.1,
            99.8 + index * 0.1,
            100.3 + index * 0.1,
            source_id=f"c{index}",
        )
        for index in range(30)
    )
    observed_at = rows[-1].end
    return MarketSnapshot(
        "USA100",
        observed_at,
        (RawTick("USA100", observed_at, rows[-1].close, rows[-1].close + 0.1),),
        {"M1": rows},
        {"M1": "COMPLETED"},
        tuple(row.source_id for row in rows),
    )


def test_setup_fingerprint_has_numeric_and_semantic_views():
    fingerprint = SetupFingerprintEngine().build(_snapshot(), ("M1",))
    assert len(fingerprint.values) == len(feature_names(("M1",)))
    assert len(fingerprint.values) > 20
    assert fingerprint.as_mapping()["M1:available"] == 1.0
    assert fingerprint.semantic_labels
    assert len(fingerprint.source_digest) == 64
