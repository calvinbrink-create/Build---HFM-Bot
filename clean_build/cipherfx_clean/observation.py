"""Restart-safe read-only polling of terminal-authored HFM tick exports."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import Mapping, Protocol

from .calendar import ObservedTradingCalendar, observed_session_profile
from .hfm_data import HfmCsvMarketDataAdapter, TickExport
from .market import TickQuality, validate_tick
from .store import EvidenceStore


class MarketCalendar(Protocol):
    def state(self, at: datetime) -> object: ...


@dataclass(frozen=True)
class TickPollResult:
    event_id: str
    symbol: str
    observed_at: datetime
    tick_at: datetime | None
    status: str
    reason: str
    stored: bool
    broker_utc_offset_seconds: int
    source_path: str | None


class CsvTickObserver:
    def __init__(
        self,
        *,
        adapter: HfmCsvMarketDataAdapter,
        store: EvidenceStore,
        calendars: Mapping[str, MarketCalendar],
        maximum_age: timedelta = timedelta(seconds=5),
    ):
        if maximum_age.total_seconds() <= 0:
            raise ValueError("positive tick age is required")
        self._adapter = adapter
        self._store = store
        self._calendars = dict(calendars)
        self._maximum_age = maximum_age

    def poll(self, symbol: str, *, observed_at: datetime) -> TickPollResult:
        if symbol not in self._calendars:
            raise KeyError(f"missing trading calendar for {symbol}")
        try:
            export = self._adapter.latest_tick_export(symbol)
        except FileNotFoundError as exc:
            return self._unavailable(
                symbol,
                observed_at=observed_at,
                status=TickQuality.MISSING,
                reason="TICK_EXPORT_MISSING",
                source_path=str(exc.filename or exc),
            )
        except (OSError, TypeError, ValueError) as exc:
            return self._unavailable(
                symbol,
                observed_at=observed_at,
                status=TickQuality.MALFORMED,
                reason=f"TICK_EXPORT_MALFORMED:{type(exc).__name__}",
                source_path=str(self._adapter.tick_export_path(symbol)),
            )
        previous = self._store.latest_tick(symbol)
        validation = validate_tick(
            export.tick,
            now=observed_at,
            max_age=self._maximum_age,
            previous=previous,
        )
        market = self._calendars[symbol].state(observed_at)
        status = validation.quality.value
        reason = validation.reason
        stored = False
        if validation.quality is TickQuality.STALE and market.status == "CLOSED":
            status = "MARKET_CLOSED"
            reason = market.reason
        elif validation.quality is TickQuality.VALID:
            stored = self._store.write_ticks((export.tick,)) == 1
            if not stored:
                status = "DUPLICATE"
                reason = "ALREADY_PERSISTED"
        payload = {
            "tick": asdict(export.tick),
            "broker_timestamp": export.broker_timestamp.isoformat(),
            "utc_timestamp": export.utc_timestamp.isoformat(),
            "broker_utc_offset_seconds": export.broker_utc_offset_seconds,
            "export_receipt_epoch": export.export_receipt_epoch,
            "source_path": export.source_path,
            "market_state": asdict(market),
            "quality": validation.quality.value,
            "quality_reason": validation.reason,
            "stored": stored,
        }
        feed_status = (
            "FRESH"
            if status in ("VALID", "DUPLICATE")
            else "CLOSED"
            if status == "MARKET_CLOSED"
            else "STALE"
        )
        event_id = _digest((symbol, observed_at.isoformat(), export, feed_status, status, reason))
        if self._store.latest_tick_quality_status(symbol) != feed_status:
            self._store.write_tick_quality_event(
                event_id,
                symbol,
                observed_at,
                feed_status,
                {**payload, "poll_status": status, "feed_status": feed_status},
            )
        return TickPollResult(
            event_id, symbol, observed_at, export.tick.timestamp, status, reason,
            stored, export.broker_utc_offset_seconds, export.source_path,
        )

    def _unavailable(
        self,
        symbol: str,
        *,
        observed_at: datetime,
        status: TickQuality,
        reason: str,
        source_path: str,
    ) -> TickPollResult:
        payload = {
            "quality": status.value,
            "quality_reason": reason,
            "source_path": source_path,
            "stored": False,
            "feed_status": "STALE",
            "poll_status": status.value,
        }
        event_id = _digest((symbol, observed_at.isoformat(), status.value, reason, source_path))
        if self._store.latest_tick_quality_status(symbol) != "STALE":
            self._store.write_tick_quality_event(
                event_id,
                symbol,
                observed_at,
                "STALE",
                payload,
            )
        return TickPollResult(
            event_id=event_id,
            symbol=symbol,
            observed_at=observed_at,
            tick_at=None,
            status=status.value,
            reason=reason,
            stored=False,
            broker_utc_offset_seconds=0,
            source_path=source_path,
        )


def _digest(value: object) -> str:
    return sha256(json.dumps(value, default=str, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--maximum-age-seconds", type=float, default=5.0)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    calendar_observed_at = datetime.now(timezone.utc)
    adapter = HfmCsvMarketDataAdapter(args.root)
    store = EvidenceStore(args.database)
    try:
        calendars = {
            symbol: ObservedTradingCalendar(
                observed_session_profile(
                    symbol,
                    adapter.read_bars(symbol, "M1", observed_at=calendar_observed_at),
                    timezone_name="UTC",
                )
            )
            for symbol in args.symbols
        }
        observer = CsvTickObserver(
            adapter=adapter,
            store=store,
            calendars=calendars,
            maximum_age=timedelta(seconds=args.maximum_age_seconds),
        )
        poll_started_at = datetime.now(timezone.utc)
        results = tuple(
            observer.poll(symbol, observed_at=datetime.now(timezone.utc))
            for symbol in args.symbols
        )
        completed_at = datetime.now(timezone.utc)
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(
                {
                    "calendar_observed_at": calendar_observed_at.isoformat(),
                    "poll_started_at": poll_started_at.isoformat(),
                    "completed_at": completed_at.isoformat(),
                    "results": [asdict(item) for item in results],
                },
                indent=2,
                sort_keys=True,
                default=str,
            ) + "\n",
            encoding="utf-8",
        )
        store.checkpoint()
        allowed = {"VALID", "DUPLICATE", "MARKET_CLOSED"}
        return 0 if all(item.status in allowed for item in results) else 2
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
