import json
import unittest
from datetime import datetime, timezone

from cipherfx_platform.planning import (
    PLANNING_TIMEFRAMES,
    PremarketPlanningEngine,
)


class FakeConfig:
    def __init__(self, symbols):
        self._symbols = list(symbols)

    def all_symbols(self):
        return list(self._symbols)

    def group_for_symbol(self, symbol):
        return "forex" if symbol in {"EURUSD", "USDJPY"} else "indices"


class FakeDatabase:
    def __init__(self, rows):
        self.rows = rows
        self.saved = []

    def latest_snapshots(self, symbols):
        wanted = {str(symbol).upper() for symbol in symbols}
        return [row for row in self.rows if row["symbol"].upper() in wanted]

    def save_premarket_plan(self, **kwargs):
        self.saved.append(kwargs)


class FakeLearning:
    def __init__(self):
        self.last_report = {
            "scores": {frame: {"BUY": 50.0, "SELL": 50.0} for frame in PLANNING_TIMEFRAMES},
            "totals": {"BUY": 50.0, "SELL": 50.0},
            "feature_scores": {},
            "threshold": 70.0,
            "reasons": ["WATCHING"],
        }
        self.seen_frames = []

    def propose(self, snapshot):
        self.seen_frames.append(set(snapshot.frames))
        return None


def snapshot_row(symbol):
    stamp = "2026-07-17T06:00:00+00:00"
    frames = {
        frame: [
            {
                "timestamp": stamp,
                "open": 1.0,
                "high": 1.1,
                "low": 0.9,
                "close": 1.05,
                "volume": 10,
            }
        ]
        for frame in PLANNING_TIMEFRAMES
    }
    return {
        "symbol": symbol,
        "asset_class": "forex",
        "captured_at": stamp,
        "tick_timestamp": stamp,
        "payload_json": json.dumps(
            {
                "frames": frames,
                "tick": {
                    "bid": 1.04,
                    "ask": 1.05,
                    "timestamp": stamp,
                    "received_at": stamp,
                },
                "freshness": {},
            }
        ),
    }


class PremarketPlanningTests(unittest.TestCase):
    def test_next_market_date_skips_weekend(self):
        cases = (
            (datetime(2026, 7, 19, tzinfo=timezone.utc), "2026-07-20"),
            (datetime(2026, 7, 20, tzinfo=timezone.utc), "2026-07-21"),
            (datetime(2026, 7, 21, tzinfo=timezone.utc), "2026-07-22"),
            (datetime(2026, 7, 22, tzinfo=timezone.utc), "2026-07-23"),
            (datetime(2026, 7, 23, tzinfo=timezone.utc), "2026-07-24"),
            (datetime(2026, 7, 24, tzinfo=timezone.utc), "2026-07-27"),
            (datetime(2026, 7, 25, tzinfo=timezone.utc), "2026-07-27"),
        )
        for current, expected in cases:
            with self.subTest(current=current):
                self.assertEqual(
                    PremarketPlanningEngine.next_market_date(current).isoformat(),
                    expected,
                )

    def test_refresh_uses_all_ten_frames_and_stays_informational(self):
        database = FakeDatabase([snapshot_row("EURUSD")])
        learning = FakeLearning()
        engine = PremarketPlanningEngine(
            database,
            learning,
            FakeConfig(["EURUSD"]),
            interval_seconds=300,
        )

        summary = engine.refresh(datetime(2026, 7, 19, 12, tzinfo=timezone.utc))

        self.assertEqual(summary["target_market_date"], "2026-07-20")
        self.assertEqual(summary["planned"], 0)
        self.assertEqual(summary["watching"], 1)
        self.assertEqual(learning.seen_frames, [set(PLANNING_TIMEFRAMES)])
        self.assertEqual(len(database.saved), 1)
        saved = database.saved[0]
        self.assertEqual(saved["status"], "WATCHING")
        self.assertEqual(saved["setup_id"], "plan:2026-07-20:EURUSD")
        self.assertFalse(saved["payload"]["is_trade_proposal"])
        self.assertEqual(saved["payload"]["execution_status"], "WAITING_FOR_LIVE_REVALIDATION")
        self.assertEqual(saved["payload"]["timeframes"], list(PLANNING_TIMEFRAMES))

    def test_missing_snapshot_is_reported_per_symbol(self):
        database = FakeDatabase([])
        engine = PremarketPlanningEngine(
            database,
            FakeLearning(),
            FakeConfig(["EURUSD", "USDJPY"]),
        )

        summary = engine.refresh(datetime(2026, 7, 20, 12, tzinfo=timezone.utc))

        self.assertEqual(summary["waiting_for_snapshot"], 2)
        self.assertEqual([item["status"] for item in summary["plans"]], [
            "WAITING_FOR_SNAPSHOT",
            "WAITING_FOR_SNAPSHOT",
        ])
        self.assertEqual(len(database.saved), 2)


if __name__ == "__main__":
    unittest.main()
