"""Standalone evidence store for the clean build.

It is deliberately a new database contract. It does not open or migrate any
legacy database and accepts its path only through dependency injection.
"""

from __future__ import annotations

import base64
import json
import sqlite3
import zlib
from hashlib import sha256
from dataclasses import asdict, is_dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Iterable, Mapping

from .contracts import EventMarketSnapshot, MarketSnapshot, MarketState, TradeDecision, TradeOutcome
from .execution import BotInstruction, ClaimResult, ExecutionReceipt
from .hfm_data import SourceDataset
from .intelligence.model import IntelligenceReport
from .market import RawTick, exact_tick_change, exact_tick_direction, exact_tick_mid
from .runtime_trace import RuntimeTraceEvent


class EvidenceStore:
    def __init__(self, path: str | Path):
        self._path = str(path)
        self._conn = sqlite3.connect(self._path)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._conn.execute("PRAGMA journal_size_limit = 67108864")
        self._create_schema()

    def _create_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS raw_ticks (
                symbol TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                bid REAL NOT NULL,
                ask REAL NOT NULL,
                spread REAL,
                mid REAL,
                price_change REAL,
                interarrival_seconds REAL,
                tick_velocity REAL,
                direction TEXT,
                event_type TEXT,
                event_id TEXT,
                PRIMARY KEY (symbol, timestamp, bid, ask)
            );
            CREATE TABLE IF NOT EXISTS tick_quality_events (
                event_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                status TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS scheduler_checkpoints (
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                event_id TEXT NOT NULL,
                PRIMARY KEY (symbol, timeframe)
            );
            CREATE TABLE IF NOT EXISTS evaluation_attempts (
                event_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                last_attempt_at TEXT NOT NULL,
                status TEXT NOT NULL,
                error_signature TEXT,
                attempts INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS market_snapshots (
                symbol TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY (symbol, observed_at)
            );
            CREATE TABLE IF NOT EXISTS trade_decisions (
                decision_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                action TEXT NOT NULL,
                created_at TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS trade_outcomes (
                decision_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                payload TEXT NOT NULL,
                FOREIGN KEY (decision_id) REFERENCES trade_decisions(decision_id)
            );
            CREATE TABLE IF NOT EXISTS audit_events (
                event_id INTEGER PRIMARY KEY,
                event_type TEXT NOT NULL,
                event_at TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chart_evidence (
                chart_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS feature_records (
                state_id TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                version INTEGER NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY (state_id, timeframe, version)
            );
            CREATE TABLE IF NOT EXISTS state_outcomes (
                outcome_id TEXT PRIMARY KEY,
                state_id TEXT NOT NULL,
                outcome_kind TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS experiment_registry (
                experiment_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS research_hypotheses (
                experiment_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS research_assessments (
                assessment_id TEXT PRIMARY KEY,
                experiment_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                status TEXT NOT NULL,
                validation_id TEXT,
                payload TEXT NOT NULL,
                FOREIGN KEY (experiment_id) REFERENCES research_hypotheses(experiment_id)
            );
            CREATE INDEX IF NOT EXISTS idx_research_assessments_experiment
                ON research_assessments(experiment_id, run_id);
            CREATE TABLE IF NOT EXISTS candidate_universes (
                universe_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                discovery_end TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS strategy_graveyard (
                graveyard_id TEXT PRIMARY KEY,
                experiment_id TEXT NOT NULL,
                reason TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS execution_handoffs (
                decision_id TEXT PRIMARY KEY,
                request_hash TEXT NOT NULL,
                instruction TEXT NOT NULL,
                state TEXT NOT NULL,
                receipt TEXT
            );
            CREATE TABLE IF NOT EXISTS datasets (
                dataset_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                start_at TEXT NOT NULL,
                end_at TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS dataset_rejections (
                rejection_id TEXT PRIMARY KEY,
                dataset_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS dataset_quality_reports (
                report_id TEXT PRIMARY KEY,
                dataset_id TEXT NOT NULL,
                status TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS market_states (
                state_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                snapshot TEXT NOT NULL,
                intelligence TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_market_states_symbol_time
                ON market_states(symbol, observed_at);
            CREATE TABLE IF NOT EXISTS event_market_snapshots (
                snapshot_id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL,
                phase TEXT NOT NULL,
                symbol TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                state_id TEXT NOT NULL,
                decision_id TEXT,
                source_ids TEXT NOT NULL,
                canonical_state TEXT NOT NULL,
                UNIQUE(event_id, phase)
            );
            CREATE INDEX IF NOT EXISTS idx_event_market_snapshots_event
                ON event_market_snapshots(event_id, captured_at);
            CREATE TABLE IF NOT EXISTS lineage_events (
                event_id TEXT PRIMARY KEY,
                state_id TEXT NOT NULL,
                decision_id TEXT,
                outcome_id TEXT,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS edge_registry (
                edge_key TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS validation_reports (
                validation_id TEXT PRIMARY KEY,
                edge_key TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS broker_events (
                event_id TEXT PRIMARY KEY,
                decision_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                event_at TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS position_events (
                event_id TEXT PRIMARY KEY,
                position_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                event_at TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS learning_events (
                event_id TEXT PRIMARY KEY,
                edge_key TEXT NOT NULL,
                event_at TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS liquidity_failure_events (
                failure_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                event_time TEXT NOT NULL,
                outcome TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_liquidity_failure_symbol_time
                ON liquidity_failure_events(symbol, timeframe, event_time);
            CREATE TABLE IF NOT EXISTS release_artifacts (
                artifact_id TEXT PRIMARY KEY,
                artifact_type TEXT NOT NULL,
                digest TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS execution_cost_samples (
                sample_id TEXT PRIMARY KEY,
                decision_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_execution_cost_symbol_time
                ON execution_cost_samples(symbol, observed_at);
            CREATE TABLE IF NOT EXISTS execution_cost_profiles (
                profile_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                status TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS slippage_observations (
                observation_id TEXT PRIMARY KEY,
                decision_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                order_ticket TEXT NOT NULL,
                deal_ticket TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                payload TEXT NOT NULL,
                UNIQUE(order_ticket, deal_ticket)
            );
            CREATE INDEX IF NOT EXISTS idx_slippage_symbol_time
                ON slippage_observations(symbol, observed_at);
            CREATE TABLE IF NOT EXISTS execution_latency_observations (
                observation_id TEXT PRIMARY KEY,
                decision_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                order_ticket TEXT NOT NULL,
                deal_ticket TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                payload TEXT NOT NULL,
                UNIQUE(order_ticket, deal_ticket)
            );
            CREATE INDEX IF NOT EXISTS idx_execution_latency_symbol_time
                ON execution_latency_observations(symbol, observed_at);
            CREATE TABLE IF NOT EXISTS shadow_positions (
                shadow_id TEXT PRIMARY KEY,
                decision_id TEXT NOT NULL,
                edge_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                status TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS shadow_marks (
                mark_id TEXT PRIMARY KEY,
                shadow_id TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                payload TEXT NOT NULL,
                FOREIGN KEY (shadow_id) REFERENCES shadow_positions(shadow_id)
            );
            CREATE TABLE IF NOT EXISTS shadow_outcomes (
                shadow_id TEXT PRIMARY KEY,
                edge_id TEXT NOT NULL,
                status TEXT NOT NULL,
                payload TEXT NOT NULL,
                FOREIGN KEY (shadow_id) REFERENCES shadow_positions(shadow_id)
            );
            CREATE TABLE IF NOT EXISTS hfm_deals (
                deal_id INTEGER PRIMARY KEY,
                position_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                utc_time TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS trade_lifecycles (
                lifecycle_id TEXT PRIMARY KEY,
                position_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                status TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS history_reconciliations (
                report_id TEXT PRIMARY KEY,
                source_digest TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS broker_trade_feedback (
                feedback_id TEXT PRIMARY KEY,
                lifecycle_id TEXT NOT NULL UNIQUE,
                decision_id TEXT,
                status TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS broker_feedback_reports (
                report_id TEXT PRIMARY KEY,
                history_report_id TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS historical_states (
                state_id TEXT PRIMARY KEY,
                dataset_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                vector TEXT NOT NULL,
                labels TEXT NOT NULL,
                source_ids TEXT NOT NULL,
                feature_version INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_historical_states_symbol_time
                ON historical_states(symbol, observed_at);
            CREATE TABLE IF NOT EXISTS historical_paths (
                state_id TEXT PRIMARY KEY,
                fidelity TEXT NOT NULL,
                payload TEXT NOT NULL,
                FOREIGN KEY (state_id) REFERENCES historical_states(state_id)
            );
            CREATE TABLE IF NOT EXISTS runtime_trace_events (
                trace_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                stage TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                artifact_id TEXT,
                payload TEXT NOT NULL,
                PRIMARY KEY (trace_id, sequence)
            );
            CREATE INDEX IF NOT EXISTS idx_runtime_trace_stage
                ON runtime_trace_events(stage, observed_at);
            """
        )
        historical_columns = {
            row[1] for row in self._conn.execute("PRAGMA table_info(historical_states)")
        }
        if "feature_version" not in historical_columns:
            self._conn.execute(
                "ALTER TABLE historical_states ADD COLUMN feature_version INTEGER NOT NULL DEFAULT 1"
            )
        tick_columns = {
            row[1] for row in self._conn.execute("PRAGMA table_info(raw_ticks)")
        }
        for name, sql_type in (
            ("spread", "REAL"),
            ("mid", "REAL"),
            ("price_change", "REAL"),
            ("interarrival_seconds", "REAL"),
            ("tick_velocity", "REAL"),
            ("direction", "TEXT"),
            ("event_type", "TEXT"),
            ("event_id", "TEXT"),
        ):
            if name not in tick_columns:
                self._conn.execute(f"ALTER TABLE raw_ticks ADD COLUMN {name} {sql_type}")
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_tick_event_id "
            "ON raw_ticks(event_id) WHERE event_id IS NOT NULL"
        )
        self._conn.commit()

    def write_tick(self, tick: RawTick) -> None:
        self.write_ticks((tick,))

    def write_ticks(self, ticks: tuple[RawTick, ...]) -> int:
        inserted = 0
        previous_by_symbol: dict[str, RawTick | None] = {}
        for tick in sorted(ticks, key=lambda item: (item.symbol, item.timestamp, item.bid, item.ask)):
            previous = previous_by_symbol.get(tick.symbol)
            if tick.symbol not in previous_by_symbol:
                previous = self.latest_tick(tick.symbol)
            values = self._tick_values(tick, previous)
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO raw_ticks (
                    symbol,timestamp,bid,ask,spread,mid,price_change,
                    interarrival_seconds,tick_velocity,direction,event_type,event_id
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                values,
            )
            if cursor.rowcount == 1:
                inserted += 1
                previous_by_symbol[tick.symbol] = tick
        self._conn.commit()
        return inserted

    @staticmethod
    def _tick_values(tick: RawTick, previous: RawTick | None) -> tuple[object, ...]:
        mid = exact_tick_mid(tick)
        spread = tick.spread
        if previous is None:
            price_change = 0.0
            interarrival = 0.0
            velocity = 0.0
            direction = "FIRST"
        else:
            price_change = exact_tick_change(previous, tick)
            interarrival = (tick.timestamp - previous.timestamp).total_seconds()
            velocity = price_change / interarrival if interarrival > 0 else 0.0
            direction = exact_tick_direction(previous, tick)
        identity = json.dumps(
            (tick.symbol, tick.timestamp.isoformat(), tick.bid, tick.ask),
            separators=(",", ":"),
        ).encode("utf-8")
        return (
            tick.symbol,
            tick.timestamp.isoformat(),
            tick.bid,
            tick.ask,
            spread,
            mid,
            price_change,
            interarrival,
            velocity,
            direction,
            "QUOTE",
            sha256(identity).hexdigest(),
        )

    def latest_tick(self, symbol: str) -> RawTick | None:
        row = self._conn.execute(
            "SELECT timestamp,bid,ask FROM raw_ticks WHERE symbol=? ORDER BY timestamp DESC LIMIT 1",
            (symbol,),
        ).fetchone()
        if row is None:
            return None
        return RawTick(symbol, datetime.fromisoformat(row[0]), row[1], row[2])

    def load_ticks(
        self,
        symbol: str,
        *,
        start_at: datetime,
        end_at: datetime,
        limit: int = 2048,
    ) -> tuple[RawTick, ...]:
        """Load a bounded chronological quote window for one symbol."""

        if limit <= 0:
            raise ValueError("tick limit must be positive")
        rows = self._conn.execute(
            """
            SELECT timestamp,bid,ask
            FROM (
                SELECT timestamp,bid,ask
                FROM raw_ticks
                WHERE symbol=? AND timestamp>=? AND timestamp<=?
                ORDER BY timestamp DESC,bid DESC,ask DESC
                LIMIT ?
            )
            ORDER BY timestamp,bid,ask
            """,
            (symbol, start_at.isoformat(), end_at.isoformat(), limit),
        ).fetchall()
        return tuple(
            RawTick(symbol, datetime.fromisoformat(timestamp), bid, ask)
            for timestamp, bid, ask in rows
        )

    def load_tick_observations(
        self,
        symbol: str,
        *,
        start_at: datetime,
        end_at: datetime,
        limit: int = 2048,
    ) -> tuple[Mapping[str, object], ...]:
        if limit <= 0:
            raise ValueError("tick limit must be positive")
        rows = self._conn.execute(
            """
            SELECT timestamp,bid,ask,spread,mid,price_change,
                   interarrival_seconds,tick_velocity,direction,event_type,event_id
            FROM raw_ticks
            WHERE symbol=? AND timestamp>=? AND timestamp<=?
            ORDER BY timestamp,bid,ask
            LIMIT ?
            """,
            (symbol, start_at.isoformat(), end_at.isoformat(), limit),
        ).fetchall()
        names = (
            "timestamp", "bid", "ask", "spread", "mid", "price_change",
            "interarrival_seconds", "tick_velocity", "direction", "event_type", "event_id",
        )
        return tuple(dict(zip(names, row)) for row in rows)

    def write_tick_quality_event(
        self,
        event_id: str,
        symbol: str,
        observed_at: datetime,
        status: str,
        payload: object,
    ) -> None:
        self._write_immutable(
            "tick_quality_events",
            ("event_id", "symbol", "observed_at", "status", "payload"),
            (event_id, symbol, observed_at.isoformat(), status, _json(payload)),
            ("event_id",),
        )

    def latest_tick_quality_status(self, symbol: str) -> str | None:
        row = self._conn.execute(
            "SELECT status FROM tick_quality_events WHERE symbol=? ORDER BY observed_at DESC,event_id DESC LIMIT 1",
            (symbol,),
        ).fetchone()
        return None if row is None else str(row[0])

    def scheduler_checkpoints(self) -> dict[tuple[str, str], datetime]:
        return {
            (symbol, timeframe): datetime.fromisoformat(completed_at)
            for symbol, timeframe, completed_at in self._conn.execute(
                "SELECT symbol,timeframe,completed_at FROM scheduler_checkpoints"
            )
        }

    def write_scheduler_checkpoint(
        self,
        symbol: str,
        timeframe: str,
        completed_at: datetime,
        event_id: str,
    ) -> None:
        row = self._conn.execute(
            "SELECT completed_at,event_id FROM scheduler_checkpoints WHERE symbol=? AND timeframe=?",
            (symbol, timeframe),
        ).fetchone()
        if row is not None and datetime.fromisoformat(row[0]) >= completed_at:
            if row == (completed_at.isoformat(), event_id):
                return
            raise ValueError("scheduler checkpoint cannot move backward or conflict")
        self._conn.execute(
            "INSERT INTO scheduler_checkpoints(symbol,timeframe,completed_at,event_id) VALUES (?,?,?,?) ON CONFLICT(symbol,timeframe) DO UPDATE SET completed_at=excluded.completed_at,event_id=excluded.event_id",
            (symbol, timeframe, completed_at.isoformat(), event_id),
        )
        self._conn.commit()

    def evaluation_retry_due(
        self,
        event_id: str,
        *,
        observed_at: datetime,
        retry_after_seconds: float,
    ) -> bool:
        if retry_after_seconds <= 0:
            raise ValueError("retry interval must be positive")
        row = self._conn.execute(
            "SELECT last_attempt_at,status FROM evaluation_attempts WHERE event_id=?",
            (event_id,),
        ).fetchone()
        if row is None:
            return True
        if row[1] == "PASS":
            return False
        elapsed = (observed_at - datetime.fromisoformat(row[0])).total_seconds()
        return elapsed >= retry_after_seconds

    def record_evaluation_attempt(
        self,
        event_id: str,
        symbol: str,
        observed_at: datetime,
        status: str,
        error_signature: str | None = None,
    ) -> bool:
        """Persist an attempt and report whether its error is new for the event."""

        row = self._conn.execute(
            "SELECT error_signature FROM evaluation_attempts WHERE event_id=?",
            (event_id,),
        ).fetchone()
        unique_error = error_signature is not None and (
            row is None or row[0] != error_signature
        )
        self._conn.execute(
            """
            INSERT INTO evaluation_attempts
                (event_id,symbol,last_attempt_at,status,error_signature,attempts)
            VALUES (?,?,?,?,?,1)
            ON CONFLICT(event_id) DO UPDATE SET
                last_attempt_at=excluded.last_attempt_at,
                status=excluded.status,
                error_signature=excluded.error_signature,
                attempts=evaluation_attempts.attempts+1
            """,
            (event_id, symbol, observed_at.isoformat(), status, error_signature),
        )
        self._conn.commit()
        return unique_error

    def write_snapshot(self, state: MarketState) -> None:
        payload = _json(state.payload)
        self._write_immutable(
            "market_snapshots",
            ("symbol", "observed_at", "payload"),
            (state.symbol, state.observed_at.isoformat(), payload),
            ("symbol", "observed_at"),
        )

    def write_decision(self, decision: TradeDecision) -> None:
        self._write_immutable(
            "trade_decisions",
            ("decision_id", "symbol", "action", "created_at", "payload"),
            (decision.decision_id, decision.symbol, decision.action, decision.created_at.isoformat(), _json(decision)),
            ("decision_id",),
        )

    def write_outcome(self, outcome: TradeOutcome) -> None:
        self._write_immutable(
            "trade_outcomes",
            ("decision_id", "status", "payload"),
            (outcome.decision_id, outcome.status, _json(outcome)),
            ("decision_id",),
        )

    def audit(self, event_type: str, event_at: datetime, payload: dict) -> None:
        self._conn.execute(
            "INSERT INTO audit_events(event_type, event_at, payload) VALUES (?, ?, ?)",
            (event_type, event_at.isoformat(), json.dumps(payload, sort_keys=True, default=str)),
        )
        self._conn.commit()

    def write_chart(self, chart_id: str, symbol: str, observed_at: datetime, payload: object) -> None:
        self._write_immutable(
            "chart_evidence",
            ("chart_id", "symbol", "observed_at", "payload"),
            (chart_id, symbol, observed_at.isoformat(), _packed_json(payload)),
            ("chart_id",),
        )

    def load_chart(self, chart_id: str) -> dict[str, object] | None:
        row = self._conn.execute(
            "SELECT chart_id,symbol,observed_at,payload FROM chart_evidence WHERE chart_id=?",
            (chart_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "chart_id": row[0],
            "symbol": row[1],
            "observed_at": row[2],
            "payload": _unpack_json(row[3]),
        }

    def load_chart_history(
        self,
        *,
        symbol: str | None = None,
        timeframe: str | None = None,
    ) -> tuple[dict[str, object], ...]:
        clauses: list[str] = []
        values: list[object] = []
        if symbol is not None:
            clauses.append("symbol=?")
            values.append(symbol)
        query = "SELECT chart_id,symbol,observed_at,payload FROM chart_evidence"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY observed_at,chart_id"
        records = []
        for chart_id, row_symbol, observed_at, payload in self._conn.execute(query, values):
            decoded = _unpack_json(payload)
            if timeframe is not None and decoded.get("timeframe") != timeframe:
                continue
            records.append(
                {
                    "chart_id": chart_id,
                    "symbol": row_symbol,
                    "observed_at": observed_at,
                    "payload": decoded,
                }
            )
        return tuple(records)

    def write_feature(self, state_id: str, timeframe: str, version: int, payload: object) -> None:
        self._write_immutable(
            "feature_records",
            ("state_id", "timeframe", "version", "payload"),
            (state_id, timeframe, version, _json(payload)),
            ("state_id", "timeframe", "version"),
        )

    def write_state_outcome(self, outcome_id: str, state_id: str, outcome_kind: str, payload: object) -> None:
        self._write_immutable(
            "state_outcomes",
            ("outcome_id", "state_id", "outcome_kind", "payload"),
            (outcome_id, state_id, outcome_kind, _json(payload)),
            ("outcome_id",),
        )

    def write_experiment(self, experiment_id: str, status: str, payload: object) -> None:
        self._write_immutable(
            "experiment_registry",
            ("experiment_id", "status", "payload"),
            (experiment_id, status, _json(payload)),
            ("experiment_id",),
        )

    def write_candidate_universe(self, universe: object) -> None:
        if not all(hasattr(universe, field) for field in ("universe_id", "symbol", "timeframe", "discovery_end")):
            raise TypeError("candidate universe has an invalid contract")
        self._write_immutable(
            "candidate_universes",
            ("universe_id", "symbol", "timeframe", "discovery_end", "payload"),
            (
                universe.universe_id,
                universe.symbol,
                universe.timeframe,
                universe.discovery_end.isoformat(),
                _json(universe),
            ),
            ("universe_id",),
        )

    def write_research_batch(self, rows: Iterable[Mapping[str, object]]) -> None:
        """Persist immutable hypotheses and run-specific assessments atomically."""

        records = tuple(rows)
        hypotheses = tuple(
            (row["experiment_id"], _json(row["spec"]))
            for row in records
        )
        validations = tuple(
            (row["validation_id"], row["experiment_id"], _json(row["validation"]))
            for row in records
            if row.get("validation_id") is not None
        )
        assessments = tuple(
            (
                row["assessment_id"], row["experiment_id"], row["run_id"],
                row["status"], row.get("validation_id"), _json(row["assessment"]),
            )
            for row in records
        )
        graveyard = tuple(
            (
                row["graveyard_id"], row["experiment_id"],
                row["graveyard_reason"], _json(row["graveyard"]),
            )
            for row in records
            if row.get("graveyard_id") is not None
        )
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            self._insert_many_immutable_tx(
                "research_hypotheses",
                ("experiment_id", "payload"),
                hypotheses,
                ("experiment_id",),
            )
            self._insert_many_immutable_tx(
                "validation_reports",
                ("validation_id", "edge_key", "payload"),
                validations,
                ("validation_id",),
            )
            self._insert_many_immutable_tx(
                "research_assessments",
                ("assessment_id", "experiment_id", "run_id", "status", "validation_id", "payload"),
                assessments,
                ("assessment_id",),
            )
            self._insert_many_immutable_tx(
                "strategy_graveyard",
                ("graveyard_id", "experiment_id", "reason", "payload"),
                graveyard,
                ("graveyard_id",),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def merge_research_database(
        self,
        source_path: str | Path,
        *,
        batch_size: int = 5_000,
    ) -> Mapping[str, int]:
        """Merge an independently evaluated research shard without mutation.

        The source is opened read-only. Existing identical rows make retries
        idempotent; an existing primary key with different content fails the
        merge instead of silently replacing evidence.
        """

        if batch_size <= 0:
            raise ValueError("batch size must be positive")
        source = Path(source_path).resolve()
        if source == Path(self._path).resolve():
            raise ValueError("research source and destination must differ")
        if not source.is_file():
            raise FileNotFoundError(source)
        connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        tables = (
            ("research_hypotheses", ("experiment_id", "payload"), ("experiment_id",)),
            ("validation_reports", ("validation_id", "edge_key", "payload"), ("validation_id",)),
            (
                "research_assessments",
                ("assessment_id", "experiment_id", "run_id", "status", "validation_id", "payload"),
                ("assessment_id",),
            ),
            (
                "strategy_graveyard",
                ("graveyard_id", "experiment_id", "reason", "payload"),
                ("graveyard_id",),
            ),
        )
        counts: dict[str, int] = {}
        try:
            if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise ValueError(f"research shard failed integrity check: {source}")
            for table, columns, keys in tables:
                cursor = connection.execute(
                    f"SELECT {','.join(columns)} FROM {table} ORDER BY {','.join(keys)}"
                )
                source_count = 0
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    while rows := cursor.fetchmany(batch_size):
                        selected = tuple(tuple(row) for row in rows)
                        source_count += len(selected)
                        self._insert_many_immutable_tx(table, columns, selected, keys)
                    self._conn.commit()
                except Exception:
                    self._conn.rollback()
                    raise
                counts[table] = source_count
        finally:
            connection.close()
        return counts

    def write_graveyard(self, graveyard_id: str, experiment_id: str, reason: str, payload: object) -> None:
        self._write_immutable(
            "strategy_graveyard",
            ("graveyard_id", "experiment_id", "reason", "payload"),
            (graveyard_id, experiment_id, reason, _json(payload)),
            ("graveyard_id",),
        )

    def write_dataset(self, dataset: SourceDataset) -> None:
        manifest = {
            "dataset_id": dataset.dataset_id,
            "symbol": dataset.symbol,
            "start": dataset.start.isoformat(),
            "end": dataset.end.isoformat(),
            "frame_counts": {frame: len(rows) for frame, rows in dataset.bars.items()},
            "frame_first": {
                frame: rows[0].start.isoformat() if rows else None
                for frame, rows in dataset.bars.items()
            },
            "frame_last_completed": {
                frame: rows[-1].end.isoformat() if rows else None
                for frame, rows in dataset.bars.items()
            },
            "source_hashes": dict(dataset.source_hashes),
            "tick_count": len(dataset.ticks),
            "first_tick": dataset.ticks[0].timestamp.isoformat() if dataset.ticks else None,
            "last_tick": dataset.ticks[-1].timestamp.isoformat() if dataset.ticks else None,
            "issues": dataset.issues,
            "terminal": dict(dataset.terminal),
            "account": dict(dataset.account),
            "contract": dict(dataset.contract),
            "revision": dataset.revision,
            "parent_dataset_id": dataset.parent_dataset_id,
            "storage_contract": "CONTENT_HASHED_SOURCE_MANIFEST_V1",
        }
        self._write_immutable(
            "datasets",
            ("dataset_id", "symbol", "start_at", "end_at", "payload"),
            (dataset.dataset_id, dataset.symbol, dataset.start.isoformat(), dataset.end.isoformat(), _json(manifest)),
            ("dataset_id",),
        )

    def write_dataset_rejection(self, rejection_id: str, dataset: SourceDataset, observed_at: datetime, payload: object) -> None:
        self._write_immutable(
            "dataset_rejections",
            ("rejection_id", "dataset_id", "symbol", "observed_at", "payload"),
            (rejection_id, dataset.dataset_id, dataset.symbol, observed_at.isoformat(), _json(payload)),
            ("rejection_id",),
        )

    def write_dataset_quality(self, report_id: str, dataset_id: str, status: str, observed_at: datetime, payload: object) -> None:
        self._write_immutable(
            "dataset_quality_reports",
            ("report_id", "dataset_id", "status", "observed_at", "payload"),
            (report_id, dataset_id, status, observed_at.isoformat(), _json(payload)),
            ("report_id",),
        )

    def write_market_state(
        self,
        snapshot: MarketSnapshot,
        report: IntelligenceReport,
        *,
        context_record_limit: int | None = None,
    ) -> None:
        if report.symbol != snapshot.symbol or report.observed_at != snapshot.observed_at.isoformat():
            raise ValueError("snapshot and intelligence identity mismatch")
        persisted_report = _persisted_intelligence_report(
            report,
            context_record_limit=context_record_limit,
        )
        self._write_immutable(
            "market_states",
            ("state_id", "symbol", "observed_at", "snapshot", "intelligence"),
            (
                report.state_id,
                snapshot.symbol,
                snapshot.observed_at.isoformat(),
                _packed_json(snapshot),
                _packed_json(persisted_report),
            ),
            ("state_id",),
        )

    def load_market_state(self, state_id: str) -> dict[str, object] | None:
        row = self._conn.execute(
            """
            SELECT state_id,symbol,observed_at,snapshot,intelligence
              FROM market_states WHERE state_id=?
            """,
            (state_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "state_id": row[0],
            "symbol": row[1],
            "observed_at": row[2],
            "snapshot": _unpack_json(row[3]),
            "intelligence": _unpack_json(row[4]),
        }

    def write_event_market_snapshot(self, snapshot: EventMarketSnapshot) -> None:
        self._write_immutable(
            "event_market_snapshots",
            (
                "snapshot_id", "event_id", "phase", "symbol", "observed_at",
                "captured_at", "state_id", "decision_id", "source_ids",
                "canonical_state",
            ),
            (
                snapshot.snapshot_id,
                snapshot.event_id,
                snapshot.phase,
                snapshot.symbol,
                snapshot.observed_at.isoformat(),
                snapshot.captured_at.isoformat(),
                snapshot.state_id,
                snapshot.decision_id,
                _json(snapshot.source_ids),
                snapshot.canonical_state,
            ),
            ("snapshot_id",),
        )

    def load_event_market_snapshots(self, event_id: str) -> tuple[EventMarketSnapshot, ...]:
        order = {"PRE_EVENT": 0, "ENTRY": 1, "POST_EVENT": 2}
        rows = self._conn.execute(
            """
            SELECT snapshot_id,event_id,phase,symbol,observed_at,captured_at,
                   state_id,decision_id,source_ids,canonical_state
            FROM event_market_snapshots WHERE event_id=?
            """,
            (event_id,),
        ).fetchall()
        snapshots = tuple(
            EventMarketSnapshot(
                snapshot_id=row[0],
                event_id=row[1],
                phase=row[2],
                symbol=row[3],
                observed_at=datetime.fromisoformat(row[4]),
                captured_at=datetime.fromisoformat(row[5]),
                state_id=row[6],
                decision_id=row[7],
                source_ids=tuple(json.loads(row[8])),
                canonical_state=row[9],
            )
            for row in rows
        )
        return tuple(sorted(snapshots, key=lambda item: order[item.phase]))

    def write_lineage(self, event_id: str, state_id: str, decision_id: str | None, outcome_id: str | None, payload: object) -> None:
        self._write_immutable(
            "lineage_events",
            ("event_id", "state_id", "decision_id", "outcome_id", "payload"),
            (event_id, state_id, decision_id, outcome_id, _json(payload)),
            ("event_id",),
        )

    def write_edge(self, edge_key: str, status: str, payload: object) -> None:
        self._write_immutable("edge_registry", ("edge_key", "status", "payload"), (edge_key, status, _json(payload)), ("edge_key",))

    def write_validation(self, validation_id: str, edge_key: str, payload: object) -> None:
        self._write_immutable("validation_reports", ("validation_id", "edge_key", "payload"), (validation_id, edge_key, _json(payload)), ("validation_id",))

    def write_broker_event(self, event_id: str, decision_id: str, event_type: str, event_at: datetime, payload: object) -> None:
        self._write_immutable("broker_events", ("event_id", "decision_id", "event_type", "event_at", "payload"), (event_id, decision_id, event_type, event_at.isoformat(), _json(payload)), ("event_id",))

    def write_position_event(self, event_id: str, position_id: str, event_type: str, event_at: datetime, payload: object) -> None:
        self._write_immutable("position_events", ("event_id", "position_id", "event_type", "event_at", "payload"), (event_id, position_id, event_type, event_at.isoformat(), _json(payload)), ("event_id",))

    def write_learning_event(self, event_id: str, edge_key: str, event_at: datetime, payload: object) -> None:
        self._write_immutable("learning_events", ("event_id", "edge_key", "event_at", "payload"), (event_id, edge_key, event_at.isoformat(), _json(payload)), ("event_id",))

    def write_liquidity_failures(self, records: Iterable[object]) -> int:
        from .liquidity_failures import LiquidityFailureRecord

        selected = tuple(records)
        if any(not isinstance(row, LiquidityFailureRecord) for row in selected):
            raise TypeError("liquidity failure store accepts LiquidityFailureRecord only")
        values = tuple(
            (
                row.failure_id,
                row.symbol,
                row.timeframe,
                row.event_time.isoformat(),
                row.outcome,
                _json(row),
            )
            for row in selected
        )
        before = self._conn.total_changes
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            self._insert_many_immutable_tx(
                "liquidity_failure_events",
                ("failure_id", "symbol", "timeframe", "event_time", "outcome", "payload"),
                values,
                ("failure_id",),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return self._conn.total_changes - before

    def load_liquidity_failures(self, symbol: str | None = None) -> tuple[object, ...]:
        from .liquidity_failures import LiquidityFailureRecord

        sql = "SELECT payload FROM liquidity_failure_events"
        params: tuple[object, ...] = ()
        if symbol is not None:
            sql += " WHERE symbol=?"
            params = (symbol,)
        sql += " ORDER BY event_time,failure_id"
        records = []
        for (payload,) in self._conn.execute(sql, params):
            raw = json.loads(payload)
            raw["event_time"] = datetime.fromisoformat(raw["event_time"])
            raw["observation_end_time"] = datetime.fromisoformat(raw["observation_end_time"])
            records.append(LiquidityFailureRecord(**raw))
        return tuple(records)

    def write_runtime_trace_event(self, event: RuntimeTraceEvent) -> None:
        self._write_immutable(
            "runtime_trace_events",
            ("trace_id", "sequence", "stage", "observed_at", "artifact_id", "payload"),
            (
                event.trace_id,
                event.sequence,
                event.stage,
                event.observed_at.isoformat(),
                event.artifact_id,
                _json(event.details),
            ),
            ("trace_id", "sequence"),
        )

    def runtime_trace(self, trace_id: str) -> tuple[RuntimeTraceEvent, ...]:
        rows = self._conn.execute(
            """
            SELECT sequence,stage,observed_at,artifact_id,payload
            FROM runtime_trace_events
            WHERE trace_id=?
            ORDER BY sequence
            """,
            (trace_id,),
        ).fetchall()
        return tuple(
            RuntimeTraceEvent(
                trace_id,
                int(sequence),
                str(stage),
                datetime.fromisoformat(observed_at),
                artifact_id,
                json.loads(payload),
            )
            for sequence, stage, observed_at, artifact_id, payload in rows
        )

    def write_release_artifact(self, artifact_id: str, artifact_type: str, digest: str, payload: object) -> None:
        self._write_immutable("release_artifacts", ("artifact_id", "artifact_type", "digest", "payload"), (artifact_id, artifact_type, digest, _json(payload)), ("artifact_id",))

    def write_cost_sample(self, sample: object) -> None:
        from .cost_evidence import ExecutionCostSample

        if not isinstance(sample, ExecutionCostSample):
            raise TypeError("cost sample must be ExecutionCostSample")
        self._write_immutable(
            "execution_cost_samples",
            ("sample_id", "decision_id", "symbol", "observed_at", "payload"),
            (sample.sample_id, sample.decision_id, sample.symbol, sample.observed_at.isoformat(), _json(sample)),
            ("sample_id",),
        )

    def write_cost_profile(self, profile: object) -> None:
        from .cost_evidence import CostProfile

        if not isinstance(profile, CostProfile):
            raise TypeError("cost profile must be CostProfile")
        self._write_immutable(
            "execution_cost_profiles",
            ("profile_id", "symbol", "status", "payload"),
            (profile.profile_id, profile.symbol, profile.status, _json(profile)),
            ("profile_id",),
        )

    def write_slippage_observation(self, observation: object) -> None:
        from .slippage import SlippageObservation

        if not isinstance(observation, SlippageObservation):
            raise TypeError("slippage observation must be SlippageObservation")
        self._write_immutable(
            "slippage_observations",
            (
                "observation_id",
                "decision_id",
                "symbol",
                "order_ticket",
                "deal_ticket",
                "observed_at",
                "payload",
            ),
            (
                observation.observation_id,
                observation.decision_id,
                observation.symbol,
                observation.order_ticket,
                observation.deal_ticket,
                observation.filled_at.isoformat(),
                _json(observation),
            ),
            ("observation_id",),
        )

    def write_execution_latency_observation(self, observation: object) -> None:
        from .intelligence.latency import ExecutionLatencyObservation

        if not isinstance(observation, ExecutionLatencyObservation):
            raise TypeError("latency observation must be ExecutionLatencyObservation")
        self._write_immutable(
            "execution_latency_observations",
            (
                "observation_id",
                "decision_id",
                "symbol",
                "order_ticket",
                "deal_ticket",
                "observed_at",
                "payload",
            ),
            (
                observation.observation_id,
                observation.decision_id,
                observation.symbol,
                observation.order_ticket,
                observation.deal_ticket,
                observation.fill_received_at.isoformat(),
                _json(observation),
            ),
            ("observation_id",),
        )

    def write_shadow_position(self, position: object) -> None:
        from .shadow import ShadowPosition

        if not isinstance(position, ShadowPosition):
            raise TypeError("shadow position must be ShadowPosition")
        self._write_immutable(
            "shadow_positions",
            ("shadow_id", "decision_id", "edge_id", "symbol", "status", "payload"),
            (
                position.shadow_id, position.decision_id, position.edge_id,
                position.symbol, position.status, _json(position),
            ),
            ("shadow_id",),
        )

    def write_shadow_mark(self, mark_id: str, shadow_id: str, observed_at: datetime, payload: object) -> None:
        self._write_immutable(
            "shadow_marks",
            ("mark_id", "shadow_id", "observed_at", "payload"),
            (mark_id, shadow_id, observed_at.isoformat(), _json(payload)),
            ("mark_id",),
        )

    def write_shadow_outcome(self, outcome: object) -> None:
        from .shadow import ShadowOutcome

        if not isinstance(outcome, ShadowOutcome):
            raise TypeError("shadow outcome must be ShadowOutcome")
        self._write_immutable(
            "shadow_outcomes",
            ("shadow_id", "edge_id", "status", "payload"),
            (outcome.shadow_id, outcome.edge_id, outcome.status, _json(outcome)),
            ("shadow_id",),
        )

    def shadow_mark(self, mark_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT payload FROM shadow_marks WHERE mark_id=?", (mark_id,),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def latest_shadow_mark(self, shadow_id: str) -> tuple[datetime, dict] | None:
        row = self._conn.execute(
            "SELECT observed_at,payload FROM shadow_marks WHERE shadow_id=? ORDER BY observed_at DESC,mark_id DESC LIMIT 1",
            (shadow_id,),
        ).fetchone()
        return (datetime.fromisoformat(row[0]), json.loads(row[1])) if row else None

    def load_active_shadow_positions(self, *, symbol: str | None = None) -> tuple[object, ...]:
        from .shadow import ShadowPosition

        sql = "SELECT shadow_id,payload FROM shadow_positions"
        params: tuple[object, ...] = ()
        if symbol is not None:
            sql += " WHERE symbol=?"
            params = (symbol,)
        sql += " ORDER BY shadow_id"
        rows = []
        for shadow_id, initial_payload in self._conn.execute(sql, params):
            if self._conn.execute("SELECT 1 FROM shadow_outcomes WHERE shadow_id=?", (shadow_id,)).fetchone():
                continue
            latest = self.latest_shadow_mark(shadow_id)
            raw = latest[1]["position"] if latest else json.loads(initial_payload)
            rows.append(_shadow_position(raw, ShadowPosition))
        return tuple(rows)

    def load_shadow_outcome(self, shadow_id: str) -> object | None:
        from .shadow import ShadowOutcome

        row = self._conn.execute(
            "SELECT payload FROM shadow_outcomes WHERE shadow_id=?", (shadow_id,),
        ).fetchone()
        if row is None:
            return None
        raw = json.loads(row[0])
        for field in ("entry_at", "closed_at"):
            raw[field] = datetime.fromisoformat(raw[field]) if raw.get(field) else None
        return ShadowOutcome(**raw)

    def load_shadow_outcomes(self) -> tuple[object, ...]:
        return tuple(
            outcome
            for (shadow_id,) in self._conn.execute("SELECT shadow_id FROM shadow_outcomes ORDER BY shadow_id")
            if (outcome := self.load_shadow_outcome(shadow_id)) is not None
        )

    def write_hfm_history(self, report: object, deals: Iterable[object], lifecycles: Iterable[object]) -> None:
        if not all(hasattr(report, field) for field in ("report_id", "source_digest")):
            raise TypeError("history report must be HistoryReconciliation")
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            for deal in deals:
                if not all(hasattr(deal, field) for field in ("deal_id", "position_id", "symbol", "utc_time")):
                    raise TypeError("deal must be HfmDeal")
                self._insert_immutable_tx(
                    "hfm_deals",
                    ("deal_id", "position_id", "symbol", "utc_time", "payload"),
                    (deal.deal_id, deal.position_id, deal.symbol, deal.utc_time.isoformat(), _json(deal)),
                    ("deal_id",),
                )
            for lifecycle in lifecycles:
                if not all(hasattr(lifecycle, field) for field in ("lifecycle_id", "position_id", "symbol", "status")):
                    raise TypeError("lifecycle must be TradeLifecycle")
                self._insert_immutable_tx(
                    "trade_lifecycles",
                    ("lifecycle_id", "position_id", "symbol", "status", "payload"),
                    (lifecycle.lifecycle_id, lifecycle.position_id, lifecycle.symbol, lifecycle.status, _json(lifecycle)),
                    ("lifecycle_id",),
                )
            self._insert_immutable_tx(
                "history_reconciliations",
                ("report_id", "source_digest", "payload"),
                (report.report_id, report.source_digest, _json(report)),
                ("report_id",),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def load_decisions(self) -> tuple[TradeDecision, ...]:
        rows = []
        for (payload,) in self._conn.execute("SELECT payload FROM trade_decisions ORDER BY created_at,decision_id"):
            raw = json.loads(payload)
            raw["created_at"] = datetime.fromisoformat(raw["created_at"])
            for field in ("evidence", "reason_codes"):
                raw[field] = tuple(raw.get(field, ()))
            if raw.get("entry_area") is not None:
                raw["entry_area"] = tuple(raw["entry_area"])
            rows.append(TradeDecision(**raw))
        return tuple(rows)

    def load_execution_receipts(self) -> tuple[ExecutionReceipt, ...]:
        rows = []
        for (payload,) in self._conn.execute(
            "SELECT receipt FROM execution_handoffs WHERE state='COMPLETED' AND receipt IS NOT NULL ORDER BY decision_id"
        ):
            raw = json.loads(payload)
            rows.append(ExecutionReceipt(
                raw["decision_id"], raw["status"], raw.get("broker_reference"), raw.get("details", {}),
            ))
        return tuple(rows)

    def load_hfm_deals(self) -> tuple[object, ...]:
        from .hfm_history import HfmDeal

        rows = []
        for (payload,) in self._conn.execute("SELECT payload FROM hfm_deals ORDER BY utc_time,deal_id"):
            raw = json.loads(payload)
            raw["broker_time"] = datetime.fromisoformat(raw["broker_time"])
            raw["utc_time"] = datetime.fromisoformat(raw["utc_time"])
            rows.append(HfmDeal(**raw))
        return tuple(rows)

    def load_trade_lifecycles(self) -> tuple[object, ...]:
        from .hfm_history import TradeLifecycle

        rows = []
        for (payload,) in self._conn.execute("SELECT payload FROM trade_lifecycles ORDER BY position_id,lifecycle_id"):
            raw = json.loads(payload)
            raw["opened_at"] = datetime.fromisoformat(raw["opened_at"]) if raw.get("opened_at") else None
            raw["closed_at"] = datetime.fromisoformat(raw["closed_at"]) if raw.get("closed_at") else None
            for field in ("deal_ids", "source_ids", "reason_codes"):
                raw[field] = tuple(raw.get(field, ()))
            rows.append(TradeLifecycle(**raw))
        return tuple(rows)

    def write_broker_feedback(self, report: object, feedback: Iterable[object]) -> None:
        if not all(hasattr(report, field) for field in ("report_id", "history_report_id")):
            raise TypeError("feedback report must be BrokerFeedbackReport")
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            for row in feedback:
                if not all(hasattr(row, field) for field in ("feedback_id", "lifecycle_id", "status")):
                    raise TypeError("feedback row must be BrokerTradeFeedback")
                self._insert_immutable_tx(
                    "broker_trade_feedback",
                    ("feedback_id", "lifecycle_id", "decision_id", "status", "payload"),
                    (row.feedback_id, row.lifecycle_id, row.decision_id, row.status, _json(row)),
                    ("feedback_id",),
                )
            self._insert_immutable_tx(
                "broker_feedback_reports",
                ("report_id", "history_report_id", "payload"),
                (report.report_id, report.history_report_id, _json(report)),
                ("report_id",),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def write_historical_cases(self, cases: Iterable[tuple[object, object]]) -> int:
        from .historical_memory import HistoricalPath, HistoricalState

        before = self._conn.total_changes
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            for state, path in cases:
                if not isinstance(state, HistoricalState) or not isinstance(path, HistoricalPath):
                    raise TypeError("historical case requires HistoricalState and HistoricalPath")
                self._insert_immutable_tx(
                    "historical_states",
                    ("state_id", "dataset_id", "symbol", "observed_at", "vector", "labels", "source_ids", "feature_version"),
                    (state.state_id, state.dataset_id, state.symbol, state.observed_at.isoformat(), _json(state.vector), _json(state.labels), _json(state.source_ids), state.feature_schema_version),
                    ("state_id",),
                )
                self._insert_immutable_tx(
                    "historical_paths",
                    ("state_id", "fidelity", "payload"),
                    (path.state_id, path.fidelity, _json(path)),
                    ("state_id",),
                )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return self._conn.total_changes - before

    def load_historical_cases(
        self,
        symbol: str,
        limit: int | None = None,
        *,
        feature_version: int | None = None,
    ) -> tuple[tuple[object, object], ...]:
        from .historical_memory import FEATURE_SCHEMA_VERSION, HistoricalPath, HistoricalState, HorizonOutcome

        selected_version = FEATURE_SCHEMA_VERSION if feature_version is None else feature_version
        sql = "SELECT s.state_id,s.dataset_id,s.symbol,s.observed_at,s.vector,s.labels,s.source_ids,s.feature_version,p.payload FROM historical_states s LEFT JOIN historical_paths p ON p.state_id=s.state_id WHERE s.symbol=? AND s.feature_version=? ORDER BY s.observed_at"
        params: tuple[object, ...] = (symbol, selected_version)
        if limit is not None:
            sql += " LIMIT ?"
            params += (limit,)
        result = []
        for state_id, dataset_id, row_symbol, observed_at, vector, labels, source_ids, feature_version, path_payload in self._conn.execute(sql, params):
            state = HistoricalState(state_id, dataset_id, row_symbol, datetime.fromisoformat(observed_at), tuple(json.loads(vector)), tuple(json.loads(labels)), tuple(json.loads(source_ids)), feature_version)
            path = None
            if path_payload:
                raw = json.loads(path_payload)
                path = HistoricalPath(state_id, tuple(HorizonOutcome(**item) for item in raw["outcomes"]), raw["fidelity"])
            result.append((state, path))
        return tuple(result)

    def _insert_immutable_tx(self, table: str, columns: tuple[str, ...], values: tuple[object, ...], keys: tuple[str, ...]) -> None:
        placeholders = ",".join("?" for _ in columns)
        try:
            self._conn.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})", values)
            return
        except sqlite3.IntegrityError:
            pass
        where = " AND ".join(f"{key} = ?" for key in keys)
        key_values = tuple(values[columns.index(key)] for key in keys)
        row = self._conn.execute(f"SELECT {','.join(columns)} FROM {table} WHERE {where}", key_values).fetchone()
        if row != values:
            raise ValueError(f"immutable record conflict in {table}: {key_values}")

    def _insert_many_immutable_tx(
        self,
        table: str,
        columns: tuple[str, ...],
        rows: tuple[tuple[object, ...], ...],
        keys: tuple[str, ...],
    ) -> None:
        if not rows:
            return
        placeholders = ",".join("?" for _ in columns)
        before = self._conn.total_changes
        self._conn.executemany(
            f"INSERT OR IGNORE INTO {table} ({','.join(columns)}) VALUES ({placeholders})",
            rows,
        )
        if self._conn.total_changes - before == len(rows):
            return
        where = " AND ".join(f"{key} = ?" for key in keys)
        for values in rows:
            key_values = tuple(values[columns.index(key)] for key in keys)
            observed = self._conn.execute(
                f"SELECT {','.join(columns)} FROM {table} WHERE {where}",
                key_values,
            ).fetchone()
            if observed != values:
                raise ValueError(f"immutable record conflict in {table}: {key_values}")

    def _write_immutable(self, table: str, columns: tuple[str, ...], values: tuple[object, ...], keys: tuple[str, ...]) -> None:
        placeholders = ",".join("?" for _ in columns)
        try:
            self._conn.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})", values)
            self._conn.commit()
            return
        except sqlite3.IntegrityError:
            self._conn.rollback()
        where = " AND ".join(f"{key} = ?" for key in keys)
        key_values = tuple(values[columns.index(key)] for key in keys)
        row = self._conn.execute(f"SELECT {','.join(columns)} FROM {table} WHERE {where}", key_values).fetchone()
        if row != values:
            raise ValueError(f"immutable record conflict in {table}: {key_values}")

    def claim_instruction(self, instruction: BotInstruction) -> ClaimResult:
        payload = json.dumps(asdict(instruction), sort_keys=True, default=str)
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute(
                "SELECT request_hash, state, receipt FROM execution_handoffs WHERE decision_id = ?",
                (instruction.decision_id,),
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO execution_handoffs VALUES (?, ?, ?, 'IN_PROGRESS', NULL)",
                    (instruction.decision_id, instruction.request_hash, payload),
                )
                self._conn.commit()
                return ClaimResult("NEW")
            request_hash, state, receipt_payload = row
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        if request_hash != instruction.request_hash:
            return ClaimResult("CONFLICT")
        if state == "IN_PROGRESS":
            return ClaimResult("IN_PROGRESS")
        if receipt_payload is None:
            raise RuntimeError("completed execution handoff has no receipt")
        raw = json.loads(receipt_payload)
        receipt = ExecutionReceipt(
            decision_id=raw["decision_id"],
            status=raw["status"],
            broker_reference=raw.get("broker_reference"),
            details=raw.get("details", {}),
        )
        return ClaimResult("COMPLETED", receipt)

    def complete_instruction(
        self,
        instruction: BotInstruction,
        receipt: ExecutionReceipt,
        slippage: tuple[object, ...],
    ) -> None:
        from .execution import _validate_slippage_completion
        from .slippage import SlippageObservation

        if not all(isinstance(item, SlippageObservation) for item in slippage):
            raise TypeError("slippage records must be SlippageObservation values")
        observations = tuple(slippage)
        _validate_slippage_completion(instruction, receipt, observations)
        payload = json.dumps(asdict(receipt), sort_keys=True, default=str)
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            cursor = self._conn.execute(
                """
                UPDATE execution_handoffs
                   SET state = 'COMPLETED', receipt = ?
                 WHERE decision_id = ? AND request_hash = ? AND state = 'IN_PROGRESS'
                """,
                (payload, instruction.decision_id, instruction.request_hash),
            )
            if cursor.rowcount != 1:
                raise ValueError("execution instruction was not claimed")
            for observation in observations:
                values = (
                    observation.observation_id,
                    observation.decision_id,
                    observation.symbol,
                    observation.order_ticket,
                    observation.deal_ticket,
                    observation.filled_at.isoformat(),
                    _json(observation),
                )
                try:
                    self._conn.execute(
                        "INSERT INTO slippage_observations VALUES (?, ?, ?, ?, ?, ?, ?)",
                        values,
                    )
                except sqlite3.IntegrityError:
                    row = self._conn.execute(
                        """
                        SELECT observation_id,decision_id,symbol,order_ticket,deal_ticket,observed_at,payload
                          FROM slippage_observations
                         WHERE observation_id=? OR (order_ticket=? AND deal_ticket=?)
                        """,
                        (
                            observation.observation_id,
                            observation.order_ticket,
                            observation.deal_ticket,
                        ),
                    ).fetchone()
                    if row != values:
                        raise ValueError("immutable slippage observation conflict")
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def load_slippage_observations(
        self,
        decision_id: str | None = None,
    ) -> tuple[object, ...]:
        from .slippage import SlippageObservation

        if decision_id is None:
            rows = self._conn.execute(
                "SELECT payload FROM slippage_observations ORDER BY observed_at,observation_id"
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT payload FROM slippage_observations
                 WHERE decision_id=? ORDER BY observed_at,observation_id
                """,
                (decision_id,),
            ).fetchall()
        return tuple(SlippageObservation.from_payload(json.loads(row[0])) for row in rows)

    def load_execution_latency_observations(
        self,
        decision_id: str | None = None,
    ) -> tuple[object, ...]:
        from .intelligence.latency import ExecutionLatencyObservation

        if decision_id is None:
            rows = self._conn.execute(
                """
                SELECT payload FROM execution_latency_observations
                 ORDER BY observed_at,observation_id
                """
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT payload FROM execution_latency_observations
                 WHERE decision_id=? ORDER BY observed_at,observation_id
                """,
                (decision_id,),
            ).fetchall()
        return tuple(
            ExecutionLatencyObservation.from_payload(json.loads(row[0]))
            for row in rows
        )

    def prune_read_only_live_cache(self, *, retain_after: datetime) -> dict[str, int]:
        """Prune only disposable evidence from a no-broker research cache.

        This is deliberately unavailable once the database contains a broker
        event, execution handoff, position event, or a non-NO_TRADE decision.
        Historical-memory and research-validation tables are never included.
        """

        cutoff = retain_after.isoformat()
        guarded_tables = (
            "broker_events",
            "execution_handoffs",
            "position_events",
            "hfm_deals",
            "trade_lifecycles",
            "broker_trade_feedback",
        )
        active = {
            table: self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in guarded_tables
        }
        non_research_decisions = self._conn.execute(
            "SELECT COUNT(*) FROM trade_decisions WHERE action != 'NO_TRADE'"
        ).fetchone()[0]
        if any(active.values()) or non_research_decisions:
            raise RuntimeError("read-only live cache retention refuses broker or executable evidence")

        tables = (
            "raw_ticks",
            "tick_quality_events",
            "evaluation_attempts",
            "market_snapshots",
            "market_states",
            "event_market_snapshots",
            "chart_evidence",
            "feature_records",
            "lineage_events",
            "runtime_trace_events",
            "audit_events",
            "dataset_quality_reports",
            "dataset_rejections",
            "datasets",
            "trade_outcomes",
            "trade_decisions",
        )
        before = {
            table: self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in tables
        }
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            stale_state_ids = tuple(
                row[0]
                for row in self._conn.execute(
                    "SELECT state_id FROM market_states WHERE observed_at < ?",
                    (cutoff,),
                )
            )
            stale_dataset_ids = tuple(
                row[0]
                for row in self._conn.execute(
                    "SELECT dataset_id FROM datasets WHERE end_at < ?",
                    (cutoff,),
                )
            )
            stale_decision_ids = tuple(
                row[0]
                for row in self._conn.execute(
                    "SELECT decision_id FROM trade_decisions WHERE action='NO_TRADE' AND created_at < ?",
                    (cutoff,),
                )
            )
            _delete_many(self._conn, "feature_records", "state_id", stale_state_ids)
            _delete_many(self._conn, "lineage_events", "state_id", stale_state_ids)
            _delete_many(self._conn, "event_market_snapshots", "state_id", stale_state_ids)
            _delete_many(self._conn, "market_states", "state_id", stale_state_ids)
            _delete_many(self._conn, "dataset_quality_reports", "dataset_id", stale_dataset_ids)
            _delete_many(self._conn, "dataset_rejections", "dataset_id", stale_dataset_ids)
            _delete_many(self._conn, "datasets", "dataset_id", stale_dataset_ids)
            _delete_many(self._conn, "trade_outcomes", "decision_id", stale_decision_ids)
            _delete_many(self._conn, "trade_decisions", "decision_id", stale_decision_ids)
            self._conn.execute("DELETE FROM raw_ticks WHERE timestamp < ?", (cutoff,))
            self._conn.execute("DELETE FROM tick_quality_events WHERE observed_at < ?", (cutoff,))
            self._conn.execute("DELETE FROM evaluation_attempts WHERE last_attempt_at < ?", (cutoff,))
            self._conn.execute("DELETE FROM market_snapshots WHERE observed_at < ?", (cutoff,))
            self._conn.execute("DELETE FROM chart_evidence WHERE observed_at < ?", (cutoff,))
            self._conn.execute("DELETE FROM runtime_trace_events WHERE observed_at < ?", (cutoff,))
            self._conn.execute("DELETE FROM audit_events WHERE event_at < ?", (cutoff,))
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return {
            table: before[table] - self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in tables
        }

    def checkpoint(self, *, truncate: bool = False) -> tuple[object, ...]:
        mode = "TRUNCATE" if truncate else "PASSIVE"
        return self._conn.execute(f"PRAGMA wal_checkpoint({mode})").fetchone()

    def integrity(self) -> bool:
        return self._conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    def close(self) -> None:
        self._conn.close()


def _json(value: object) -> str:
    return json.dumps(_primitive(value), sort_keys=True, separators=(",", ":"), default=str)


_PACKED_JSON_PREFIX = "ZLIB_JSON_V1:"


def _packed_json(value: object) -> str:
    """Persist JSON losslessly without repeated large evidence payloads."""

    raw = _json(value).encode("utf-8")
    compressed = zlib.compress(raw, level=9)
    return _PACKED_JSON_PREFIX + base64.b64encode(compressed).decode("ascii")


def _unpack_json(value: str) -> object:
    """Read packed payloads and the prior plain-JSON format."""

    if value.startswith(_PACKED_JSON_PREFIX):
        value = zlib.decompress(base64.b64decode(value[len(_PACKED_JSON_PREFIX) :])).decode("utf-8")
    return json.loads(value)


def _delete_many(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    values: tuple[str, ...],
) -> None:
    if not values:
        return
    placeholders = ",".join("?" for _ in values)
    connection.execute(f"DELETE FROM {table} WHERE {column} IN ({placeholders})", values)


def _persisted_intelligence_report(
    report: IntelligenceReport,
    *,
    context_record_limit: int | None,
) -> IntelligenceReport:
    """Bound only redundant historical context in a persisted live report.

    The intelligence object supplied to the decision provider is never
    changed.  This projection is made after analysis and contains a digest and
    full count for every bounded context, so evidence storage does not repeat
    the same historical pattern records for every completed bar.
    """

    if context_record_limit is None:
        return report
    if context_record_limit <= 0:
        raise ValueError("context_record_limit must be positive when supplied")

    pattern_records, pattern_summary = _bounded_context_records(
        report.pattern_outcome_context,
        context_record_limit,
    )
    failed_records, failed_summary = _bounded_context_records(
        report.failed_pattern_context,
        context_record_limit,
    )
    if pattern_summary is None and failed_summary is None:
        return report

    metadata = dict(report.metadata)
    metadata["persistence_projection"] = {
        "version": "CONTEXT_BOUNDED_V1",
        "pattern_outcome_context": pattern_summary,
        "failed_pattern_context": failed_summary,
    }
    return replace(
        report,
        pattern_outcome_context=pattern_records,
        failed_pattern_context=failed_records,
        metadata=metadata,
    )


def _bounded_context_records(
    records: Mapping[str, object],
    limit: int,
) -> tuple[dict[str, object], dict[str, object] | None]:
    ordered = tuple(records.items())
    if len(ordered) <= limit:
        return dict(ordered), None
    retained = dict(ordered[-limit:])
    record_ids = tuple(str(record_id) for record_id, _ in ordered)
    return retained, {
        "total_count": len(ordered),
        "retained_count": len(retained),
        "dropped_count": len(ordered) - len(retained),
        "record_id_digest": sha256("\x1f".join(record_ids).encode("utf-8")).hexdigest(),
    }


def _primitive(value: object) -> object:
    if is_dataclass(value):
        return {key: _primitive(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _primitive(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_primitive(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _shadow_position(raw: dict, cls):
    for field in ("created_at", "expires_at", "entry_at", "closed_at"):
        raw[field] = datetime.fromisoformat(raw[field]) if raw.get(field) else None
    if raw.get("entry_area") is not None:
        raw["entry_area"] = tuple(raw["entry_area"])
    return cls(**raw)
