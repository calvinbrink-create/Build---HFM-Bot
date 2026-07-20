import sys
import sqlite3
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, "/opt/cipherfx_mt5")
from dashboard.backend import state_store


class DashboardTradeMetricsTests(unittest.TestCase):
    def test_win_rate_excludes_breakeven_from_decided_rate(self):
        rows = [
            {"realized": 10.0, "outcome": "win", "closed_at": "2026-07-16T10:00:00+00:00", "trade_date": "2026-07-16"},
            {"realized": -5.0, "outcome": "loss", "closed_at": "2026-07-16T10:01:00+00:00", "trade_date": "2026-07-16"},
            {"realized": 0.0, "outcome": "breakeven", "closed_at": "2026-07-16T10:02:00+00:00", "trade_date": "2026-07-16"},
        ]
        summary = state_store._deals_summary(rows, "today", "2026-07-16", "2026-07-16")
        self.assertEqual(summary["total_trades"], 3)
        self.assertEqual(summary["decided_trades"], 2)
        self.assertEqual(summary["win_rate"], 50.0)
        self.assertEqual(summary["win_rate_including_breakeven"], 33.3)

    def test_symbol_results_include_win_loss_breakeven_and_rates(self):
        rows = [
            {"symbol": "EURUSD", "realized": 10.0},
            {"symbol": "EURUSD", "realized": -5.0},
            {"symbol": "EURUSD", "realized": 0.0},
        ]
        summary = state_store._deals_summary(rows, "today", "2026-07-16", "2026-07-16")
        self.assertEqual(summary["by_symbol"], [{
            "symbol": "EURUSD", "trades": 3, "pnl": 5.0,
            "wins": 1, "losses": 1, "breakeven": 1,
            "win_rate": 50.0, "loss_rate": 50.0,
        }])

    def test_expired_feedback_is_not_presented_as_a_broker_trade(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.db"
            conn = sqlite3.connect(path)
            conn.executescript("""
                CREATE TABLE trade_history(
                    trade_id TEXT PRIMARY KEY, proposal_id TEXT, symbol TEXT,
                    asset_class TEXT, side TEXT, realized_pnl REAL,
                    initial_risk REAL, result_r REAL, exit_reason TEXT,
                    closed_at TEXT, metrics_json TEXT
                );
                CREATE TABLE executions(
                    proposal_id TEXT PRIMARY KEY, position_ticket INTEGER,
                    order_ticket INTEGER, deal_ticket INTEGER, volume REAL,
                    fill_price REAL, filled_at TEXT
                );
            """)
            now = "2026-07-16T10:00:00+00:00"
            conn.execute("INSERT INTO trade_history VALUES (?,?,?,?,?,?,?,?,?,?,?)", ("real-1", "p-1", "EURUSD", "forex", "BUY", 12.0, 10.0, 1.2, "BROKER_HISTORY", now, "{}"))
            conn.execute("INSERT INTO trade_history VALUES (?,?,?,?,?,?,?,?,?,?,?)", ("expired:p-2", "p-2", "EURUSD", "forex", "BUY", 0.0, 0.0, 0.0, "PROPOSAL_EXPIRED", now, "{}"))
            conn.execute("INSERT INTO executions VALUES (?,?,?,?,?,?,?)", ("p-1", 123, 123, 456, 1.0, 1.1, now))
            conn.commit()
            conn.close()
            previous = state_store.DB_PATH
            state_store.DB_PATH = str(path)
            try:
                rows = state_store._read_modular_history("today", "2026-07-16", "2026-07-16", 50)
            finally:
                state_store.DB_PATH = previous
            self.assertEqual([row["trade_id"] for row in rows], ["real-1"])
            self.assertEqual(rows[0]["symbol"], "EURUSD")


if __name__ == "__main__":
    unittest.main()
