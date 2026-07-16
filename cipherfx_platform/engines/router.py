from __future__ import annotations

from ..contracts import MarketSnapshot, TradeProposal
from ..database import DatabaseLayer
from .forex import ForexLearningEngine
from .indices import IndicesLearningEngine
from .metals import MetalsLearningEngine


class MarketIntelligenceEngine:
    """Routes snapshots to exactly one independent asset engine."""

    def __init__(self, database: DatabaseLayer | None = None):
        self.engines = {
            "forex": ForexLearningEngine(database),
            "index": IndicesLearningEngine(database),
            "metal": MetalsLearningEngine(database),
        }
        self.last_report = {}

    def propose(self, snapshot: MarketSnapshot) -> TradeProposal | None:
        engine = self.engines[snapshot.asset_class]
        proposal = engine.propose(snapshot)
        self.last_report = dict(engine.last_report)
        return proposal

    def record_outcome(self, asset_class: str, trade_id: str, symbol: str, result_r: float, pnl: float) -> None:
        self.engines[asset_class].record_outcome(trade_id, symbol, result_r, pnl)
