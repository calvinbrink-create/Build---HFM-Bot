from datetime import datetime, timezone

from cipherfx_clean.contracts import TradeDecision, TradeOutcome
from cipherfx_clean.dashboard import EvidenceDashboard
from cipherfx_clean.historical_memory import HistoricalPath, HistoricalState
from cipherfx_clean.store import EvidenceStore


def test_dashboard_reads_only_canonical_runtime_research_and_trade_evidence(tmp_path):
    path = tmp_path / "evidence.sqlite3"
    store = EvidenceStore(path)
    now = datetime(2026, 8, 23, tzinfo=timezone.utc)
    decision = TradeDecision("decision-1", "XAUUSD", "NO_TRADE", now)
    state = HistoricalState("state-1", "dataset-1", "XAUUSD", now, (1.0,), ("SESSION:QUIET",), ("source-1",))
    store.write_historical_cases(((state, HistoricalPath(state.state_id, (), "BAR_OHLC_NO_EXECUTABLE_TICKS")),))
    store.write_experiment("experiment-1", "NO_EDGE", {"reason": "negative expectancy"})
    store.write_graveyard("grave-1", "experiment-1", "NO_EDGE", {"experiment_id": "experiment-1"})
    store.write_decision(decision)
    store.write_outcome(TradeOutcome(decision.decision_id, "NO_TRADE"))
    store.write_lineage("lineage-1", state.state_id, decision.decision_id, decision.decision_id, {"source_ids": state.source_ids})
    store.close()

    dashboard = EvidenceDashboard(path)
    overview = dashboard.overview()
    research = dashboard.research()
    trade = dashboard.trade("decision-1")

    assert overview["counts"]["historical_states"] == 1
    assert overview["memory_coverage"][0]["symbol"] == "XAUUSD"
    assert research["experiments"][0]["status"] == "NO_EDGE"
    assert research["graveyard_count"] == 1
    assert trade["decision"]["action"] == "NO_TRADE"
    assert trade["lineage"][0]["state_id"] == "state-1"
    assert dashboard.trade("missing") is None
    dashboard.close()
