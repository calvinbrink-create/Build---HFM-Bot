from datetime import datetime, timedelta, timezone

import pytest

from cipherfx_clean.dashboard import outcome_view
from cipherfx_clean.contracts import TradeOutcome
from cipherfx_clean.intelligence.costs import FillEvidence, TransactionCosts
from cipherfx_clean.intelligence.correlation import pearson
from cipherfx_clean.intelligence.latency import LatencyTrace
from cipherfx_clean.intelligence.news import NewsEvent, news_regime


def test_operational_evidence_is_measurable_and_read_only():
    costs = TransactionCosts(0.1, 0.2, 0.0, 0.05, 0.01)
    assert costs.total == pytest.approx(0.36)
    fill = FillEvidence(10, 10.2, 1, 4)
    assert fill.slippage == pytest.approx(0.2)
    assert fill.latency_ns == 3
    assert pearson((1, 2, 3), (1, 2, 3)) == pytest.approx(1.0)
    now = datetime.now(timezone.utc)
    assert news_regime(now, (NewsEvent("n", "XAUUSD", now, "HIGH"),), timedelta(minutes=5)) == "NEWS_WINDOW"
    assert LatencyTrace(1, 2, 3, 4, 5).data_to_fill_ns == 4
    assert outcome_view(TradeOutcome("d", "CLOSED"))["status"] == "CLOSED"
