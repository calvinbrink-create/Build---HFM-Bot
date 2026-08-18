
from __future__ import annotations
import hashlib
from datetime import timedelta
from ..contracts import MarketSnapshot, TradeProposal, SIDES, utc_now
from ..database import DatabaseLayer
from ..strategy_dispatch import build_setup
ENGINE_BY_ASSET = {"forex": "FOREX_ENGINE", "index": "INDICES_ENGINE", "metal": "METALS_ENGINE"}

def _setup_fingerprint(snapshot: MarketSnapshot, setup: dict) -> str:
    """Identify the active chart setup without using score or a new gate."""
    frames = setup.get("frames") or {}
    h4 = (frames.get("H4") or {}).get("bar_time", "")
    m15 = (frames.get("M15") or {}).get("bar_time", "")
    chart = setup.get("chart_setup") or {}
    material = "|".join(
        (
            str(snapshot.symbol).upper(),
            str(setup.get("side", "")),
            str(setup.get("setup_type", "")),
            str(setup.get("h4_direction", "")),
            str(chart.get("type", "")),
            str(setup.get("m5_trigger_type", "")),
            str(h4),
            str(m15),
        )
    )
    return hashlib.sha256(material.encode()).hexdigest()[:24]
class LearningEngine:
    """Single proposal authority with explicit asset-specific strategy dispatch."""
    def __init__(self, database: DatabaseLayer | None = None):
        self.database, self.last_report = database, {}
    def propose(self, snapshot: MarketSnapshot) -> TradeProposal | None:
        setup = build_setup(snapshot)
        engine = str(setup.get("engine") or ENGINE_BY_ASSET.get(snapshot.asset_class, "UNKNOWN_ENGINE"))
        self.last_report = {"engine": engine, "symbol": snapshot.symbol, "asset_class": snapshot.asset_class, "setup": setup, "decision": "SETUP_READY" if setup.get("valid") else "NO_SETUP"}
        if not setup.get("valid") or setup.get("side") not in SIDES: return None
        side, created = setup["side"], utc_now()
        material = "|".join((snapshot.symbol, side, setup.get("setup_type", ""), setup.get("frames", {}).get("M5", {}).get("bar_time", "")))
        proposal_id = hashlib.sha256(material.encode()).hexdigest()[:24]
        setup_fingerprint = _setup_fingerprint(snapshot, setup)
        return TradeProposal(proposal_id=proposal_id, symbol=snapshot.symbol, asset_class=snapshot.asset_class, side=side, entry_price=float(setup["entry"]), stop_loss=float(setup["stop_loss"]), take_profit=float(setup["take_profit"]), volume_hint=0.0, strategy_name=str(setup.get("strategy_name") or setup.get("setup_type") or engine), score=0.0, score_components={}, created_at=created, expires_at=created + timedelta(seconds=10), context={"engine": engine, "setup": setup, "score_role": "NOT_USED", "memory_role": "EVIDENCE_ONLY", "model_version": str(setup.get("strategy_name") or setup.get("setup_type") or engine), "memory_shape": (setup.get("memory") or {}).get("shape"), "setup_fingerprint": setup_fingerprint}, confidence=0.0, probability=0.0, reasoning=tuple(setup.get("reasons", ())), risk_amount=0.0)
    def record_outcome(self, asset_class: str, trade_id: str, symbol: str, result_r: float, pnl: float, metrics: dict | None = None) -> None:
        values = metrics or {}; observed = False
        try:
            from ..pattern_memory import PatternMemory
            observed = PatternMemory.load().observe(values.get("memory_shape"), values.get("side", ""), result_r)
        except Exception: pass
        engine = str(values.get("engine") or ENGINE_BY_ASSET.get(asset_class, "UNKNOWN_ENGINE")).upper()
        if self.database:
            self.database.record_engine_outcome(engine, trade_id, symbol, result_r, pnl, metrics={**values, "memory_observed": observed})
            self.database.event("LEARNING_OUTCOME_APPLIED", {"trade_id": trade_id, "symbol": symbol, "engine": engine, "result_r": result_r, "memory_observed": observed, "model_version": values.get("model_version")}, symbol=symbol)
    def record_expired(self, asset_class: str, proposal_id: str, symbol: str, metrics: dict | None = None) -> None:
        self.record_outcome(asset_class, f"expired:{proposal_id}", symbol, 0.0, 0.0, metrics={"expired": True, **(metrics or {})})
