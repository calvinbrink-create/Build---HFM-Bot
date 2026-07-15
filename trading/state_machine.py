"""Phase 2 lifecycle and concurrency primitives.

This module owns execution-state validation only. It does not decide whether a
market setup is valid and does not contain strategy or risk rules.
"""
from __future__ import annotations

import threading
import time
from enum import Enum
from typing import Callable


class InvalidTransition(ValueError):
    """Raised when an execution lifecycle transition is not permitted."""


class LifecycleState(str, Enum):
    IDLE = "IDLE"
    MARKET_DATA_READY = "MARKET_DATA_READY"
    BIAS_CALCULATION = "BIAS_CALCULATION"
    SETUP_DETECTED = "SETUP_DETECTED"
    CONFIRMATION_WAIT = "CONFIRMATION_WAIT"
    ENTRY_VALIDATION = "ENTRY_VALIDATION"
    ORDER_SUBMITTED = "ORDER_SUBMITTED"
    POSITION_OPEN = "POSITION_OPEN"
    POSITION_MANAGEMENT = "POSITION_MANAGEMENT"
    EXIT = "EXIT"
    COOLDOWN = "COOLDOWN"
    EXECUTED = "EXECUTED"
    EXPIRED = "EXPIRED"
    INVALIDATED = "INVALIDATED"
    REJECTED = "REJECTED"


_TERMINAL = {
    LifecycleState.EXECUTED,
    LifecycleState.EXPIRED,
    LifecycleState.INVALIDATED,
    LifecycleState.REJECTED,
}

_ALLOWED = {
    LifecycleState.IDLE: {
        LifecycleState.IDLE,
        LifecycleState.MARKET_DATA_READY,
    },
    LifecycleState.MARKET_DATA_READY: {
        LifecycleState.MARKET_DATA_READY,
        LifecycleState.BIAS_CALCULATION,
        LifecycleState.IDLE,
    },
    LifecycleState.BIAS_CALCULATION: {
        LifecycleState.BIAS_CALCULATION,
        LifecycleState.SETUP_DETECTED,
        LifecycleState.IDLE,
    },
    LifecycleState.SETUP_DETECTED: {
        LifecycleState.SETUP_DETECTED,
        LifecycleState.CONFIRMATION_WAIT,
        LifecycleState.ENTRY_VALIDATION,
        LifecycleState.EXPIRED,
        LifecycleState.INVALIDATED,
        LifecycleState.REJECTED,
    },
    LifecycleState.CONFIRMATION_WAIT: {
        LifecycleState.CONFIRMATION_WAIT,
        LifecycleState.ENTRY_VALIDATION,
        LifecycleState.EXPIRED,
        LifecycleState.INVALIDATED,
        LifecycleState.REJECTED,
    },
    LifecycleState.ENTRY_VALIDATION: {
        LifecycleState.ENTRY_VALIDATION,
        LifecycleState.ORDER_SUBMITTED,
        LifecycleState.EXPIRED,
        LifecycleState.INVALIDATED,
        LifecycleState.REJECTED,
    },
    LifecycleState.ORDER_SUBMITTED: {
        LifecycleState.ORDER_SUBMITTED,
        LifecycleState.POSITION_OPEN,
        LifecycleState.EXECUTED,
        LifecycleState.EXIT,
        LifecycleState.REJECTED,
        LifecycleState.INVALIDATED,
    },
    LifecycleState.POSITION_OPEN: {
        LifecycleState.POSITION_OPEN,
        LifecycleState.POSITION_MANAGEMENT,
        LifecycleState.EXIT,
        LifecycleState.COOLDOWN,
    },
    LifecycleState.POSITION_MANAGEMENT: {
        LifecycleState.POSITION_MANAGEMENT,
        LifecycleState.EXIT,
        LifecycleState.COOLDOWN,
    },
    LifecycleState.EXIT: {
        LifecycleState.EXIT,
        LifecycleState.COOLDOWN,
        LifecycleState.IDLE,
    },
    LifecycleState.COOLDOWN: {
        LifecycleState.COOLDOWN,
        LifecycleState.IDLE,
        LifecycleState.MARKET_DATA_READY,
    },
    LifecycleState.EXECUTED: set(),
    LifecycleState.EXPIRED: set(),
    LifecycleState.INVALIDATED: set(),
    LifecycleState.REJECTED: set(),
}


_ALIASES = {
    "": LifecycleState.IDLE,
    "NEW": LifecycleState.SETUP_DETECTED,
    "SETUP_CREATED": LifecycleState.SETUP_DETECTED,
    "WAITING": LifecycleState.CONFIRMATION_WAIT,
    "WAITING_M1": LifecycleState.CONFIRMATION_WAIT,
    "WAITING_M5": LifecycleState.CONFIRMATION_WAIT,
    "ARMED": LifecycleState.CONFIRMATION_WAIT,
    "PENDING": LifecycleState.CONFIRMATION_WAIT,
    "PENDING_SOFT": LifecycleState.CONFIRMATION_WAIT,
    "CONFIRMED": LifecycleState.ENTRY_VALIDATION,
    "CONFIRMED_M5": LifecycleState.ENTRY_VALIDATION,
    "CONFIRMED_1M": LifecycleState.ENTRY_VALIDATION,
    "EXECUTION_PENDING": LifecycleState.ORDER_SUBMITTED,
    "ORDER_SENT": LifecycleState.ORDER_SUBMITTED,
    "POSITION": LifecycleState.POSITION_OPEN,
    "MANAGING": LifecycleState.POSITION_MANAGEMENT,
    "CANCELLED": LifecycleState.INVALIDATED,
    "EXPIRED": LifecycleState.EXPIRED,
    "INVALIDATED": LifecycleState.INVALIDATED,
    "REJECTED": LifecycleState.REJECTED,
    "ORDER_REJECTED": LifecycleState.REJECTED,
    "ORDER_UNCONFIRMED": LifecycleState.REJECTED,
    "EXECUTED": LifecycleState.EXECUTED,
}


def normalize_state(value: str | LifecycleState | None) -> LifecycleState:
    if isinstance(value, LifecycleState):
        return value
    raw = str(value or "").strip().upper()
    if raw in _ALIASES:
        return _ALIASES[raw]
    try:
        return LifecycleState(raw)
    except ValueError as exc:
        raise InvalidTransition(f"unknown lifecycle state: {value!r}") from exc


def transition_state(current: str | LifecycleState | None, requested: str | LifecycleState) -> LifecycleState:
    current_state = normalize_state(current)
    requested_state = normalize_state(requested)
    if requested_state not in _ALLOWED[current_state]:
        raise InvalidTransition(
            f"{current_state.value} -> {requested_state.value} is not allowed"
        )
    return requested_state


def is_terminal(value: str | LifecycleState | None) -> bool:
    return normalize_state(value) in _TERMINAL


class TimedRLock:
    """RLock wrapper that reports wait and hold durations without holding a broker lock."""

    def __init__(
        self,
        name: str,
        timeout_seconds: float = 2.0,
        observer: Callable[[dict], None] | None = None,
        slow_hold_seconds: float = 0.25,
    ):
        self.name = str(name)
        self.timeout_seconds = max(0.01, float(timeout_seconds))
        self.slow_hold_seconds = max(0.01, float(slow_hold_seconds))
        self._lock = threading.RLock()
        self._observer = observer
        self._local = threading.local()

    def _emit(self, payload: dict) -> None:
        if self._observer is None:
            return
        try:
            self._observer({"lock": self.name, **payload})
        except Exception:
            pass

    def __enter__(self):
        started = time.monotonic()
        acquired = self._lock.acquire(timeout=self.timeout_seconds)
        waited = time.monotonic() - started
        if not acquired:
            self._emit({
                "event": "LOCK_TIMEOUT",
                "wait_ms": round(waited * 1000.0, 3),
                "timeout_ms": round(self.timeout_seconds * 1000.0, 3),
            })
            raise TimeoutError(f"lock timeout: {self.name}")
        depth = int(getattr(self._local, "depth", 0) or 0) + 1
        self._local.depth = depth
        if depth == 1:
            self._local.held_started = time.monotonic()
        if waited >= 0.05:
            self._emit({"event": "LOCK_WAIT", "wait_ms": round(waited * 1000.0, 3)})
        return self

    def __exit__(self, exc_type, exc, tb):
        depth = int(getattr(self._local, "depth", 1) or 1)
        if depth == 1:
            held = time.monotonic() - float(getattr(self._local, "held_started", time.monotonic()))
            if held >= self.slow_hold_seconds:
                self._emit({"event": "LOCK_HOLD_SLOW", "hold_ms": round(held * 1000.0, 3)})
            self._local.depth = 0
        else:
            self._local.depth = depth - 1
        self._lock.release()
        return False
