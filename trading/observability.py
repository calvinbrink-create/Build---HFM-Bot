"""Production-only operational telemetry for CipherFX.

This module observes process, worker, broker, and SQLite health.  It does not
make trading decisions and it never changes strategy or risk state.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import os
from pathlib import Path
import resource
import sqlite3
import threading
import time
from typing import Any

UTC = timezone.utc


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _proc_value(name: str) -> int | None:
    try:
        for raw in Path("/proc/self/status").read_text().splitlines():
            if raw.startswith(name + ":"):
                return int(raw.split(":", 1)[1].strip().split()[0])
    except (OSError, ValueError):
        return None
    return None


class OperationalMetrics:
    """Thread-safe counters and snapshots for one Python process."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._counters: dict[str, int] = defaultdict(int)
        self._last_errors: dict[str, str] = {}
        self._last_commit_latency_ms = 0.0
        self._max_commit_latency_ms = 0.0
        self._last_broker_latency_ms = 0.0
        self._max_broker_latency_ms = 0.0
        self._last_process_cpu = 0.0
        self._last_process_sample = time.monotonic()
        self._last_db_sample = 0.0
        self._last_db_snapshot: dict[str, Any] = {
            "status": "NOT_SAMPLED",
            "probe_latency_ms": None,
            "journal_mode": "",
            "db_bytes": 0,
            "wal_bytes": 0,
        }

    def reset(self) -> None:
        """Reset counters for isolated tests only."""
        with self._lock:
            self._counters.clear()
            self._last_errors.clear()
            self._last_commit_latency_ms = 0.0
            self._max_commit_latency_ms = 0.0
            self._last_broker_latency_ms = 0.0
            self._max_broker_latency_ms = 0.0
            self._last_process_cpu = 0.0
            self._last_process_sample = time.monotonic()
            self._last_db_sample = 0.0
            self._last_db_snapshot = {
                "status": "NOT_SAMPLED",
                "probe_latency_ms": None,
                "journal_mode": "",
                "db_bytes": 0,
                "wal_bytes": 0,
            }

    def _record(self, counter: str, *, latency_ms: float | None = None, error: str = "") -> None:
        with self._lock:
            self._counters[counter] += 1
            if latency_ms is not None:
                value = round(max(0.0, float(latency_ms)), 3)
                if counter == "db_commit_success":
                    self._last_commit_latency_ms = value
                    self._max_commit_latency_ms = max(self._max_commit_latency_ms, value)
                elif counter == "broker_request_success":
                    self._last_broker_latency_ms = value
                    self._max_broker_latency_ms = max(self._max_broker_latency_ms, value)
            if error:
                self._last_errors[counter] = str(error)[:240]

    def record_db_commit(self, success: bool, latency_ms: float, error: str = "") -> None:
        self._record("db_commit_success" if success else "db_commit_failure", latency_ms=latency_ms, error=error)

    def record_broker_request(self, success: bool, latency_ms: float, error: str = "") -> None:
        self._record("broker_request_success" if success else "broker_request_failure", latency_ms=latency_ms, error=error)

    def record_event(self, name: str, count: int = 1, error: str = "") -> None:
        with self._lock:
            self._counters[str(name)] += int(count)
            if error:
                self._last_errors[str(name)] = str(error)[:240]

    def _process_snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        usage = resource.getrusage(resource.RUSAGE_SELF)
        cpu_time = float(usage.ru_utime + usage.ru_stime)
        with self._lock:
            elapsed = max(1e-6, now - self._last_process_sample)
            cpu_delta = max(0.0, cpu_time - self._last_process_cpu)
            self._last_process_cpu = cpu_time
            self._last_process_sample = now
        cpu_percent = min(10000.0, max(0.0, (cpu_delta / elapsed) * 100.0))
        rss_kb = _proc_value("VmRSS")
        thread_count = _proc_value("Threads")
        return {
            "pid": os.getpid(),
            "cpu_percent": round(cpu_percent, 3),
            "cpu_time_seconds": round(cpu_time, 6),
            "rss_bytes": int(rss_kb * 1024) if rss_kb is not None else None,
            "thread_count": int(thread_count) if thread_count is not None else threading.active_count(),
        }

    @staticmethod
    def _database_snapshot(path: Path) -> dict[str, Any]:
        result: dict[str, Any] = {
            "path": str(path),
            "status": "NOT_FOUND" if not path.exists() else "UNKNOWN",
            "probe_latency_ms": None,
            "journal_mode": "",
            "db_bytes": int(path.stat().st_size) if path.exists() else 0,
            "wal_bytes": int(path.with_name(path.name + "-wal").stat().st_size)
            if path.with_name(path.name + "-wal").exists() else 0,
        }
        if not path.exists():
            return result
        started = time.perf_counter()
        conn = None
        try:
            uri = f"file:{path}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=0.5)
            conn.execute("PRAGMA busy_timeout=500")
            conn.execute("SELECT 1").fetchone()
            journal = conn.execute("PRAGMA journal_mode").fetchone()
            result["journal_mode"] = str(journal[0] if journal else "")
            result["status"] = "PASS"
        except Exception as exc:
            result["status"] = "FAIL"
            result["error"] = f"{type(exc).__name__}: {str(exc)[:180]}"
        finally:
            if conn is not None:
                conn.close()
            result["probe_latency_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
        return result

    def snapshot(self, db_path: str | Path | None = None, *, db_sample_seconds: float = 5.0) -> dict[str, Any]:
        process = self._process_snapshot()
        path = Path(db_path) if db_path else None
        now = time.monotonic()
        if path is not None:
            with self._lock:
                should_sample = now - self._last_db_sample >= max(0.5, float(db_sample_seconds))
            if should_sample:
                sampled = self._database_snapshot(path)
                with self._lock:
                    self._last_db_snapshot = sampled
                    self._last_db_sample = now
        with self._lock:
            counters = dict(self._counters)
            errors = dict(self._last_errors)
            database = dict(self._last_db_snapshot)
            database.update({
                "commit_count": int(counters.get("db_commit_success", 0)),
                "commit_failures": int(counters.get("db_commit_failure", 0)),
                "last_commit_latency_ms": self._last_commit_latency_ms,
                "max_commit_latency_ms": self._max_commit_latency_ms,
            })
            execution = {
                "broker_request_count": int(counters.get("broker_request_success", 0)),
                "broker_request_failures": int(counters.get("broker_request_failure", 0)),
                "last_latency_ms": self._last_broker_latency_ms,
                "max_latency_ms": self._max_broker_latency_ms,
            }
        return {
            "captured_at": _utc_now(),
            "process": process,
            "database": database,
            "execution": execution,
            "counters": counters,
            "last_errors": errors,
        }


GLOBAL_OPERATIONAL_METRICS = OperationalMetrics()


class InstrumentedSQLiteConnection(sqlite3.Connection):
    """SQLite connection that records commit latency without changing semantics."""

    def commit(self) -> None:
        started = time.perf_counter()
        try:
            result = super().commit()
        except Exception as exc:
            GLOBAL_OPERATIONAL_METRICS.record_db_commit(
                False, (time.perf_counter() - started) * 1000.0, f"{type(exc).__name__}: {exc}"
            )
            raise
        GLOBAL_OPERATIONAL_METRICS.record_db_commit(
            True, (time.perf_counter() - started) * 1000.0
        )
        return result
