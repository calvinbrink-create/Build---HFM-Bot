"""Phase 4 operational observability and recovery tests."""
from __future__ import annotations

from pathlib import Path
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest

from trading.execution_service import ExecutionRequest, ExecutionService
from trading.remediation import BoundedShadowWorker
from trading.observability import (
    GLOBAL_OPERATIONAL_METRICS,
    InstrumentedSQLiteConnection,
)


class Phase4ObservabilityTests(unittest.TestCase):
    def setUp(self):
        GLOBAL_OPERATIONAL_METRICS.reset()

    def test_process_resource_snapshot(self):
        snapshot = GLOBAL_OPERATIONAL_METRICS.snapshot()
        process = snapshot["process"]
        self.assertGreater(process["pid"], 0)
        self.assertGreaterEqual(process["rss_bytes"], 0)
        self.assertGreaterEqual(process["thread_count"], 1)
        self.assertIn("cpu_percent", process)

    def test_sqlite_commit_latency_and_wal_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            conn = sqlite3.connect(
                path,
                factory=InstrumentedSQLiteConnection,
                timeout=0.5,
            )
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE sample (value INTEGER)")
            conn.execute("INSERT INTO sample(value) VALUES (1)")
            conn.commit()
            conn.close()
            snapshot = GLOBAL_OPERATIONAL_METRICS.snapshot(path, db_sample_seconds=0.0)
            self.assertEqual(snapshot["database"]["status"], "PASS")
            self.assertEqual(snapshot["database"]["journal_mode"].lower(), "wal")
            self.assertGreaterEqual(snapshot["database"]["commit_count"], 1)
            self.assertGreaterEqual(snapshot["database"]["last_commit_latency_ms"], 0.0)

    def test_stale_market_data_is_measurable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "last_tick.json"
            path.write_text("{}")
            old = time.time() - 90.0
            os.utime(path, (old, old))
            age = time.time() - path.stat().st_mtime
            self.assertGreaterEqual(age, 89.0)
            self.assertTrue(age > 60.0)

    def test_broker_timeout_can_retry_without_duplicate_completion(self):
        calls = []
        attempts = {"count": 0}

        def submit(request):
            attempts["count"] += 1
            calls.append(request.idempotency_key)
            if attempts["count"] == 1:
                raise TimeoutError("simulated broker timeout")
            return {"accepted": True}

        service = ExecutionService(submit)
        request = ExecutionRequest("EURUSD", "BUY", 0.1, 1.0, 2.0, request_id="phase4-timeout")
        with self.assertRaises(TimeoutError):
            service.submit(request)
        self.assertEqual(service.submit(request), {"accepted": True})
        self.assertEqual(calls, ["phase4-timeout", "phase4-timeout"])
        self.assertEqual(attempts["count"], 2)

    def test_delayed_event_queue_recovery_and_drop_metric(self):
        started = threading.Event()
        release = threading.Event()
        handled = []

        def handler(item):
            started.set()
            release.wait(timeout=2.0)
            handled.append(item["id"])

        worker = BoundedShadowWorker(handler, maxsize=1)
        worker.start()
        self.assertTrue(worker.submit({"id": 1}))
        self.assertTrue(started.wait(timeout=2.0))
        self.assertTrue(worker.submit({"id": 2}))
        self.assertFalse(worker.submit({"id": 3}))
        release.set()
        worker.stop(timeout=3.0)
        self.assertEqual(worker.pending, 0)
        self.assertEqual(worker.dropped, 1)
        self.assertEqual(sorted(handled), [1, 2])

    def test_restart_preserves_active_lifecycle_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime_state.json"
            state = {
                "pending_setups": {
                    "EURUSD:BUY:phase4": {
                        "status": "EXECUTION_PENDING",
                        "setup_id": "EURUSD:BUY:phase4",
                        "execution_deadline": "2026-07-15T08:00:00+00:00",
                    }
                }
            }
            path.write_text(json.dumps(state))
            recovered = json.loads(path.read_text())
            setup = recovered["pending_setups"]["EURUSD:BUY:phase4"]
            self.assertEqual(setup["status"], "EXECUTION_PENDING")
            self.assertEqual(setup["setup_id"], "EURUSD:BUY:phase4")
            self.assertIn("execution_deadline", setup)


if __name__ == "__main__":
    unittest.main()
