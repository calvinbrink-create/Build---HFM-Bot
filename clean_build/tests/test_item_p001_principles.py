from pathlib import Path

from cipherfx_clean.principles_runtime import ACTIVE_SYMBOLS, PRINCIPLE_IDS


def test_principle_scope_is_complete_and_uses_the_active_universe():
    assert PRINCIPLE_IDS == tuple(f"P{number:03d}" for number in range(1, 43))
    assert ACTIVE_SYMBOLS == ("XAUUSD", "UK100", "USA100", "USA500", "USA30")


def test_principles_verifier_is_research_only():
    source = Path(__file__).parents[1].joinpath("cipherfx_clean", "principles_runtime.py").read_text()
    assert "NO_LIVE_MT5_ORDER_CALL" in source
    assert "order_send" not in source
