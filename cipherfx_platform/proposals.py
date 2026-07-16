from __future__ import annotations

import hashlib
from datetime import timedelta

from .contracts import MarketAnalysis, ScoreResult, TradeProposal, SIDES, utc_now


class TradeProposalEngine:
    """Builds immutable proposals from analysis and scoring results."""

    def build(self, analysis: MarketAnalysis, score: ScoreResult) -> TradeProposal | None:
        if not score.passed or analysis.side not in SIDES:
            return None
        price = float(analysis.metrics.get("current_price") or 0.0)
        if price <= 0 or analysis.stop_distance <= 0:
            return None
        stop = price - analysis.stop_distance if analysis.side == "BUY" else price + analysis.stop_distance
        target_distance = analysis.stop_distance * max(analysis.target_r, 1.0)
        target = price + target_distance if analysis.side == "BUY" else price - target_distance
        material = f"{analysis.symbol}|{analysis.side}|{analysis.m5.bar_open_time.isoformat()}|{analysis.h4.bar_open_time.isoformat()}|{analysis.h1.bar_open_time.isoformat()}|{analysis.m15.bar_open_time.isoformat()}"
        proposal_id = hashlib.sha256(material.encode()).hexdigest()[:24]
        created = utc_now()
        return TradeProposal(
            proposal_id, analysis.symbol, analysis.asset_class, analysis.side,
            price, stop, target, 0.0, analysis.strategy_name, score.total,
            score.components, created, created + timedelta(seconds=30),
            {"timeframes": score.timeframe_scores, "reasons": score.reasons},
        )
