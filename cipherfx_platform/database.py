from __future__ import annotations
import json, sqlite3, threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

class DatabaseLayer:
    """Sole persistence boundary for the rebuilt platform."""
    def __init__(self, path: str | Path):
        self.path = Path(path); self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock(); self._initialize()
    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL"); conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=10000"); return conn
    def _initialize(self):
        with self._connect() as conn:
            conn.executescript("""CREATE TABLE IF NOT EXISTS platform_events(
                id INTEGER PRIMARY KEY AUTOINCREMENT,event_time TEXT NOT NULL,event_type TEXT NOT NULL,
                symbol TEXT,proposal_id TEXT,payload_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS platform_status(
                key TEXT PRIMARY KEY,value_json TEXT NOT NULL,updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS platform_executions(
                proposal_id TEXT PRIMARY KEY,symbol TEXT NOT NULL,side TEXT NOT NULL,status TEXT NOT NULL,
                volume REAL NOT NULL,pnl REAL DEFAULT 0,initial_risk REAL DEFAULT 0,created_at TEXT NOT NULL,closed_at TEXT);
            CREATE INDEX IF NOT EXISTS idx_platform_events_time ON platform_events(event_time);
            CREATE INDEX IF NOT EXISTS idx_platform_exec_created ON platform_executions(created_at);
            CREATE TABLE IF NOT EXISTS engine_performance (
                engine TEXT NOT NULL, trade_id TEXT NOT NULL, symbol TEXT NOT NULL,
                result_r REAL NOT NULL, pnl REAL NOT NULL, closed_at TEXT NOT NULL,
                PRIMARY KEY(engine, trade_id)
            );
            CREATE INDEX IF NOT EXISTS idx_engine_perf_closed ON engine_performance(engine, closed_at);""")
    @staticmethod
    def _now(): return datetime.now(timezone.utc).isoformat()
    def event(self,event_type:str,payload:dict[str,Any]|None=None,*,symbol="",proposal_id=""):
        with self._lock,self._connect() as conn:
            conn.execute("INSERT INTO platform_events(event_time,event_type,symbol,proposal_id,payload_json) VALUES(?,?,?,?,?)",
                (self._now(),str(event_type),str(symbol),str(proposal_id),json.dumps(payload or {},default=str,sort_keys=True)))
    def status(self,key:str,value:Any):
        with self._lock,self._connect() as conn:
            conn.execute("INSERT INTO platform_status(key,value_json,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
                (str(key),json.dumps(value,default=str,sort_keys=True),self._now()))
    def execution(self,proposal_id,symbol,side,status,volume,*,pnl=0.0,initial_risk=0.0,closed_at=None):
        with self._lock,self._connect() as conn:
            conn.execute("INSERT INTO platform_executions(proposal_id,symbol,side,status,volume,pnl,initial_risk,created_at,closed_at) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(proposal_id) DO UPDATE SET status=excluded.status,volume=excluded.volume,pnl=excluded.pnl,initial_risk=excluded.initial_risk,closed_at=excluded.closed_at",
                (str(proposal_id),str(symbol),str(side),str(status),float(volume),float(pnl),float(initial_risk),self._now(),closed_at))
    def proposal_seen(self,proposal_id):
        with self._lock,self._connect() as conn:
            return conn.execute("SELECT 1 FROM platform_executions WHERE proposal_id=? LIMIT 1",(str(proposal_id),)).fetchone() is not None
    def record_engine_outcome(self, engine: str, trade_id: str, symbol: str, result_r: float, pnl: float, closed_at: str | None = None) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO engine_performance(engine,trade_id,symbol,result_r,pnl,closed_at) VALUES(?,?,?,?,?,?)", (str(engine), str(trade_id), str(symbol), float(result_r), float(pnl), closed_at or self._now()))

    def engine_history(self, engine: str, limit: int = 100) -> list[dict[str, float | str]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT trade_id,symbol,result_r,pnl,closed_at FROM engine_performance WHERE engine=? ORDER BY closed_at DESC LIMIT ?", (str(engine), max(1, int(limit)))).fetchall()
        return [{"trade_id": r[0], "symbol": r[1], "result_r": float(r[2]), "pnl": float(r[3]), "closed_at": r[4]} for r in rows]

    def count_today(self):
        with self._lock,self._connect() as conn:
            row=conn.execute("SELECT COUNT(*) FROM platform_executions WHERE created_at LIKE ?",(datetime.now(timezone.utc).date().isoformat()+"%",)).fetchone()
        return int(row[0] if row else 0)
