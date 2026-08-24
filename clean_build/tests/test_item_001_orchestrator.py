from datetime import datetime, timezone

from cipherfx_clean.intelligence.engine import IntelligenceEngine
from cipherfx_clean.intelligence.orchestrator import IntelligenceOrchestrator
from cipherfx_clean.intelligence.provider import StructuredResearchProvider
from cipherfx_clean.market import MarketSnapshot


def test_complete_intelligence_path_returns_structured_decision():
    now = datetime.now(timezone.utc)
    provider = StructuredResearchProvider(
        lambda request: {
            "decision_id": "decision-1",
            "action": "NO_TRADE",
            "edge_id": "",
            "evidence": ["report_received", "no_approved_edge"],
        }
    )
    snapshot = MarketSnapshot("XAUUSD", now, (), {})
    orchestrator = IntelligenceOrchestrator(IntelligenceEngine(), provider)
    report = orchestrator.analyse(snapshot)
    result = orchestrator.decide(
        snapshot,
        report,
        dataset_id="dataset-1",
        analogue_ids=(),
        statistics={},
    )
    assert result.decision.action == "NO_TRADE"
    assert result.validation.valid is True
    assert result.report.symbol == "XAUUSD"
