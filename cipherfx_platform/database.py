from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any

from .contracts import MarketSnapshot, TradeProposal, ExecutionResult


class _ManagedConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class DatabaseLayer:
    """Sole persistence boundary for proposals, execution, positions, and learning."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self):
        # sqlite3.Connection.__exit__ commits/rolls back but does not close.
        # Use a managed connection so each short operation releases its file
        # descriptors and WAL read handles.
        conn = sqlite3.connect(self.path, timeout=10.0, factory=_ManagedConnection)
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA journal_size_limit=262144000")
        conn.execute("PRAGMA wal_autocheckpoint=1000")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _initialize(self):
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA journal_size_limit=262144000")
            conn.execute("PRAGMA wal_autocheckpoint=1000")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS platform_events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_time TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    symbol TEXT,
                    proposal_id TEXT,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS platform_status(
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS premarket_plans(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    setup_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS premarket_plan_versions(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    setup_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS premarket_plan_events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    setup_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS platform_executions(
                    proposal_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    status TEXT NOT NULL,
                    volume REAL NOT NULL,
                    pnl REAL DEFAULT 0,
                    initial_risk REAL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    closed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS engine_performance(
                    engine TEXT NOT NULL,
                    trade_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    result_r REAL NOT NULL,
                    pnl REAL NOT NULL,
                    closed_at TEXT NOT NULL,
                    metrics_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY(engine, trade_id)
                );
                CREATE TABLE IF NOT EXISTS market_snapshots(
                    snapshot_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    asset_class TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    tick_timestamp TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS trade_proposals(
                    proposal_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    asset_class TEXT NOT NULL,
                    engine TEXT NOT NULL,
                    side TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    stop_loss REAL NOT NULL,
                    take_profit REAL NOT NULL,
                    score REAL NOT NULL,
                    confidence REAL NOT NULL,
                    probability REAL NOT NULL,
                    risk_amount REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    state TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS orders(
                    proposal_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    volume REAL NOT NULL,
                    status TEXT NOT NULL,
                    order_ticket INTEGER NOT NULL DEFAULT 0,
                    deal_ticket INTEGER NOT NULL DEFAULT 0,
                    submitted_at TEXT NOT NULL,
                    response_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS executions(
                    proposal_id TEXT PRIMARY KEY,
                    order_ticket INTEGER NOT NULL DEFAULT 0,
                    deal_ticket INTEGER NOT NULL DEFAULT 0,
                    position_ticket INTEGER NOT NULL DEFAULT 0,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    volume REAL NOT NULL,
                    status TEXT NOT NULL,
                    fill_price REAL NOT NULL DEFAULT 0,
                    initial_risk REAL NOT NULL DEFAULT 0,
                    submitted_at TEXT NOT NULL,
                    filled_at TEXT,
                    closed_at TEXT,
                    response_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS positions(
                    position_ticket INTEGER PRIMARY KEY,
                    proposal_id TEXT,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    volume REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    current_price REAL NOT NULL,
                    profit REAL NOT NULL,
                    opened_at TEXT,
                    updated_at TEXT NOT NULL,
                    state TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS trade_history(
                    trade_id TEXT PRIMARY KEY,
                    proposal_id TEXT,
                    symbol TEXT NOT NULL,
                    asset_class TEXT NOT NULL,
                    side TEXT NOT NULL,
                    realized_pnl REAL NOT NULL,
                    initial_risk REAL NOT NULL,
                    result_r REAL NOT NULL,
                    exit_reason TEXT NOT NULL,
                    closed_at TEXT NOT NULL,
                    metrics_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS learning_metrics(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    engine TEXT NOT NULL,
                    trade_id TEXT NOT NULL,
                    proposal_id TEXT,
                    symbol TEXT NOT NULL,
                    asset_class TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    metrics_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS position_management(
                    position_ticket INTEGER PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_platform_events_time ON platform_events(event_time);
                CREATE INDEX IF NOT EXISTS idx_platform_exec_created ON platform_executions(created_at);
                CREATE INDEX IF NOT EXISTS idx_proposals_state_expiry ON trade_proposals(state, expires_at);
                CREATE INDEX IF NOT EXISTS idx_exec_status ON executions(status, closed_at);
                CREATE INDEX IF NOT EXISTS idx_learning_engine_time ON learning_metrics(engine, created_at);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_premarket_plans_event ON premarket_plans(event_id);
                CREATE INDEX IF NOT EXISTS idx_premarket_plan_target ON premarket_plans(symbol, status, created_at);
                """
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(engine_performance)")}
            if "metrics_json" not in columns:
                conn.execute("ALTER TABLE engine_performance ADD COLUMN metrics_json TEXT NOT NULL DEFAULT '{}'")

    def checkpoint(self) -> dict[str, int]:
        deleted = self.retention_sweep()
        with self._lock, self._connect() as conn:
            row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        values = list(row or (0, 0, 0))
        result = {
            "busy": int(values[0] if len(values) > 0 else 0),
            "log_pages": int(values[1] if len(values) > 1 else 0),
            "checkpointed_pages": int(values[2] if len(values) > 2 else 0),
        }
        result.update({f"pruned_{table}": count for table, count in deleted.items()})
        return result

    def retention_sweep(self, days: int | None = None) -> dict[str, int]:
        """Bound the growth of high-frequency append-only telemetry tables.

        Only pure event/telemetry logs are pruned here - trade_history,
        learning_metrics, executions/orders/positions and proposals are
        permanent trading records and are never touched by this sweep.
        """
        if days is None:
            days = max(1, int(os.getenv("MT5_TELEMETRY_RETENTION_DAYS", "2")))
        else:
            days = max(1, int(days))
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        deleted: dict[str, int] = {}
        with self._lock, self._connect() as conn:
            for table, column in (
                ("platform_events", "event_time"),
                ("premarket_plan_versions", "created_at"),
                ("premarket_plan_events", "created_at"),
                ("market_snapshots", "captured_at"),
            ):
                cursor = conn.execute(f"DELETE FROM {table} WHERE {column} < ?", (cutoff,))
                deleted[table] = int(cursor.rowcount or 0)
        return deleted

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, default=str, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _dt(value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None

    def event(self, event_type: str, payload: dict[str, Any] | None = None, *, symbol="", proposal_id=""):
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO platform_events(event_time,event_type,symbol,proposal_id,payload_json) VALUES(?,?,?,?,?)",
                (self._now(), str(event_type), str(symbol), str(proposal_id), self._json(payload or {})),
            )

    def status(self, key: str, value: Any):
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO platform_status(key,value_json,updated_at) VALUES(?,?,?)
                ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at
                """,
                (str(key), self._json(value), self._now()),
            )

    def portfolio_proposal_states(self) -> dict[str, int]:
        """Read proposal lifecycle counts without changing proposal state."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT state, COUNT(*) FROM trade_proposals GROUP BY state"
            ).fetchall()
        return {str(state).upper(): int(count) for state, count in rows}

    def portfolio_closed_summaries(self) -> dict[str, dict[str, Any]]:
        """Read aggregate closed-trade facts for portfolio reporting only."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT engine, COUNT(*) AS closed_trades,
                       SUM(pnl) AS realized_pnl,
                       SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
                       SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) AS losses
                FROM engine_performance GROUP BY engine
                """
            ).fetchall()
            if not rows:
                rows = conn.execute(
                    """
                    SELECT asset_class, COUNT(*) AS closed_trades,
                           SUM(realized_pnl) AS realized_pnl,
                           SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) AS wins,
                           SUM(CASE WHEN realized_pnl < 0 THEN 1 ELSE 0 END) AS losses
                    FROM trade_history GROUP BY asset_class
                    """
                ).fetchall()
        return {
            str(engine).upper(): {
                "closed_trades": int(closed_trades or 0),
                "realized_pnl": round(float(realized_pnl or 0.0), 2),
                "wins": int(wins or 0),
                "losses": int(losses or 0),
            }
            for engine, closed_trades, realized_pnl, wins, losses in rows
        }

    def proposal_seen(self, proposal_id):
        """Return True only for terminal execution outcomes.

        A transient operational block must not make a still-valid immutable
        proposal look executed. Permanent broker failures and successful
        outcomes remain idempotent.
        """
        retryable = {
            "BROKER_DISCONNECTED",
            "TRADING_NOT_ALLOWED",
            "MAX_DAILY_TRADES",
            "MAX_OPEN_TRADES",
            "INVALID_TICK",
            "SPREAD_LIMIT",
            "INSUFFICIENT_MARGIN",
        }
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT p.status, COALESCE(o.response_json, '')
                FROM platform_executions AS p
                LEFT JOIN orders AS o ON o.proposal_id = p.proposal_id
                WHERE p.proposal_id=?
                LIMIT 1
                """,
                (str(proposal_id),),
            ).fetchone()
        if row is None:
            return False
        status, response_json = row
        if status in {"FILLED", "DRY_RUN", "CLOSED", "REJECTED", "DUPLICATE_SUPPRESSED"}:
            return True
        if status != "EXECUTION_BLOCKED":
            return True
        if not response_json:
            return False
        try:
            reason = str(json.loads(response_json).get("reason", ""))
        except (TypeError, ValueError):
            reason = ""
        return reason not in retryable

    def save_snapshot(self, snapshot_id: str, snapshot: MarketSnapshot) -> None:
        payload = {
            "captured_at": snapshot.captured_at.isoformat(),
            "freshness": snapshot.freshness,
            "tick": {
                "bid": snapshot.tick.bid,
                "ask": snapshot.tick.ask,
                "timestamp": snapshot.tick.timestamp.isoformat(),
                "received_at": snapshot.tick.received_at.isoformat(),
            },
            "frames": {
                timeframe: [
                    {
                        "timestamp": candle.timestamp.isoformat(),
                        "open": candle.open,
                        "high": candle.high,
                        "low": candle.low,
                        "close": candle.close,
                        "volume": candle.volume,
                    }
                    for candle in frame.candles[-100:]
                ]
                for timeframe, frame in snapshot.frames.items()
            },
        }
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO market_snapshots(
                    snapshot_id,symbol,asset_class,captured_at,tick_timestamp,payload_json
                ) VALUES(?,?,?,?,?,?)
                """,
                (
                    str(snapshot_id),
                    snapshot.symbol,
                    snapshot.asset_class,
                    snapshot.captured_at.isoformat(),
                    snapshot.tick.timestamp.isoformat(),
                    self._json(payload),
                ),
            )

    def save_proposal(self, proposal: TradeProposal) -> None:
        engine = str(proposal.context.get("engine", proposal.strategy_name))
        payload = {
            "volume_hint": proposal.volume_hint,
            "strategy_name": proposal.strategy_name,
            "score_components": proposal.score_components,
            "context": proposal.context,
            "reasoning": proposal.reasoning,
        }
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO trade_proposals(
                    proposal_id,symbol,asset_class,engine,side,entry_price,stop_loss,take_profit,
                    score,confidence,probability,risk_amount,created_at,expires_at,state,payload_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    proposal.proposal_id,
                    proposal.symbol,
                    proposal.asset_class,
                    engine,
                    proposal.side,
                    float(proposal.entry_price),
                    float(proposal.stop_loss),
                    float(proposal.take_profit),
                    float(proposal.score),
                    float(proposal.confidence),
                    float(proposal.probability),
                    float(proposal.risk_amount),
                    proposal.created_at.isoformat(),
                    proposal.expires_at.isoformat(),
                    "APPROVED",
                    self._json(payload),
                ),
            )

    def latest_snapshots(self, symbols: list[str] | tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        """Return the newest persisted ten-frame snapshot for each symbol."""
        with self._lock, self._connect() as conn:
            params: list[str] = []
            where = ""
            if symbols:
                clean = sorted({str(symbol or "").strip().upper() for symbol in symbols if str(symbol or "").strip()})
                if not clean:
                    return []
                placeholders = ",".join("?" for _ in clean)
                where = f"WHERE UPPER(symbol) IN ({placeholders})"
                params.extend(clean)
            rows = conn.execute(
                "SELECT m.symbol,m.asset_class,m.captured_at,m.tick_timestamp,m.payload_json "
                "FROM market_snapshots m "
                "JOIN (SELECT symbol,MAX(captured_at) AS captured_at "
                "      FROM market_snapshots " + where + " GROUP BY symbol) latest "
                "ON latest.symbol=m.symbol AND latest.captured_at=m.captured_at "
                "ORDER BY m.symbol",
                params,
            ).fetchall()
        return [
            {
                "symbol": row[0],
                "asset_class": row[1],
                "captured_at": row[2],
                "tick_timestamp": row[3],
                "payload_json": row[4],
            }
            for row in rows
        ]

    def save_premarket_plan(
        self,
        *,
        event_id: str,
        symbol: str,
        setup_id: str,
        status: str,
        reason: str,
        payload: dict[str, Any],
    ) -> None:
        """Persist a planning record only; it is never an executable proposal."""
        created_at = self._now()
        encoded = self._json(payload)
        values = (
            str(event_id),
            created_at,
            str(symbol),
            str(setup_id),
            str(status),
            str(reason),
            encoded,
        )
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO premarket_plans(event_id,created_at,symbol,setup_id,status,reason,payload_json) "
                "VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(event_id) DO UPDATE SET created_at=excluded.created_at, "
                "symbol=excluded.symbol, setup_id=excluded.setup_id, status=excluded.status, "
                "reason=excluded.reason, payload_json=excluded.payload_json",
                values,
            )
            conn.execute(
                "INSERT INTO premarket_plan_versions(event_id,created_at,symbol,setup_id,status,reason,payload_json) "
                "VALUES(?,?,?,?,?,?,?)",
                values,
            )
            conn.execute(
                "INSERT INTO premarket_plan_events(event_id,created_at,symbol,setup_id,status,reason,payload_json) "
                "VALUES(?,?,?,?,?,?,?)",
                values,
            )

    def get_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT proposal_id,symbol,asset_class,engine,side,entry_price,stop_loss,
                       take_profit,score,confidence,probability,risk_amount,created_at,
                       expires_at,state,payload_json
                FROM trade_proposals WHERE proposal_id=?
                """,
                (str(proposal_id),),
            ).fetchone()
        if not row:
            return None
        try:
            payload = json.loads(row[15] or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        keys = (
            "proposal_id", "symbol", "asset_class", "engine", "side", "entry_price",
            "stop_loss", "take_profit", "score", "confidence", "probability",
            "risk_amount", "created_at", "expires_at", "state", "payload",
        )
        result = dict(zip(keys, row))
        result["payload"] = payload
        return result

    def calibrated_probability(
        self, engine: str, side: str, score: float, model_version: str,
    ) -> dict[str, Any]:
        """Return a transparent, engine/version/score-bucket outcome estimate."""
        bucket = max(0, min(90, int(float(score) // 10) * 10))
        wins = losses = 0
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT result_r,metrics_json FROM engine_performance "
                "WHERE engine=? ORDER BY closed_at DESC",
                (str(engine),),
            ).fetchall()
        for result_r, raw_metrics in rows:
            try:
                metrics = json.loads(raw_metrics or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            if (
                metrics.get("model_version") != model_version
                or metrics.get("outcome_type") != "closed_trade"
                or not metrics.get("learning_eligible")
                or not metrics.get("result_r_valid")
                or str(metrics.get("side", side)).upper() != str(side).upper()
                or int(metrics.get("score_bucket", -1)) != bucket
            ):
                continue
            try:
                value = float(result_r)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(value) or abs(value) <= 1e-12:
                continue
            if value > 0:
                wins += 1
            else:
                losses += 1
        samples = wins + losses
        probability = (2.0 + wins) / (4.0 + samples) * 100.0
        return {
            "probability": round(probability, 2),
            "status": "CALIBRATED" if samples >= 30 else "PRIOR_ONLY",
            "sample_size": samples,
            "wins": wins,
            "losses": losses,
            "score_bucket": bucket,
            "model_version": model_version,
            "side": str(side).upper(),
        }

    def update_proposal_state(self, proposal_id: str, state: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE trade_proposals SET state=? WHERE proposal_id=?",
                (str(state), str(proposal_id)),
            )

    def save_execution_result(
        self,
        proposal: TradeProposal,
        result: ExecutionResult,
        *,
        response: dict[str, Any] | None = None,
    ) -> None:
        response_json = self._json(response or {"reason": result.reason})
        state = {
            "FILLED": "EXECUTED",
            "DRY_RUN": "EXECUTED",
            "EXECUTION_BLOCKED": "EXECUTION_BLOCKED",
            "REJECTED": "REJECTED",
            "DUPLICATE_SUPPRESSED": "REJECTED",
        }.get(result.status, result.status)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO platform_executions(
                    proposal_id,symbol,side,status,volume,pnl,initial_risk,created_at,closed_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(proposal_id) DO UPDATE SET
                    status=excluded.status,volume=excluded.volume,
                    initial_risk=excluded.initial_risk
                """,
                (
                    proposal.proposal_id,
                    result.symbol,
                    result.side,
                    result.status,
                    float(result.volume),
                    0.0,
                    float(result.initial_risk),
                    result.submitted_at.isoformat(),
                    None,
                ),
            )
            conn.execute(
                """
                INSERT INTO orders(
                    proposal_id,request_id,symbol,side,volume,status,order_ticket,deal_ticket,
                    submitted_at,response_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(proposal_id) DO UPDATE SET
                    status=excluded.status, order_ticket=excluded.order_ticket,
                    deal_ticket=excluded.deal_ticket, submitted_at=excluded.submitted_at,
                    response_json=excluded.response_json
                """,
                (
                    proposal.proposal_id,
                    proposal.proposal_id,
                    result.symbol,
                    result.side,
                    float(result.volume),
                    result.status,
                    int(result.order_ticket),
                    int(result.deal_ticket),
                    result.submitted_at.isoformat(),
                    response_json,
                ),
            )
            conn.execute(
                """
                INSERT INTO executions(
                    proposal_id,order_ticket,deal_ticket,position_ticket,symbol,side,volume,status,
                    fill_price,initial_risk,submitted_at,filled_at,response_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(proposal_id) DO UPDATE SET
                    order_ticket=excluded.order_ticket, deal_ticket=excluded.deal_ticket,
                    position_ticket=excluded.position_ticket, volume=excluded.volume,
                    status=excluded.status, fill_price=excluded.fill_price,
                    initial_risk=excluded.initial_risk, submitted_at=excluded.submitted_at,
                    filled_at=excluded.filled_at, response_json=excluded.response_json
                """,
                (
                    proposal.proposal_id,
                    int(result.order_ticket),
                    int(result.deal_ticket),
                    int(result.position_ticket),
                    result.symbol,
                    result.side,
                    float(result.volume),
                    result.status,
                    float(result.fill_price),
                    float(result.initial_risk),
                    result.submitted_at.isoformat(),
                    self._dt(result.filled_at),
                    response_json,
                ),
            )
            conn.execute(
                "UPDATE trade_proposals SET state=? WHERE proposal_id=?",
                (state, proposal.proposal_id),
            )

    def save_position(self, position, proposal_id: str | None = None) -> None:
        with self._lock, self._connect() as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(positions)")}
            if "position_ticket" in columns:
                conn.execute(
                    """
                    INSERT INTO positions(
                        position_ticket,proposal_id,symbol,side,volume,entry_price,current_price,
                        profit,opened_at,updated_at,state
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(position_ticket) DO UPDATE SET
                        proposal_id=COALESCE(excluded.proposal_id,positions.proposal_id),
                        current_price=excluded.current_price, profit=excluded.profit,
                        volume=excluded.volume, updated_at=excluded.updated_at, state=excluded.state
                    """,
                    (
                        int(position.ticket),
                        proposal_id,
                        str(position.symbol),
                        str(position.direction),
                        float(position.volume),
                        float(position.price_open),
                        float(position.price_current),
                        float(position.profit),
                        self._dt(position.time),
                        self._now(),
                        "OPEN",
                    ),
                )
                return
            # Preserve the pre-Phase-3 operational positions table used by the
            # existing dashboard while keeping the same current position data.
            conn.execute(
                """
                INSERT OR REPLACE INTO positions(
                    sym,market,direction,qty,entry,current,sl,tp,atr,unrealized,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    str(position.symbol),
                    str(position.symbol),
                    str(position.direction),
                    float(position.volume),
                    float(position.price_open),
                    float(position.price_current),
                    float(position.sl),
                    float(position.tp),
                    0.0,
                    float(position.profit),
                    self._now(),
                ),
            )

    def prune_closed_positions(self, open_tickets: set[int], open_symbols: set[str]) -> None:
        """Delete rows for positions the broker no longer reports as open.

        save_position() is a pure upsert with no corresponding delete, so
        without this the positions table only ever grows - closed positions
        stay visible forever. Handles both the current schema (keyed by
        position_ticket, safe with multiple simultaneous same-symbol
        positions under hedging) and the legacy pre-Phase-3 schema (keyed by
        symbol alone, one row per symbol) that this specific deployment's
        database still uses.
        """
        with self._lock, self._connect() as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(positions)")}
            if "position_ticket" in columns:
                if open_tickets:
                    placeholders = ",".join("?" for _ in open_tickets)
                    conn.execute(
                        f"DELETE FROM positions WHERE position_ticket NOT IN ({placeholders})",
                        tuple(open_tickets),
                    )
                else:
                    conn.execute("DELETE FROM positions")
                return
            if open_symbols:
                placeholders = ",".join("?" for _ in open_symbols)
                conn.execute(
                    f"DELETE FROM positions WHERE sym NOT IN ({placeholders})",
                    tuple(open_symbols),
                )
            else:
                conn.execute("DELETE FROM positions")

    def open_filled_executions(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT e.proposal_id,e.order_ticket,e.deal_ticket,e.position_ticket,
                       e.symbol,e.side,e.volume,e.fill_price,e.initial_risk,e.submitted_at,
                       e.filled_at,p.asset_class,p.engine,p.created_at
                FROM executions e JOIN trade_proposals p ON p.proposal_id=e.proposal_id
                WHERE e.status='FILLED' AND e.closed_at IS NULL
                ORDER BY e.filled_at
                """
            ).fetchall()
        keys = (
            "proposal_id", "order_ticket", "deal_ticket", "position_ticket", "symbol", "side",
            "volume", "fill_price", "initial_risk", "submitted_at", "filled_at", "asset_class",
            "engine", "created_at",
        )
        return [dict(zip(keys, row)) for row in rows]

    def update_execution_position_ticket(self, proposal_id: str, position_ticket: int) -> bool:
        ticket = int(position_ticket or 0)
        if ticket <= 0:
            return False
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                '''
                UPDATE executions
                SET position_ticket=?
                WHERE proposal_id=? AND status='FILLED'
                  AND (position_ticket IS NULL OR position_ticket=0)
                ''',
                (ticket, str(proposal_id)),
            )
            return cursor.rowcount > 0

    def mark_trade_closed(self, proposal_id: str, closed_at: datetime, pnl: float) -> None:
        stamp = closed_at.isoformat()
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE executions SET closed_at=?, status='CLOSED' WHERE proposal_id=?",
                (stamp, str(proposal_id)),
            )
            conn.execute(
                "UPDATE platform_executions SET status='CLOSED', pnl=?, closed_at=? WHERE proposal_id=?",
                (float(pnl), stamp, str(proposal_id)),
            )
            conn.execute(
                "UPDATE trade_proposals SET state='CLOSED' WHERE proposal_id=?",
                (str(proposal_id),),
            )

    def mark_position_closed(self, position_ticket: int, closed_at: datetime, pnl: float) -> None:
        with self._lock, self._connect() as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(positions)")}
            if "position_ticket" not in columns:
                return
            updates = []
            values = []
            if "state" in columns:
                updates.append("state=?")
                values.append("CLOSED")
            if "profit" in columns:
                updates.append("profit=?")
                values.append(float(pnl))
            if "updated_at" in columns:
                updates.append("updated_at=?")
                values.append(closed_at.isoformat())
            if not updates:
                return
            values.append(int(position_ticket))
            conn.execute(
                f"UPDATE positions SET {', '.join(updates)} WHERE position_ticket=?",
                tuple(values),
            )

    def record_trade_history(
        self,
        trade_id: str,
        proposal_id: str | None,
        symbol: str,
        asset_class: str,
        side: str,
        realized_pnl: float,
        initial_risk: float,
        result_r: float,
        exit_reason: str,
        closed_at: datetime,
        metrics: dict[str, Any],
    ) -> bool:
        # Preserve broker outcome fields; enrich older sparse rows with missing
        # proposal telemetry so later learning remains fully traceable.
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO trade_history("
                "trade_id,proposal_id,symbol,asset_class,side,realized_pnl,initial_risk,"
                "result_r,exit_reason,closed_at,metrics_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(trade_id), proposal_id, str(symbol), str(asset_class), str(side),
                    float(realized_pnl), float(initial_risk), float(result_r),
                    str(exit_reason), closed_at.isoformat(), self._json(metrics),
                ),
            )
            if cursor.rowcount == 1:
                return True

            existing = conn.execute(
                "SELECT proposal_id,metrics_json FROM trade_history WHERE trade_id=?",
                (str(trade_id),),
            ).fetchone()
            if existing is None:
                return False
            try:
                existing_metrics = json.loads(existing[1] or "{}")
            except (TypeError, json.JSONDecodeError):
                existing_metrics = {}
            if not isinstance(existing_metrics, dict):
                existing_metrics = {}

            trace_keys = (
                "model_version", "outcome_type", "learning_eligible",
                "result_r_valid", "proposal_trace_missing", "proposal_score",
                "proposal_confidence", "proposal_probability", "proposal_created_at",
                "proposal_expires_at", "score_bucket",
            )
            enriched = False
            for key in trace_keys:
                value = metrics.get(key) if isinstance(metrics, dict) else None
                if value is not None and existing_metrics.get(key) is None:
                    existing_metrics[key] = value
                    enriched = True
            proposal_value = proposal_id or existing[0]
            proposal_changed = bool(proposal_value and not existing[0])
            if enriched or proposal_changed:
                conn.execute(
                    "UPDATE trade_history SET proposal_id=COALESCE(proposal_id,?),metrics_json=? WHERE trade_id=?",
                    (proposal_value, self._json(existing_metrics), str(trade_id)),
                )
            return bool(enriched or proposal_changed)

    def record_learning_metric(
        self,
        engine: str,
        trade_id: str,
        symbol: str,
        asset_class: str,
        metrics: dict[str, Any],
        *,
        proposal_id: str | None = None,
    ) -> None:
        # Reconciliation can observe the same broker close more than once.
        # Replace the existing record instead of creating duplicate feedback.
        with self._lock, self._connect() as conn:
            values = (
                str(engine),
                str(trade_id),
                proposal_id,
                str(symbol),
                str(asset_class),
                self._now(),
                self._json(metrics),
            )
            existing = conn.execute(
                """
                SELECT id FROM learning_metrics
                WHERE engine=? AND trade_id=?
                ORDER BY id DESC LIMIT 1
                """,
                (str(engine), str(trade_id)),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE learning_metrics
                    SET proposal_id=?,symbol=?,asset_class=?,created_at=?,metrics_json=?
                    WHERE id=?
                    """,
                    (values[2], values[3], values[4], values[5], values[6], int(existing[0])),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO learning_metrics(
                        engine,trade_id,proposal_id,symbol,asset_class,created_at,metrics_json
                    ) VALUES(?,?,?,?,?,?,?)
                    """,
                    values,
                )

    def record_engine_outcome(
        self,
        engine: str,
        trade_id: str,
        symbol: str,
        result_r: float,
        pnl: float,
        closed_at: str | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO engine_performance(
                    engine,trade_id,symbol,result_r,pnl,closed_at,metrics_json
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    str(engine),
                    str(trade_id),
                    str(symbol),
                    float(result_r),
                    float(pnl),
                    closed_at or self._now(),
                    self._json(metrics or {}),
                ),
            )

    def engine_history(
        self, engine: str, limit: int = 100, *,
        model_version: str | None = None, learning_only: bool = False,
    ) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT trade_id,symbol,result_r,pnl,closed_at,metrics_json
                FROM engine_performance WHERE engine=?
                ORDER BY closed_at DESC
                """,
                (str(engine),),
            ).fetchall()
        selected = []
        for row in rows:
            try:
                metrics = json.loads(row[5] or "{}")
            except (TypeError, json.JSONDecodeError):
                metrics = {}
            if model_version is not None and metrics.get("model_version") != model_version:
                continue
            if learning_only and (
                metrics.get("outcome_type") != "closed_trade"
                or not metrics.get("learning_eligible")
                or not metrics.get("result_r_valid")
            ):
                continue
            selected.append({
                "trade_id": row[0],
                "symbol": row[1],
                "result_r": float(row[2]),
                "pnl": float(row[3]),
                "closed_at": row[4],
                "metrics": metrics,
            })
            if len(selected) >= max(1, int(limit)):
                break
        return selected

    def position_baseline(self, position_ticket: int) -> dict[str, Any] | None:
        """Return immutable entry geometry for an executed position."""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT e.proposal_id,e.initial_risk,p.entry_price,p.stop_loss,p.take_profit,
                       p.asset_class,p.side
                FROM executions e
                JOIN trade_proposals p ON p.proposal_id=e.proposal_id
                WHERE e.position_ticket=? AND e.status IN ('FILLED','CLOSED')
                LIMIT 1
                """,
                (int(position_ticket),),
            ).fetchone()
        if not row:
            return None
        keys = (
            "proposal_id", "initial_risk", "entry_price", "stop_loss",
            "take_profit", "asset_class", "side",
        )
        return dict(zip(keys, row))

    def load_management_state(self, position_ticket: int) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT state_json FROM position_management WHERE position_ticket=?",
                (int(position_ticket),),
            ).fetchone()
        if not row:
            return None
        try:
            return json.loads(row[0])
        except (TypeError, json.JSONDecodeError):
            return None

    def save_management_state(self, position_ticket: int, state: dict[str, Any]) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO position_management(position_ticket,symbol,state_json,updated_at)
                VALUES(?,?,?,?)
                ON CONFLICT(position_ticket) DO UPDATE SET
                    symbol=excluded.symbol,state_json=excluded.state_json,
                    updated_at=excluded.updated_at
                """,
                (
                    int(position_ticket),
                    str(state.get("symbol", "")),
                    self._json(state),
                    self._now(),
                ),
            )

    def expired_proposals(self, now: datetime | None = None) -> list[dict[str, Any]]:
        stamp = (now or datetime.now(timezone.utc)).isoformat()
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT proposal_id,symbol,asset_class,engine,side,created_at,expires_at
                FROM trade_proposals
                WHERE state='APPROVED' AND expires_at<=?
                """,
                (stamp,),
            ).fetchall()
        keys = ("proposal_id", "symbol", "asset_class", "engine", "side", "created_at", "expires_at")
        return [dict(zip(keys, row)) for row in rows]

    def latest_loss_for(self, symbol: str, engine: str) -> dict[str, Any] | None:
        asset_class = {"FOREX": "forex", "INDICES": "index", "METALS": "metal"}.get(str(engine).upper(), str(engine).lower())
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT h.trade_id, h.closed_at, h.realized_pnl, h.result_r
                FROM trade_history h
                LEFT JOIN trade_proposals p ON p.proposal_id=h.proposal_id
                WHERE UPPER(h.symbol)=?
                  AND h.realized_pnl < 0
                  AND (UPPER(COALESCE(p.engine, ''))=? OR (p.engine IS NULL AND LOWER(h.asset_class)=?))
                ORDER BY h.closed_at DESC
                LIMIT 1
                """,
                (str(symbol).upper(), str(engine).upper(), asset_class),
            ).fetchone()
        if not row:
            return None
        return {"trade_id": row[0], "closed_at": row[1], "realized_pnl": float(row[2] or 0.0), "result_r": float(row[3] or 0.0)}

    def latest_win_for(self, symbol: str, engine: str) -> dict[str, Any] | None:
        asset_class = {"FOREX": "forex", "INDICES": "index", "METALS": "metal"}.get(str(engine).upper(), str(engine).lower())
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT h.trade_id, h.closed_at, h.realized_pnl, h.result_r
                FROM trade_history h
                LEFT JOIN trade_proposals p ON p.proposal_id=h.proposal_id
                WHERE UPPER(h.symbol)=?
                  AND h.realized_pnl > 0
                  AND (UPPER(COALESCE(p.engine, ''))=? OR (p.engine IS NULL AND LOWER(h.asset_class)=?))
                ORDER BY h.closed_at DESC
                LIMIT 1
                """,
                (str(symbol).upper(), str(engine).upper(), asset_class),
            ).fetchone()
        if not row:
            return None
        return {"trade_id": row[0], "closed_at": row[1], "realized_pnl": float(row[2] or 0.0), "result_r": float(row[3] or 0.0)}

    def count_today(self):
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*)
                FROM platform_executions
                WHERE created_at LIKE ?
                  AND status IN ('FILLED', 'DRY_RUN', 'CLOSED')
                """,
                (datetime.now(timezone.utc).date().isoformat() + "%",),
            ).fetchone()
        return int(row[0] if row else 0)

    def count_today_for_symbol(self, symbol: str) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*)
                FROM platform_executions
                WHERE created_at LIKE ?
                  AND UPPER(symbol)=?
                  AND status IN ('FILLED', 'DRY_RUN', 'CLOSED')
                """,
                (datetime.now(timezone.utc).date().isoformat() + "%", str(symbol).upper()),
            ).fetchone()
        return int(row[0] if row else 0)

    def burst_results_today_for_symbol(self, symbol: str) -> tuple[int, int]:
        """Net win/loss count for TODAY's closed bursts on this symbol.

        Verified 2026-08-15 against the broker's own deal ledger: a
        trade_history row's realized_pnl matches the broker's DEAL_ENTRY_OUT
        profit exactly for the position it tracks, and every burst produces
        exactly one trade_history row (checked across all 62 rows in history -
        none had a second row sharing the same root proposal_id). No join or
        grouping is needed; each row already IS one burst's tracked outcome.

        NOTE: some pyramid legs beyond the first are recorded with
        position_ticket=0 in the executions table and never independently
        verified against a broker close - flagged for review, not resolved
        here. This does not affect the figure used below, which is exactly
        what trade_history already reports.

        Returns (wins, losses): closed trades today, net positive vs net
        non-positive.
        """
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT realized_pnl FROM trade_history
                WHERE closed_at LIKE ? AND UPPER(symbol)=?
                """,
                (datetime.now(timezone.utc).date().isoformat() + "%", str(symbol).upper()),
            ).fetchall()
        wins = sum(1 for (pnl,) in rows if float(pnl or 0.0) > 0)
        losses = sum(1 for (pnl,) in rows if float(pnl or 0.0) <= 0)
        return wins, losses

    def count_realized_losses_today_for_symbol(self, symbol: str) -> int:
        """Count closed losing trades in the current Africa/Johannesburg day."""
        local_now = datetime.now(timezone.utc).astimezone(ZoneInfo("Africa/Johannesburg"))
        local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        utc_start = local_start.astimezone(timezone.utc).isoformat()
        utc_end = (local_start + timedelta(days=1)).astimezone(timezone.utc).isoformat()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*)
                FROM trade_history
                WHERE closed_at>=? AND closed_at<?
                  AND UPPER(symbol)=?
                  AND realized_pnl < 0
                """,
                (utc_start, utc_end, str(symbol).upper()),
            ).fetchone()
        return int(row[0] if row else 0)

    def latest_filled_execution_at_for_symbol(self, symbol: str) -> datetime | None:
        """Return the latest filled entry timestamp for a canonical symbol.

        This is an operational re-entry control. It deliberately counts a
        filled burst regardless of BUY or SELL, so a fresh opposite-direction
        proposal cannot immediately follow the first burst.
        """
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT created_at
                FROM platform_executions
                WHERE UPPER(symbol)=?
                  AND status IN ('FILLED', 'DRY_RUN', 'CLOSED')
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (str(symbol).upper(),),
            ).fetchone()
        if not row or not row[0]:
            return None
        try:
            value = datetime.fromisoformat(str(row[0]))
        except (TypeError, ValueError):
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def setup_fingerprint_seen_recently(
        self,
        symbol: str,
        fingerprint: str,
        since: datetime,
        *,
        exclude_proposal_id: str = "",
    ) -> bool:
        """Find a repeated setup identity without recalculating strategy logic."""
        if not fingerprint:
            return False
        cutoff = since.astimezone(timezone.utc).isoformat()
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT proposal_id,payload_json
                FROM trade_proposals
                WHERE UPPER(symbol)=? AND created_at>=?
                ORDER BY created_at DESC
                LIMIT 250
                """,
                (str(symbol).upper(), cutoff),
            ).fetchall()
        for proposal_id, payload_json in rows:
            if str(proposal_id) == str(exclude_proposal_id):
                continue
            try:
                payload = json.loads(payload_json or "{}")
                context = payload.get("context") or {}
            except (TypeError, ValueError, AttributeError):
                continue
            if str(context.get("setup_fingerprint") or "") == str(fingerprint):
                return True
        return False

    def realized_pnl_today(self) -> float:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT COALESCE(SUM(realized_pnl), 0.0)
                FROM trade_history
                WHERE closed_at LIKE ?
                """,
                (datetime.now(timezone.utc).date().isoformat() + "%",),
            ).fetchone()
        return float(row[0] if row else 0.0)

    def todays_closed_trades(self) -> list[tuple[str, float]]:
        # Raw (symbol, realized_pnl) rows for today, unfiltered by symbol -
        # callers that need alias-aware symbol matching (e.g. SILVER vs
        # XAGUSD) should filter in Python with their own canonicalizer,
        # since UPPER(symbol)=? exact-match queries silently miss aliases.
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT symbol, realized_pnl FROM trade_history WHERE closed_at LIKE ?",
                (datetime.now(timezone.utc).date().isoformat() + "%",),
            ).fetchall()
        return [(str(r[0]), float(r[1] or 0.0)) for r in rows]
