from pathlib import Path

from cipherfx_clean.core_runtime import ACTIVE_SYMBOLS, CORE_IDS


def test_core_authority_scope_is_complete():
    assert CORE_IDS == ("C001", "C002", "C003", "C004", "C005")
    assert ACTIVE_SYMBOLS == ("XAUUSD", "UK100", "USA100", "USA500", "USA30")


def test_core_verifier_does_not_replace_the_existing_bot():
    source = Path(__file__).parents[1].joinpath("cipherfx_clean", "core_runtime.py").read_text()
    assert "cannot certify live broker activity" in source
    assert "no_strategy_replacement" in source
    assert '"status": "BLOCKED"' in source
    assert "STATIC_SOURCE_TRACE_IS_NOT_RUNTIME_EVIDENCE" in source
