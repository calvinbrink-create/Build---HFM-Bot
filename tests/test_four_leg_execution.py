from types import SimpleNamespace

from cipherfx_platform.execution import ExecutionEngine
from mt5_xm_gateway import MT5SymbolSpec


def test_active_plan_is_capped_at_four_legs():
    assert ExecutionEngine._leg_count(SimpleNamespace(max_pyramid_trades=12)) == 4


def test_four_leg_plan_preserves_total_volume_and_broker_steps(monkeypatch):
    monkeypatch.setenv("MT5_PYRAMID_SIZE_DECAY", "0.70")
    spec = MT5SymbolSpec(
        symbol="XAUUSD",
        volume_min=0.01,
        volume_step=0.01,
        volume_max=100.0,
    )
    total_volume = 1.23
    count = ExecutionEngine._effective_leg_count(total_volume, 4, spec)
    legs = ExecutionEngine._leg_volumes(total_volume, count, spec)

    assert count == 4
    assert len(legs) == 4
    assert sum(legs) == 1.23
    assert all(volume >= spec.volume_min for volume in legs)
    assert all(
        abs(volume / spec.volume_step - round(volume / spec.volume_step)) < 1e-8
        for volume in legs
    )


def test_adverse_entry_drift_is_blocked_but_favorable_drift_is_allowed(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from cipherfx_platform.contracts import TradeProposal

    monkeypatch.setenv("MT5_MAX_ENTRY_DRIFT_STOP_FRACTION", "0.10")
    now = datetime.now(timezone.utc)
    proposal = TradeProposal(
        proposal_id="drift-check",
        symbol="XAUUSD",
        asset_class="metal",
        side="BUY",
        entry_price=100.0,
        stop_loss=90.0,
        take_profit=120.0,
        volume_hint=0.0,
        strategy_name="test",
        score=0.0,
        score_components={},
        created_at=now,
        expires_at=now + timedelta(seconds=30),
        context={"setup": {"stop_distance": 10.0}},
    )
    assert ExecutionEngine._entry_drift_reason(proposal, 98.0) == ""
    assert "ENTRY_PRICE_DRIFT:BUY" in ExecutionEngine._entry_drift_reason(proposal, 101.1)
