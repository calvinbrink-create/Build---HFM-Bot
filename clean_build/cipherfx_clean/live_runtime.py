"""Read-only HFM live collector for completed-bar intelligence evaluation.

The collector has no broker adapter. It observes terminal-authored exports,
builds the complete ten-frame snapshot, evaluates each configured completed
bar once, and persists a structured NO_TRADE decision until a separately
certified edge is explicitly supplied by release governance.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import time
from typing import Mapping, Sequence

from .calendar import ObservedTradingCalendar, TradingCalendar, observed_session_profile
from .data_quality import DataQualityPolicy
from .execution import ExecutionReceipt
from .hfm_data import HfmCsvMarketDataAdapter, with_tick_window
from .historical_memory import HistoricalAnalogueIndex
from .intelligence.cross_asset import contexts_at
from .intelligence.provider import ResearchRequest, StructuredResearchProvider
from .intelligence.spread_intelligence import (
    SpreadProfileBook,
    build_spread_profile_book,
    historical_spread_samples,
)
from .observation import CsvTickObserver
from .runtime import CleanRuntime, RuntimeConfiguration
from .scheduler import CompletionScheduler, EvaluationEvent
from .shadow_runtime import PersistentShadowRunner
from .snapshot import RESEARCH_TIMEFRAMES, snapshot_from_dataset
from .store import EvidenceStore


DEFAULT_SYMBOLS = ("XAUUSD", "UK100", "USA100", "USA500", "USA30")
DEFAULT_PERSISTED_CONTEXT_RECORD_LIMIT = 64


@dataclass(frozen=True)
class LiveSymbolResult:
    symbol: str
    tick_status: str
    tick_at: str
    evaluation_event_id: str | None
    decision_id: str | None
    outcome_status: str | None
    error: str | None


@dataclass(frozen=True)
class LiveCycleResult:
    observed_at: str
    symbols: tuple[LiveSymbolResult, ...]


class BrokerExecutionForbidden:
    """Tripwire proving the read-only collector cannot submit an order."""

    def submit(self, decision) -> ExecutionReceipt:
        raise RuntimeError(f"BROKER_EXECUTION_FORBIDDEN_IN_LIVE_RESEARCH:{decision.decision_id}")


class NoCertifiedEdgeResponder:
    """Produce a traceable decision without inventing an unvalidated edge."""

    def __call__(self, request: ResearchRequest) -> Mapping[str, object]:
        state_id = str(request.report.get("state_id") or "")
        decision_id = _digest(
            ("NO_CERTIFIED_EDGE", request.symbol, request.observed_at.isoformat(), state_id)
        )
        return {
            "decision_id": decision_id,
            "action": "NO_TRADE",
            "evidence": tuple(item for item in (state_id, *request.analogue_ids) if item),
            "reason_codes": ("NO_CERTIFIED_EDGE",),
            "setup_type": "RESEARCH_OBSERVATION_ONLY",
        }


class LiveResearchCollector:
    """Poll fresh HFM exports and process one decision per completed trigger bar."""

    def __init__(
        self,
        *,
        adapter: HfmCsvMarketDataAdapter,
        store: EvidenceStore,
        calendars: Mapping[str, TradingCalendar],
        memory: HistoricalAnalogueIndex,
        symbols: Sequence[str] = DEFAULT_SYMBOLS,
        trigger_timeframes: Sequence[str] = ("M5",),
        maximum_tick_age: timedelta = timedelta(seconds=15),
        spread_profiles: SpreadProfileBook | None = None,
        persisted_context_record_limit: int | None = DEFAULT_PERSISTED_CONTEXT_RECORD_LIMIT,
        cache_retention: timedelta | None = timedelta(hours=2),
        maintenance_interval: timedelta = timedelta(minutes=1),
    ):
        if not symbols or not trigger_timeframes:
            raise ValueError("symbols and trigger timeframes are required")
        if not set(trigger_timeframes).issubset(RESEARCH_TIMEFRAMES):
            raise ValueError("trigger timeframes must belong to the ten-frame snapshot")
        if set(symbols) != set(calendars):
            raise ValueError("one explicit calendar is required for every symbol")
        if persisted_context_record_limit is not None and persisted_context_record_limit <= 0:
            raise ValueError("persisted context record limit must be positive")
        if cache_retention is not None and cache_retention <= timedelta(0):
            raise ValueError("cache retention must be positive when supplied")
        if maintenance_interval <= timedelta(0):
            raise ValueError("maintenance interval must be positive")
        self._adapter = adapter
        self._store = store
        self._calendars = dict(calendars)
        self._symbols = tuple(symbols)
        self._trigger_timeframes = frozenset(trigger_timeframes)
        self._cache_retention = cache_retention
        self._maintenance_interval = maintenance_interval
        self._last_maintenance_at: datetime | None = None
        self._observer = CsvTickObserver(
            adapter=adapter,
            store=store,
            calendars=calendars,
            maximum_age=maximum_tick_age,
        )
        self._shadow = PersistentShadowRunner(store)
        profile_book = spread_profiles or _historical_spread_profile_book(
            adapter,
            self._symbols,
        )
        provider = StructuredResearchProvider(NoCertifiedEdgeResponder(), approved_edges={})
        self._runtimes = {
            symbol: CleanRuntime(
                store=store,
                provider=provider,
                execution=BrokerExecutionForbidden(),
                memory=memory,
                calendar=calendars[symbol],
                quality_policy=_quality_policy(maximum_tick_age),
                spread_profiles=profile_book,
                configuration=RuntimeConfiguration(
                    persisted_context_record_limit=persisted_context_record_limit,
                ),
            )
            for symbol in self._symbols
        }
    def poll_once(self, *, observed_at: datetime | None = None) -> LiveCycleResult:
        observed = (observed_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        correlation_frames = {
            symbol: self._adapter.read_bars(
                symbol,
                "M5",
                observed_at=observed,
                history_mode="rolling",
            )
            for symbol in self._symbols
        }
        cross_asset = contexts_at(correlation_frames, observed_at=observed)
        output: list[LiveSymbolResult] = []
        for symbol in self._symbols:
            output.append(self._poll_symbol(symbol, observed, cross_asset[symbol]))
        self._maintain_live_cache(observed)
        return LiveCycleResult(observed.isoformat(), tuple(output))

    def _maintain_live_cache(self, observed_at: datetime) -> None:
        if (
            self._last_maintenance_at is not None
            and observed_at - self._last_maintenance_at < self._maintenance_interval
        ):
            return
        if self._cache_retention is not None:
            self._store.prune_read_only_live_cache(
                retain_after=observed_at - self._cache_retention,
            )
        self._store.checkpoint()
        self._last_maintenance_at = observed_at

    def _poll_symbol(
        self,
        symbol: str,
        observed: datetime,
        cross_asset_context: Mapping[str, float],
    ) -> LiveSymbolResult:
        tick_result = self._observer.poll(symbol, observed_at=observed)
        if tick_result.status not in ("VALID", "DUPLICATE"):
            return LiveSymbolResult(
                symbol,
                tick_result.status,
                tick_result.tick_at.isoformat(),
                None,
                None,
                None,
                None if tick_result.status == "MARKET_CLOSED" else f"MARKET_DATA_{tick_result.status}",
            )
        if tick_result.stored:
            self._shadow.advance_tick(self._adapter.latest_tick(symbol))

        # The terminal can refresh a later symbol's tick export after the
        # cycle timestamp was captured.  Evaluate that symbol at the observed
        # tick time so its valid, newly persisted tick stays in the frozen
        # dataset instead of being discarded as a future tick.
        evaluation_observed = observed
        if tick_result.tick_at is not None and tick_result.tick_at > observed:
            evaluation_observed = tick_result.tick_at

        if not self._trigger_has_new_completed_bar(symbol, evaluation_observed):
            return LiveSymbolResult(
                symbol,
                tick_result.status,
                tick_result.tick_at.isoformat(),
                None,
                None,
                None,
                None,
            )

        dataset = self._adapter.dataset(
            symbol,
            observed_at=evaluation_observed,
            timeframes=RESEARCH_TIMEFRAMES,
            include_latest_tick=True,
            history_mode="rolling",
        )
        dataset = with_tick_window(
            dataset,
            self._store.load_ticks(
                symbol,
                start_at=evaluation_observed - timedelta(minutes=5),
                end_at=evaluation_observed,
                limit=2048,
            ),
        )
        snapshot = snapshot_from_dataset(
            dataset,
            observed_at=evaluation_observed,
            required_timeframes=RESEARCH_TIMEFRAMES,
            include_micro=False,
        )
        scheduler = CompletionScheduler(self._store.scheduler_checkpoints())
        events = scheduler.ready(snapshot)
        triggers = tuple(
            event for event in events if event.timeframe in self._trigger_timeframes
        )
        if not triggers:
            scheduler.persist(self._store, events)
            return LiveSymbolResult(
                symbol,
                tick_result.status,
                tick_result.tick_at.isoformat(),
                None,
                None,
                None,
                None,
            )

        trigger = max(triggers, key=lambda event: (event.completed_at, event.timeframe))
        if not self._store.evaluation_retry_due(
            trigger.event_id,
            observed_at=evaluation_observed,
            retry_after_seconds=5.0,
        ):
            return LiveSymbolResult(
                symbol,
                tick_result.status,
                tick_result.tick_at.isoformat(),
                trigger.event_id,
                None,
                None,
                "EVALUATION_RETRY_DEFERRED",
            )
        try:
            result = self._runtimes[symbol].process(
                dataset,
                observed_at=evaluation_observed,
                cross_asset_context=cross_asset_context,
            )
        except Exception as exc:
            signature = _digest((type(exc).__name__, str(exc)))
            unique_error = self._store.record_evaluation_attempt(
                trigger.event_id,
                symbol,
                evaluation_observed,
                "FAIL",
                signature,
            )
            if unique_error:
                self._store.audit(
                    "LIVE_EVALUATION_FAILED",
                    evaluation_observed,
                    {
                        "event_id": trigger.event_id,
                        "symbol": symbol,
                        "error_type": type(exc).__name__,
                        "error_signature": signature,
                        "reason": str(exc),
                    },
                )
            return LiveSymbolResult(
                symbol,
                tick_result.status,
                tick_result.tick_at.isoformat(),
                trigger.event_id,
                None,
                None,
                f"{type(exc).__name__}:{exc}",
            )
        self._store.record_evaluation_attempt(
            trigger.event_id,
            symbol,
            evaluation_observed,
            "PASS",
        )
        scheduler.persist(self._store, events)
        return LiveSymbolResult(
            symbol,
            tick_result.status,
            tick_result.tick_at.isoformat(),
            trigger.event_id,
            result.decision.decision_id,
            result.outcome.status,
            None,
        )

    def _trigger_has_new_completed_bar(
        self,
        symbol: str,
        observed_at: datetime,
    ) -> bool:
        """Avoid rebuilding every frame until a configured trigger bar is new."""

        checkpoints = self._store.scheduler_checkpoints()
        for timeframe in self._trigger_timeframes:
            rows = self._adapter.read_bars(
                symbol,
                timeframe,
                observed_at=observed_at,
                history_mode="rolling",
            )
            if not rows:
                # Let the complete quality path record a missing trigger frame.
                return True
            completed_at = rows[-1].end
            if completed_at > checkpoints.get((symbol, timeframe), datetime.min.replace(tzinfo=completed_at.tzinfo)):
                return True
        return False


def _historical_spread_profile_book(
    adapter: HfmCsvMarketDataAdapter,
    symbols: Sequence[str],
) -> SpreadProfileBook:
    observed_at = datetime.now(timezone.utc)
    contracts = {row.symbol: row for row in adapter.symbols()}
    samples = []
    for symbol in symbols:
        contract = contracts.get(symbol)
        if contract is None or contract.point <= 0:
            raise ValueError(f"missing broker point size for spread profile: {symbol}")
        rows = adapter.read_bars(
            symbol,
            "M1",
            observed_at=observed_at,
            history_mode="combined",
        )
        samples.extend(
            historical_spread_samples(
                symbol,
                rows,
                point_size=contract.point,
            )
        )
    return build_spread_profile_book(samples)


def load_historical_memory(
    store: EvidenceStore,
    symbols: Sequence[str] = DEFAULT_SYMBOLS,
) -> HistoricalAnalogueIndex:
    cases = tuple(
        case
        for symbol in symbols
        for case in store.load_historical_cases(symbol)
    )
    return HistoricalAnalogueIndex(
        (case[0] for case in cases),
        (case[1] for case in cases if case[1] is not None),
    )


def build_observed_calendars(
    adapter: HfmCsvMarketDataAdapter,
    symbols: Sequence[str],
    *,
    observed_at: datetime,
) -> Mapping[str, ObservedTradingCalendar]:
    return {
        symbol: ObservedTradingCalendar(
            observed_session_profile(
                symbol,
                adapter.read_bars(
                    symbol,
                    "M1",
                    observed_at=observed_at,
                    history_mode="combined",
                ),
                timezone_name="UTC",
            )
        )
        for symbol in symbols
    }
def _quality_policy(maximum_tick_age: timedelta) -> DataQualityPolicy:
    return DataQualityPolicy(
        RESEARCH_TIMEFRAMES,
        {timeframe: 20 for timeframe in RESEARCH_TIMEFRAMES},
        {
            "MN1": timedelta(days=62),
            "W1": timedelta(days=14),
            "D1": timedelta(days=4),
            "H4": timedelta(hours=12),
            "H1": timedelta(hours=3),
            "M30": timedelta(minutes=90),
            "M15": timedelta(minutes=45),
            "M5": timedelta(minutes=15),
            "M3": timedelta(minutes=9),
            "M1": timedelta(minutes=3),
        },
        maximum_tick_age,
    )


def _digest(value: object) -> str:
    return sha256(
        json.dumps(value, default=str, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument(
        "--memory-database",
        help="read-only historical-memory source; defaults to the live evidence database",
    )
    parser.add_argument("--output", required=True)
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--cycles", type=int, default=1, help="zero runs continuously")
    parser.add_argument("--maximum-tick-age-seconds", type=float, default=15.0)
    args = parser.parse_args()
    if args.poll_seconds <= 0 or args.cycles < 0 or args.maximum_tick_age_seconds <= 0:
        raise ValueError("poll interval, cycle count, and tick age must be valid")

    symbols = tuple(args.symbols)
    adapter = HfmCsvMarketDataAdapter(args.root)
    store = EvidenceStore(args.database)
    memory_store = store
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        if args.memory_database:
            memory_path = Path(args.memory_database)
            if not memory_path.is_file():
                raise FileNotFoundError(memory_path)
            if memory_path.resolve() != Path(args.database).resolve():
                memory_store = EvidenceStore(memory_path)
        startup_at = datetime.now(timezone.utc)
        calendars = build_observed_calendars(
            adapter,
            symbols,
            observed_at=startup_at,
        )
        collector = LiveResearchCollector(
            adapter=adapter,
            store=store,
            calendars=calendars,
            memory=load_historical_memory(memory_store, symbols),
            symbols=symbols,
            maximum_tick_age=timedelta(seconds=args.maximum_tick_age_seconds),
        )
        cycle = 0
        last = None
        while args.cycles == 0 or cycle < args.cycles:
            started = time.monotonic()
            last = collector.poll_once()
            payload = {
                "schema_version": 1,
                "mode": "READ_ONLY_RESEARCH_NO_BROKER_EXECUTION",
                "cycle": cycle + 1,
                "result": asdict(last),
            }
            temporary = output.with_suffix(output.suffix + ".tmp")
            temporary.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            temporary.replace(output)
            cycle += 1
            if args.cycles == 0 or cycle < args.cycles:
                time.sleep(max(0.0, args.poll_seconds - (time.monotonic() - started)))
        store.checkpoint()
        if last is None:
            return 2
        healthy = {"VALID", "DUPLICATE", "MARKET_CLOSED"}
        return 0 if all(row.tick_status in healthy and row.error is None for row in last.symbols) else 2
    finally:
        if memory_store is not store:
            memory_store.close()
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
