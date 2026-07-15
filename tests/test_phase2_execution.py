from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import tempfile
import time
import unittest

from trading.execution_service import DuplicateExecutionError, ExecutionRequest, ExecutionService
from trading.state_machine import (
    InvalidTransition,
    LifecycleState,
    TimedRLock,
    is_terminal,
    transition_state,
)
from mt5_bot import XM_MT5_Bot


class Phase2ExecutionAcceptance(unittest.TestCase):
    def test_state_transition_validation(self):
        self.assertEqual(
            transition_state("WAITING_M5", "CONFIRMED").value,
            "ENTRY_VALIDATION",
        )
        self.assertEqual(
            transition_state("CONFIRMED", "ORDER_SUBMITTED").value,
            "ORDER_SUBMITTED",
        )
        with self.assertRaises(InvalidTransition):
            transition_state("ORDER_SUBMITTED", "CONFIRMATION_WAIT")

    def test_terminal_state_is_irreversible(self):
        self.assertTrue(is_terminal(LifecycleState.EXECUTED))
        with self.assertRaises(InvalidTransition):
            transition_state("EXECUTED", "IDLE")
        with self.assertRaises(InvalidTransition):
            transition_state("EXPIRED", "ORDER_SUBMITTED")

    def test_duplicate_signal_identity_is_stable(self):
        first = ExecutionRequest("EURUSD", "BUY", 0.10, 1.09, 1.12, request_id="setup-1")
        second = ExecutionRequest("EURUSD", "BUY", 0.10, 1.09, 1.12, request_id="setup-1")
        self.assertEqual(first.idempotency_key, second.idempotency_key)

    def test_duplicate_order_prevention(self):
        calls = []
        service = ExecutionService(lambda request: calls.append(request.idempotency_key) or {"ticket": 7})
        request = ExecutionRequest("EURUSD", "BUY", 0.1, 1.09, 1.12, request_id="setup-1")
        first = service.submit(request)
        second = service.submit(request)
        self.assertEqual(first, second)
        self.assertEqual(calls, ["setup-1"])

    def test_concurrent_signal_handling(self):
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def submit(request):
            calls.append(request.idempotency_key)
            entered.set()
            release.wait(2.0)
            return {"ticket": 8}

        service = ExecutionService(submit)
        request = ExecutionRequest("EURUSD", "SELL", 0.1, 1.13, 1.10, request_id="setup-concurrent")
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(service.submit, request)
            self.assertTrue(entered.wait(1.0))
            second = pool.submit(service.submit, request)
            with self.assertRaises(DuplicateExecutionError):
                second.result()
            release.set()
            self.assertEqual(first.result(), {"ticket": 8})
        self.assertEqual(calls, ["setup-concurrent"])

    def test_execution_failure_can_retry_without_duplicate_success(self):
        attempts = []

        def submit(_request):
            attempts.append("attempt")
            if len(attempts) == 1:
                raise ConnectionError("broker disconnected")
            return {"ticket": 9}

        service = ExecutionService(submit)
        request = ExecutionRequest("EURUSD", "BUY", 0.1, 1.09, 1.12, request_id="retry-1")
        with self.assertRaises(ConnectionError):
            service.submit(request)
        self.assertEqual(service.submit(request), {"ticket": 9})
        self.assertEqual(len(attempts), 2)

    def test_execution_journal_survives_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.json"
            bot = object.__new__(XM_MT5_Bot)
            bot.state_file = path
            bot.state = {"open_trades": {}, "closed_tickets": []}
            bot._state_io_lock = threading.RLock()
            bot._audit_io_lock = threading.RLock()
            bot._record_execution_lifecycle(
                "CLAIMED",
                {"setup_id": "setup-restart", "symbol": "EURUSD", "side": "BUY"},
                request_id="setup-restart",
                reason="claim persisted",
            )
            restored = object.__new__(XM_MT5_Bot)
            restored.state_file = path
            loaded = restored._load_state()
            self.assertEqual(loaded["execution_journal"]["setup-restart"]["last_stage"], "CLAIMED")

    def test_timed_lock_reports_slow_hold(self):
        events = []
        lock = TimedRLock("test", observer=events.append, slow_hold_seconds=0.01)
        with lock:
            time.sleep(0.02)
        self.assertTrue(any(item.get("event") == "LOCK_HOLD_SLOW" for item in events))


if __name__ == "__main__":
    unittest.main(verbosity=2)
