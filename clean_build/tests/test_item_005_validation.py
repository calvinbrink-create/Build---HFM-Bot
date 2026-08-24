from datetime import datetime, timezone

from cipherfx_clean.contracts import TradeDecision
from cipherfx_clean.validation import validate_decision


def test_validator_checks_structure_without_rescoring():
    decision = TradeDecision(
        "d-1", "XAUUSD", "BUY", datetime.now(timezone.utc), evidence=("sample",), edge_id="e-1",
        entry=10, stop=9, target=12, edge_version=1, confidence=.7, probability=.6,
        setup_type="TEST_SETUP", reason_codes=("APPROVED_EDGE",),
    )
    result = validate_decision(decision)
    assert result.valid is True
    assert result.reason == "SCHEMA_VALID"
    assert decision.action == "BUY"


def test_validator_rejects_malformed_structure_only():
    decision = TradeDecision("", "", "BUY", datetime.now(timezone.utc))
    assert validate_decision(decision).reason == "MISSING_ID_OR_SYMBOL"
