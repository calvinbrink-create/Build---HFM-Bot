from pathlib import Path

from cipherfx_clean.friday_runtime import ACTIVE_SYMBOLS, F_IDS, TIMEFRAMES


def test_friday_scope_is_complete_and_bounded_to_hfm_universe():
    assert F_IDS == tuple(f"F{number:02d}" for number in range(65))
    assert ACTIVE_SYMBOLS == ("XAUUSD", "UK100", "USA100", "USA500", "USA30")
    assert TIMEFRAMES == ("M1", "M5", "M15", "H1", "H4", "D1")


def test_friday_verifier_has_no_broker_order_api():
    source = Path(__file__).parents[1].joinpath("cipherfx_clean", "friday_runtime.py").read_text()
    assert "NO_LIVE_MT5_ORDER_CALL" in source
    imports = tuple(
        line.strip()
        for line in source.splitlines()
        if line.strip().startswith(("import ", "from "))
    )
    assert not any("MetaTrader5" in line or "order_send" in line for line in imports)


def test_friday_release_verifier_cannot_manufacture_certification_evidence():
    source = Path(__file__).parents[1].joinpath("cipherfx_clean", "friday_runtime.py").read_text()

    assert "HFM_EXECUTABLE_TICKS" not in source
    assert "P-VALIDATION-CONTRACT" not in source
    assert "P-SHADOW-CONTRACT" not in source
    assert "P-COST-CONTRACT" not in source
    assert 'path_fidelity="BAR_OHLC_NO_EXECUTABLE_TICKS"' in source
    assert '"release_readiness_status": bundle.status' in source
    assert '"blocked_release_detected": blocked_release_detected' in source


def test_principles_verifier_cannot_manufacture_broker_or_forward_evidence():
    source = Path(__file__).parents[1].joinpath("cipherfx_clean", "principles_runtime.py").read_text()

    assert "class TestBot" not in source
    assert "P-TEST-BOT-FILL" not in source
    assert "P-TEST-BROKER-EVIDENCE" not in source
    assert "P_EXECUTION_TEST_DOUBLE" not in source
    assert "all_principles_verified" not in source
    assert "p001_p042_shadow_" not in source
    assert '"forward_shadow_status": forward_shadow_status' in source
    assert '"bot_handoff_status": broker_handoff_status' in source
    assert '"research_readiness_status": "BLOCKED"' in source
