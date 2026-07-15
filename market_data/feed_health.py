"""Factual WebSocket tick freshness and recovery state."""
from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from typing import Iterable


STALE_MARKET_DATA = "STALE_MARKET_DATA"
LIVE_DATA = "LIVE_DATA"
WEBSOCKET_DISCONNECTED = "WEBSOCKET_DISCONNECTED"


@dataclass
class _TickRecord:
    last_tick_epoch: float = 0.0
    last_received_epoch: float = 0.0
    state: str = STALE_MARKET_DATA
    last_transition_epoch: float = 0.0
    recovered_at: float = 0.0
    source: str = "websocket"


class FeedHealth:
    """Tracks real tick age independently from connection/heartbeat state."""

    def __init__(self, max_age_seconds: float = 5.0):
        self.max_age_seconds = max(1.0, float(max_age_seconds))
        self._records: dict[str, _TickRecord] = {}
        self._lock = threading.RLock()
        self._transport_running = False
        self._connected_clients = 0
        self._last_transport_change = 0.0

    def set_transport(self, running: bool, connected_clients: int, now: float | None = None) -> None:
        now = float(time.time() if now is None else now)
        with self._lock:
            self._transport_running = bool(running)
            self._connected_clients = max(0, int(connected_clients or 0))
            self._last_transport_change = now

    def _state_for(self, record: _TickRecord, now: float) -> tuple[str, float | None]:
        if record.last_tick_epoch <= 0.0:
            return STALE_MARKET_DATA, None
        age = max(0.0, now - record.last_tick_epoch)
        return (LIVE_DATA if age <= self.max_age_seconds else STALE_MARKET_DATA), age

    def update_tick(self, symbol: str, tick_epoch: float, received_epoch: float | None = None, source: str = "websocket") -> dict:
        canonical = str(symbol or "").strip().upper()
        tick_epoch = float(tick_epoch or 0.0)
        if not canonical or tick_epoch <= 0.0:
            return {"accepted": False, "changed": False, "state": STALE_MARKET_DATA}
        now = float(time.time() if received_epoch is None else received_epoch)
        with self._lock:
            record = self._records.setdefault(canonical, _TickRecord())
            previous = record.state
            record.last_tick_epoch = tick_epoch
            record.last_received_epoch = now
            record.source = str(source or "websocket")
            current, age = self._state_for(record, now)
            record.state = current
            changed = previous != current
            if changed:
                record.last_transition_epoch = now
                if current == LIVE_DATA:
                    record.recovered_at = now
            return {
                "accepted": True,
                "changed": changed,
                "previous_state": previous,
                "state": current,
                "symbol": canonical,
                "last_tick_epoch": tick_epoch,
                "last_received_epoch": now,
                "age_seconds": age,
            }

    def status(self, symbol: str, now: float | None = None) -> dict:
        canonical = str(symbol or "").strip().upper()
        now = float(time.time() if now is None else now)
        with self._lock:
            record = self._records.get(canonical)
            if record is None:
                return {
                    "symbol": canonical,
                    "state": STALE_MARKET_DATA,
                    "reason": "no_factual_websocket_tick_received",
                    "last_tick_epoch": 0.0,
                    "last_received_epoch": 0.0,
                    "age_seconds": None,
                    "max_age_seconds": self.max_age_seconds,
                    "source": "none",
                }
            state, age = self._state_for(record, now)
            if state != record.state:
                record.state = state
                record.last_transition_epoch = now
            source = str(record.source or "websocket")
            reason = f"fresh_{source}_tick" if state == LIVE_DATA else f"{source}_tick_age_exceeded"
            if record.last_tick_epoch <= 0.0:
                reason = "no_factual_websocket_tick_received"
            return {
                "symbol": canonical,
                "state": state,
                "reason": reason,
                "last_tick_epoch": record.last_tick_epoch,
                "last_received_epoch": record.last_received_epoch,
                "age_seconds": age,
                "max_age_seconds": self.max_age_seconds,
                "last_transition_epoch": record.last_transition_epoch,
                "recovered_at": record.recovered_at,
                "source": record.source,
            }

    def entry_allowed(self, symbol: str, now: float | None = None) -> tuple[bool, str]:
        row = self.status(symbol, now=now)
        if row["state"] != LIVE_DATA:
            age = row["age_seconds"]
            age_text = "unknown" if age is None else f"{age:.3f}s"
            return False, (
                f"{STALE_MARKET_DATA} symbol={row['symbol']} age={age_text} "
                f"max={self.max_age_seconds:.3f}s reason={row['reason']}"
            )
        return True, f"LIVE_DATA symbol={row['symbol']} age={float(row['age_seconds']):.3f}s"

    def snapshot(self, symbols: Iterable[str] | None = None, now: float | None = None) -> dict:
        now = float(time.time() if now is None else now)
        with self._lock:
            names = {str(s).strip().upper() for s in (symbols or ()) if str(s).strip()}
            names.update(self._records)
            rows = {name: self.status(name, now=now) for name in sorted(names)}
            live = [row for row in rows.values() if row["state"] == LIVE_DATA]
            latest = max((row.get("last_tick_epoch", 0.0) for row in rows.values()), default=0.0)
            latest_row = max(rows.values(), key=lambda row: row.get("last_tick_epoch", 0.0), default=None)
            if live:
                state = LIVE_DATA
                reason = "at_least_one_symbol_has_fresh_websocket_tick"
            elif self._transport_running and self._connected_clients > 0:
                state = STALE_MARKET_DATA
                reason = "websocket_connected_without_fresh_symbol_tick"
            else:
                state = STALE_MARKET_DATA
                reason = "websocket_transport_not_connected_or_no_factual_tick"
            return {
                "state": state,
                "reason": reason,
                "max_age_seconds": self.max_age_seconds,
                "transport_running": self._transport_running,
                "connected_clients": self._connected_clients,
                "symbols": rows,
                "fresh_symbols": sorted(row["symbol"] for row in live),
                "stale_symbols": sorted(name for name, row in rows.items() if row["state"] != LIVE_DATA),
                "last_tick_at_epoch": latest,
                "last_tick_symbol": (latest_row or {}).get("symbol", ""),
                "last_tick_source": (latest_row or {}).get("source", ""),
                "last_tick_age_seconds": (max(0.0, now - latest) if latest > 0.0 else None),
            }
