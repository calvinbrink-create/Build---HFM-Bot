from datetime import datetime, timezone

from cipherfx_clean.contracts import MarketState, TradeDecision, TradeOutcome
from cipherfx_clean.feedback import ClosedTrade, LearningFeedback
from cipherfx_clean.governance import PromotionPolicy, demote_if_degraded, promote_if_valid
from cipherfx_clean.intelligence.research import EdgeCandidate, EdgeStatistics
from cipherfx_clean.management import Position, PositionManager
from cipherfx_clean.replay import replay


class Broker:
    def modify_stop(self, position, stop):
        raise AssertionError("not called")

    def close(self, position):
        raise AssertionError("not called")


def test_management_feedback_replay_and_governance_are_separate():
    now = datetime.now(timezone.utc)
    position = Position("p", "d", "XAUUSD", "BUY", 1, 10, 9, 12)
    assert PositionManager(Broker()).observe(position, 11).action == "HOLD"
    feedback = LearningFeedback()
    outcome = feedback.to_outcome(ClosedTrade("d", "e", "BUY", 1.0, 0.1, now, "TREND_UP", "LONDON"))
    assert outcome.edge_id == "e"
    assert feedback.accept(TradeOutcome("d", "CLOSED"))
    decision = TradeDecision("d", "XAUUSD", "NO_TRADE", now)
    assert replay((MarketState("XAUUSD", now, {}),), lambda _: decision).decisions == (decision,)
    stats = EdgeStatistics("e", 100, 0.6, 0.2, 0.55, 0.65, 10)
    candidate = EdgeCandidate("e", 1, "SHADOW")
    approved = promote_if_valid(candidate, stats, PromotionPolicy(50, 0.0, 0.5))
    assert approved.status == "APPROVED"
    assert demote_if_degraded(approved, EdgeStatistics("e", 100, 0.4, -0.1, 0.3, 0.5, 10), 0).status == "DEGRADED"
