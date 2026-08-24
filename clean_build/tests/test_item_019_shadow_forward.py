from datetime import datetime, timedelta, timezone

from cipherfx_clean.contracts import RawTick, TradeDecision
from cipherfx_clean.shadow import advance_shadow, create_shadow_position
from cipherfx_clean.store import EvidenceStore


UTC = timezone.utc
NOW = datetime(2026, 8, 24, 8, tzinfo=UTC)


def _decision(side="BUY"):
    return TradeDecision(
        "decision-1", "XAUUSD", side, NOW, ("state-1", "validation-1"),
        "edge-1", 100.0,
        99.0 if side == "BUY" else 101.0,
        102.0 if side == "BUY" else 98.0,
        1, .7, .6, .01, "RAW_STATE", (99.9, 100.1),
        "STRUCTURAL_INVALIDATION", "OBSERVED_TARGET", ("SHADOW_ONLY",),
    )


def test_shadow_uses_executable_bid_ask_and_never_calls_a_broker(tmp_path):
    position = create_shadow_position(
        _decision(), state_id="state-1", expires_at=NOW + timedelta(minutes=5),
        extra_cost_return=.001,
    )
    waiting, outcome = advance_shadow(position, RawTick("XAUUSD", NOW + timedelta(seconds=1), 100.2, 100.4))
    assert waiting.status == "WAITING"
    assert outcome is None
    opened, outcome = advance_shadow(waiting, RawTick("XAUUSD", NOW + timedelta(seconds=2), 99.8, 100.0))
    assert opened.status == "OPEN"
    assert opened.entry_price == 100.0
    assert outcome is None
    closed, outcome = advance_shadow(opened, RawTick("XAUUSD", NOW + timedelta(seconds=3), 102.0, 102.2))
    assert closed.status == "TARGET"
    assert outcome.status == "TARGET"
    assert outcome.r_multiple == 1.9

    store = EvidenceStore(tmp_path / "shadow.sqlite3")
    store.write_shadow_position(position)
    store.write_shadow_mark("mark-1", position.shadow_id, NOW, {"bid": 99.8, "ask": 100.0})
    store.write_shadow_outcome(outcome)
    assert store._conn.execute("SELECT COUNT(*) FROM shadow_outcomes").fetchone()[0] == 1
    store.close()


def test_unfilled_shadow_expires_without_fabricating_an_entry():
    position = create_shadow_position(
        _decision(), state_id="state-1", expires_at=NOW + timedelta(minutes=5),
    )
    expired, outcome = advance_shadow(
        position,
        RawTick("XAUUSD", NOW + timedelta(minutes=5), 105.0, 105.2),
    )

    assert expired.status == "EXPIRED"
    assert outcome.entry_price is None
    assert outcome.r_multiple is None
