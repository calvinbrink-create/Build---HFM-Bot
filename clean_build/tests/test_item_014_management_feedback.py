from datetime import datetime, timedelta, timezone

from cipherfx_clean.contracts import TradeOutcome
from cipherfx_clean.feedback import ClosedTrade, LearningFeedback, PostTradeReview
from cipherfx_clean.management import (
    ManagementPolicy,
    Position,
    PositionManager,
    PositionMark,
    PositionMonitor,
)


class Broker:
    def __init__(self):
        self.calls = []

    def modify_stop(self, position, stop):
        self.calls.append(("MODIFY_STOP", position.position_id, stop))

    def close(self, position):
        self.calls.append(("CLOSE", position.position_id))


def test_position_monitor_tracks_mfe_mae_drawdown_time_and_informational_rank():
    monitor = PositionMonitor()
    opened = datetime(2026, 8, 21, 10, tzinfo=timezone.utc)
    position = Position("p1", "d1", "XAUUSD", "BUY", 1, 100, 99, 103)

    monitor.record(position, observed_at=opened, price=101, spread=.2, risk_exposure=100)
    latest = monitor.record(
        position, observed_at=opened + timedelta(minutes=5), price=100.5,
        spread=.3, risk_exposure=100, volatility=.4, session="LONDON",
        liquidity_event="SWEEP", momentum_state="WEAKENING",
    )

    assert latest.current_r == .5
    assert latest.mfe_r == 1
    assert latest.mae_r == .5
    assert latest.drawdown_r == .5
    assert latest.holding_seconds == 300
    assert latest.rank == "HEALTHY"


def test_management_uses_only_explicit_position_policy_and_applies_once():
    broker = Broker()
    manager = PositionManager(broker)
    position = Position("p1", "d1", "XAUUSD", "BUY", 1, 100, 99, 104)
    mark = PositionMark(102, 2, .2, 2, -.2)
    policy = ManagementPolicy(partial_levels=(1.5,), profit_lock_levels=((1.0, .25), (2.0, 1.0)))

    partial = manager.evaluate(position, mark, policy)
    assert partial.action == "PARTIAL_CLOSE"
    locked = manager.evaluate(position, mark, policy, completed_reasons=(partial.reason,))
    assert locked.action == "MODIFY_STOP"
    assert locked.new_stop == 101
    manager.apply(position, locked)
    assert broker.calls == [("MODIFY_STOP", "p1", 101)]


def test_terminal_outcomes_feed_learning_without_modifying_the_trade_identity():
    now = datetime(2026, 8, 21, tzinfo=timezone.utc)
    trade = ClosedTrade(
        "decision-1", "edge-1", "BUY", 1.2, .1, now, "TREND_UP", "LONDON",
        ("breakout",), 1.8, -.4, 600, .02,
    )
    feedback = LearningFeedback()

    state = feedback.to_state_outcome(trade, outcome_kind="WINNER")
    review = PostTradeReview().review(
        trade, exit_reason="TARGET", mfe=trade.mfe, mae=trade.mae,
        slippage=trade.slippage, state_id="state-1",
    )

    assert state.state_id == trade.decision_id
    assert state.r_multiple == 1.2
    assert review.entry_reason == ("breakout",)
    assert all(feedback.accept(TradeOutcome("d", status)) for status in ("CLOSED", "EXPIRED", "EXECUTION_FAILED", "REJECTED", "EXECUTION_BLOCKED", "NO_TRADE"))
