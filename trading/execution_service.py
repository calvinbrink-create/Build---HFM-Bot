"""Single execution boundary with request-level idempotency."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import threading
from typing import Any, Callable


class DuplicateExecutionError(RuntimeError):
    """A request is already in flight and must not reach the broker twice."""


@dataclass(frozen=True)
class ExecutionRequest:
    symbol: str
    side: str
    volume: float
    stop_loss: float
    take_profit: float
    comment: str = ""
    request_id: str = ""

    @property
    def idempotency_key(self) -> str:
        explicit = str(self.request_id or "").strip()
        if explicit:
            return explicit
        raw = "|".join(
            (
                str(self.symbol or "").upper(),
                str(self.side or "").upper(),
                f"{float(self.volume):.12g}",
                f"{float(self.stop_loss):.12g}",
                f"{float(self.take_profit):.12g}",
                str(self.comment or ""),
            )
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ExecutionService:
    """Delegate execution while preventing duplicate broker submissions."""

    def __init__(self, submit: Callable[[ExecutionRequest], Any], cache_size: int = 2048):
        self._submit = submit
        self._lock = threading.RLock()
        self._inflight: set[str] = set()
        self._completed: OrderedDict[str, Any] = OrderedDict()
        self._cache_size = max(1, int(cache_size))

    def submit(self, request: ExecutionRequest) -> Any:
        key = request.idempotency_key
        with self._lock:
            if key in self._completed:
                return self._completed[key]
            if key in self._inflight:
                raise DuplicateExecutionError(f"execution already in flight: {key}")
            self._inflight.add(key)
        try:
            result = self._submit(request)
        except Exception:
            with self._lock:
                self._inflight.discard(key)
            raise
        with self._lock:
            self._inflight.discard(key)
            self._completed[key] = result
            self._completed.move_to_end(key)
            while len(self._completed) > self._cache_size:
                self._completed.popitem(last=False)
        return result
