from datetime import datetime, timedelta, timezone

from cipherfx_clean.contracts import RawTick, TradeDecision
from cipherfx_clean.shadow_runtime import PersistentShadowRunner, shadow_scorecard
from cipherfx_clean.store import EvidenceStore


def _decision(now):
    return TradeDecision(
        "d1", "XAUUSD", "BUY", now, evidence=("state-1",), edge_id="edge-1", entry=100, stop=99,
        target=102, edge_version=1, confidence=.7, probability=.6,
        setup_type="TEST", entry_area=(99.9, 100.1), stop_concept="INVALIDATION",
        target_concept="TARGET", reason_codes=("APPROVED_EDGE",),
    )


def test_shadow_runner_recovers_after_restart_and_processes_each_tick_once(tmp_path):
    now = datetime(2026, 8, 21, 10, tzinfo=timezone.utc)
    path = tmp_path / "shadow.sqlite3"
    store = EvidenceStore(path)
    runner = PersistentShadowRunner(store)
    runner.submit(_decision(now), state_id="state-1", expires_at=now + timedelta(minutes=5))

    first = runner.advance_tick(RawTick("XAUUSD", now + timedelta(seconds=1), 99.9, 100.0))
    duplicate = runner.advance_tick(RawTick("XAUUSD", now + timedelta(seconds=1), 99.9, 100.0))
    assert first[0].status == "OPEN"
    assert duplicate[0].duplicate_tick
    store.close()

    restarted = EvidenceStore(path)
    resumed = PersistentShadowRunner(restarted)
    closed = resumed.advance_tick(RawTick("XAUUSD", now + timedelta(seconds=2), 102.0, 102.1))
    assert closed[0].status == "TARGET"
    assert closed[0].outcome.r_multiple == 2
    assert restarted.load_active_shadow_positions(symbol="XAUUSD") == ()
    outcomes = restarted.load_shadow_outcomes()
    assert shadow_scorecard(outcomes)["edge-1"]["win_rate"] == 1
    restarted.close()


def test_shadow_runner_never_submits_or_imports_a_broker(tmp_path):
    source = (__import__("pathlib").Path(__import__("cipherfx_clean.shadow_runtime").shadow_runtime.__file__).read_text())
    assert "MetaTrader5" not in source
    assert ".submit(" not in source
