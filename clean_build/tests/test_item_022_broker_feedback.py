from datetime import datetime, timezone

from cipherfx_clean.broker_feedback import reconcile_broker_feedback
from cipherfx_clean.contracts import TradeDecision
from cipherfx_clean.execution import ExecutionReceipt
from cipherfx_clean.hfm_history import HfmDeal, TradeLifecycle
from cipherfx_clean.store import EvidenceStore


NOW = datetime(2026, 8, 21, 10, tzinfo=timezone.utc)


def _decision(decision_id="decision-full-id", symbol="XAUUSD", side="BUY"):
    return TradeDecision(
        decision_id, symbol, side, NOW, edge_id="edge-1", entry=100, stop=99,
        target=102, edge_version=1, confidence=.7, probability=.6,
        setup_type="TEST", stop_concept="INVALIDATION", target_concept="TARGET",
    )


def _deal(deal_id, entry_type, deal_type, *, comment="", order=100, position=10, price=100, profit=0):
    return HfmDeal(
        deal_id, position, order, "XAUUSD", deal_type, entry_type, "DEAL_REASON_EXPERT",
        260626, comment, 1, price, profit, 0, -1, profit - 1, NOW, NOW, 0, f"source-{deal_id}",
    )


def _lifecycle(status="CLOSED", symbol="XAUUSD", side="BUY"):
    return TradeLifecycle(
        "life-1", 10, symbol, side, status, NOW, NOW, 1, 1, 100, 102,
        20, -2, 0, 18, 300, (1, 2), ("source-1", "source-2"), (),
    )


def test_exact_broker_ticket_links_closed_trade_and_calculates_price_r(tmp_path):
    decision = _decision()
    receipt = ExecutionReceipt(
        decision.decision_id, "FILLED", "10",
        {"order_ticket": "100", "deal_ticket": "1", "position_ticket": "10"},
    )
    deals = (
        _deal(1, "DEAL_ENTRY_IN", "DEAL_TYPE_BUY", comment="cipherfx:decision-full-id"),
        _deal(2, "DEAL_ENTRY_OUT", "DEAL_TYPE_SELL", price=102, profit=20),
    )

    report, rows = reconcile_broker_feedback(
        history_report_id="history-1", decisions=(decision,), receipts=(receipt,),
        deals=deals, lifecycles=(_lifecycle(),),
    )

    assert report.matched_closed_count == 1
    assert report.training_eligible_count == 1
    assert rows[0].decision_id == decision.decision_id
    assert rows[0].price_r_multiple == 2
    assert rows[0].net_profit_account_currency == 18

    store = EvidenceStore(tmp_path / "feedback.sqlite3")
    store.write_decision(decision)
    store.write_broker_feedback(report, rows)
    store.write_broker_feedback(report, rows)
    assert store._conn.execute("SELECT COUNT(*) FROM broker_trade_feedback").fetchone()[0] == 1
    store.close()


def test_truncated_legacy_comment_is_reported_unresolved_and_never_guessed():
    decision = _decision("503550f4c2e7360681265a-full")
    deals = (
        _deal(1, "DEAL_ENTRY_IN", "DEAL_TYPE_BUY", comment="cipherfx:503550f4c2e7360681265a"),
        _deal(2, "DEAL_ENTRY_OUT", "DEAL_TYPE_SELL", price=102, profit=20),
    )

    report, rows = reconcile_broker_feedback(
        history_report_id="history-1", decisions=(decision,), receipts=(),
        deals=deals, lifecycles=(_lifecycle(),),
    )

    assert report.unresolved_count == 1
    assert report.training_eligible_count == 0
    assert rows[0].decision_id is None
    assert rows[0].status == "UNRESOLVED_IDENTITY"
    assert "TRUNCATED_OR_UNKNOWN_DECISION_COMMENT" in rows[0].reason_codes


def test_symbol_or_direction_mismatch_is_not_learning_eligible():
    decision = _decision(symbol="UK100", side="SELL")
    receipt = ExecutionReceipt(decision.decision_id, "FILLED", "10")
    report, rows = reconcile_broker_feedback(
        history_report_id="history-1", decisions=(decision,), receipts=(receipt,),
        deals=(_deal(1, "DEAL_ENTRY_IN", "DEAL_TYPE_BUY"),),
        lifecycles=(_lifecycle(),),
    )

    assert report.mismatch_count == 1
    assert not rows[0].training_eligible
    assert set(rows[0].reason_codes) == {"DIRECTION_MISMATCH", "SYMBOL_MISMATCH"}
