"""Phase 5 portfolio intelligence contract tests."""
from __future__ import annotations

import inspect
from pathlib import Path
import tempfile
import unittest

from cipherfx_platform.database import DatabaseLayer
from cipherfx_platform.runtime import ModularTradingRuntime
from portfolio_intelligence import PortfolioIntelligenceEngine, build_portfolio_snapshot


class Phase5PortfolioIntelligenceTests(unittest.TestCase):
    def snapshot(self, **overrides):
        facts = {
            "account": {"balance": 10000, "equity": 10100, "margin": 1000, "free_margin": 9100},
            "positions": [{
                "ticket": 1, "symbol": "EURUSD", "side": "BUY", "volume": 0.2,
                "price_open": 1.1, "price_current": 1.101, "profit": 20, "engine": "FOREX",
            }],
            "orders": [],
            "proposal_states": {"APPROVED": 2, "EXECUTED": 3, "REJECTED": 1, "CLOSED": 4},
            "closed_summaries": {
                "FOREX": {"closed_trades": 3, "realized_pnl": 45, "wins": 2, "losses": 1},
            },
            "operational": {"mt5_connected": True, "stale_tick_count": 0},
        }
        facts.update(overrides)
        return build_portfolio_snapshot(**facts)

    def test_contract_is_informational_only(self):
        authority = self.snapshot()["authority"]
        self.assertTrue(authority["informational_only"])
        for key in (
            "trade_decision_authority", "can_create_trades", "can_approve_trades",
            "can_reject_trades", "can_delay_trades", "can_modify_proposals",
            "can_modify_positions",
        ):
            self.assertFalse(authority[key])

    def test_portfolio_health_and_exposure_are_observational(self):
        snapshot = self.snapshot()
        self.assertEqual(snapshot["portfolio_health"]["risk_status"], "NORMAL")
        self.assertEqual(snapshot["portfolio_health"]["score"], 100)
        self.assertEqual(snapshot["exposure"]["directional_bias"], "BUY")
        self.assertEqual(snapshot["proposals"]["pending"], 2)
        self.assertEqual(snapshot["performance"]["closed_summaries"]["FOREX"]["wins"], 2)

    def test_stale_or_disconnected_data_is_reported_not_traded_on(self):
        snapshot = self.snapshot(operational={"mt5_connected": False, "stale_tick_count": 4})
        self.assertEqual(snapshot["portfolio_health"]["risk_status"], "CRITICAL")
        self.assertEqual(snapshot["portfolio_health"]["status"], "DATA_UNAVAILABLE")
        self.assertFalse(snapshot["authority"]["can_reject_trades"])

    def test_correlation_is_explicitly_unavailable_without_returns(self):
        self.assertEqual(self.snapshot()["correlation"]["status"], "UNAVAILABLE")

    def test_database_reads_are_read_only_and_aggregate(self):
        with tempfile.TemporaryDirectory() as directory:
            database = DatabaseLayer(Path(directory) / "platform.db")
            self.assertEqual(database.portfolio_proposal_states(), {})
            self.assertEqual(database.portfolio_closed_summaries(), {})

    def test_runtime_publishes_after_management_and_feedback(self):
        source = inspect.getsource(ModularTradingRuntime.run_once)
        self.assertLess(source.index("self.management.monitor()"), source.index("self.feedback.reconcile_closed_trades"))
        self.assertLess(source.index("self.feedback.reconcile_closed_trades"), source.index("self._publish_portfolio_intelligence()"))
        self.assertNotIn("if snapshot", source)

    def test_engine_facade_has_no_trade_api(self):
        engine = PortfolioIntelligenceEngine()
        self.assertTrue(callable(engine.snapshot))
        for name in ("approve", "reject", "submit", "close"):
            self.assertFalse(hasattr(engine, name))


if __name__ == "__main__":
    unittest.main()
