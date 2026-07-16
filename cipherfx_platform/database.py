from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import MarketSnapshot, TradeProposal, ExecutionResult


class DatabaseLayer:
    """Sole persistence boundary for proposals, execution, positions, and learning."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _initialize(self):
        with self._connect() as conn:
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
                CREATE INDEX IF NOT EXISTS idx_platform_events_time ON platform_events(event_time);
                CREATE INDEX IF NOT EXISTS idx_platform_exec_created ON platform_executions(created_at);
                CREATE INDEX IF NOT EXISTS idx_proposals_state_expiry ON trade_proposals(state, expires_at);
                CREATE INDEX IF NOT EXISTS idx_exec_status ON executions(status, closed_at);
                CREATE INDEX IF NOT EXISTS idx_learning_engine_time ON learning_metrics(engine, created_at);
                """
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(engine_performance)")}
            if "metrics_json" not in columns:
                conn.execute("ALTER TABLE engine_performance ADD COLUMN metrics_json TEXT NOT NULL DEFAULT '{}'")

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

    def execution(
        self,
        proposal_id,
        symbol,
        side,
        status,
        volume,
        *,
        pnl=0.0,
        initial_risk=0.0,
        closed_at=None,
    ):
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO platform_executions(
                    proposal_id,symbol,side,status,volume,pnl,initial_risk,created_at,closed_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(proposal_id) DO UPDATE SET
                    status=excluded.status, volume=excluded.volume, pnl=excluded.pnl,
                    initial_risk=excluded.initial_risk, closed_at=excluded.closed_at
                """,
                (
                    str(proposal_id),
                    str(symbol),
                    str(side),
                    str(status),
                    float(volume),
                    float(pnl),
                    float(initial_risk),
                    self._now(),
                    closed_at,
                ),
            )

    def proposal_seen(self, proposal_id):
        with self._lock, self._connect() as conn:
            return (
                conn.execute(
                    "SELECT 1 FROM platform_executions WHERE proposal_id=? LIMIT 1",
                    (str(proposal_id),),
                ).fetchone()
                is not None
            )

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

    def open_filled_executions(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT e.proposal_id,e.position_ticket,e.symbol,e.side,e.volume,e.fill_price,
                       e.initial_risk,e.submitted_at,e.filled_at,p.asset_class,p.engine,p.created_at
                FROM executions e JOIN trade_proposals p ON p.proposal_id=e.proposal_id
                WHERE e.status='FILLED' AND e.closed_at IS NULL
                ORDER BY e.filled_at
                """
            ).fetchall()
        keys = (
            "proposal_id", "position_ticket", "symbol", "side", "volume", "fill_price",
            "initial_risk", "submitted_at", "filled_at", "asset_class", "engine", "created_at",
        )
        return [dict(zip(keys, row)) for row in rows]

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
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO trade_history(
                    trade_id,proposal_id,symbol,asset_class,side,realized_pnl,initial_risk,
                    result_r,exit_reason,closed_at,metrics_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    str(trade_id),
                    proposal_id,
                    str(symbol),
                    str(asset_class),
                    str(side),
                    float(realized_pnl),
                    float(initial_risk),
                    float(result_r),
                    str(exit_reason),
                    closed_at.isoformat(),
                    self._json(metrics),
                ),
            )
            return cursor.rowcount == 1

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
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO learning_metrics(
                    engine,trade_id,proposal_id,symbol,asset_class,created_at,metrics_json
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    str(engine),
                    str(trade_id),
                    proposal_id,
                    str(symbol),
                    str(asset_class),
                    self._now(),
                    self._json(metrics),
                ),
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

    def engine_history(self, engine: str, limit: int = 100) -> list[dict[str, float | str]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT trade_id,symbol,result_r,pnl,closed_at
                FROM engine_performance WHERE engine=?
                ORDER BY closed_at DESC LIMIT ?
                """,
                (str(engine), max(1, int(limit))),
            ).fetchall()
        return [
            {
                "trade_id": r[0],
                "symbol": r[1],
                "result_r": float(r[2]),
                "pnl": float(r[3]),
                "closed_at": r[4],
            }
            for r in rows
        ]

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

    def count_today(self):
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM platform_executions WHERE created_at LIKE ?",
                (datetime.now(timezone.utc).date().isoformat() + "%",),
            ).fetchone()
        return int(row[0] if row else 0)
