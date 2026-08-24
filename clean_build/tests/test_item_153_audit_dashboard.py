from datetime import datetime, timezone

from cipherfx_clean.contracts import TradeDecision, TradeOutcome
from cipherfx_clean.dashboard import edge_view, trade_evidence_view
from cipherfx_clean.historical_memory import HistoricalAnalogueIndex, HistoricalPath, HistoricalState, HorizonOutcome
from cipherfx_clean.intelligence.governance import EdgeScorecard
from cipherfx_clean.intelligence.outcome_metrics import summarize_outcomes
from cipherfx_clean.runtime_trace import RuntimeTraceEvent, TRADE_STAGES, verify_completed_trace


def test_similarity_and_edge_summary_are_retrievable():
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = tuple(
        HistoricalState(f"s{i}", "dataset", "UK100", at, (float(i), 1.0), ("TREND_UP",), (f"src-{i}",))
        for i in range(4)
    )
    paths = tuple(HistoricalPath(row.state_id, (HorizonOutcome(300, .01 * (i + 1), .02, -.01),), "HFM_EXECUTABLE_TICKS") for i, row in enumerate(rows))
    index = HistoricalAnalogueIndex(rows, paths)
    similar = index.search(rows[-1], limit=2)
    summary = summarize_outcomes(((.1, True), (-.05, False)))
    assert similar and all(item.path is not None for item in similar)
    assert summary.sample_size == 2 and summary.expected_value_after_cost == .025


def test_trace_and_dashboard_projections_preserve_trade_evidence():
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    decision = TradeDecision(
        "d1", "UK100", "BUY", at, evidence=("tick-1",), edge_id="edge-1", entry=100.0,
        stop=99.0, target=102.0, edge_version=1, confidence=.7, probability=.6,
        expected_value=.1, setup_type="SETUP", entry_area=(99.9, 100.1),
        stop_concept="RANGE", target_concept="LIQUIDITY", reason_codes=("EVIDENCE",),
    )
    scorecard = EdgeScorecard("edge-1", 1, 10, .6, .1, 1.5, .2, .5, .1, (.3, .8))
    edge = edge_view(scorecard, "SHADOW", {"sample_size": 10})
    trade = trade_evidence_view(
        decision=decision, chart_ids=("before", "entry", "after"), reasoning=("EVIDENCE",),
        result=TradeOutcome("d1", "CLOSED"),
    )
    events = tuple(RuntimeTraceEvent("trace", i, stage, at, "d1", {}) for i, stage in enumerate(TRADE_STAGES, start=1))
    verification = verify_completed_trace(events, execution_required=True)
    assert edge["validation_state"] == "SHADOW"
    assert trade["result"]["status"] == "CLOSED" and len(trade["chart_ids"]) == 3
    assert verification.valid
