"""SQLite persistence for shadow visual predictions and evidence."""
from __future__ import annotations
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

SHADOW_MODE = "SHADOW_ONLY"
SHADOW_MODEL_VERSION = "shadow_heuristic_v1"
SHADOW_FEATURE_VERSION = "causal_features_v1"

def _connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn

def ensure_visual_tables(db_path: str | Path) -> None:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _connect(path) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS visual_predictions (
            prediction_id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
            symbol TEXT NOT NULL, session TEXT, setup_id TEXT, trade_id TEXT,
            mode TEXT NOT NULL, module_version TEXT NOT NULL,
            model_version TEXT NOT NULL, feature_version TEXT NOT NULL,
            h1_features_json TEXT NOT NULL, m15_features_json TEXT NOT NULL,
            m5_features_json TEXT NOT NULL, m1_features_json TEXT NOT NULL,
            numeric_features_json TEXT NOT NULL, cost_context_json TEXT NOT NULL,
            engine_context_json TEXT NOT NULL, probabilities_json TEXT NOT NULL,
            confidence REAL NOT NULL, uncertainty REAL NOT NULL,
            out_of_distribution REAL NOT NULL, abstain INTEGER NOT NULL,
            reasons_json TEXT NOT NULL, input_hash TEXT NOT NULL,
            latency_ms REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_visual_predictions_created
            ON visual_predictions(created_at);
        CREATE INDEX IF NOT EXISTS idx_visual_predictions_symbol
            ON visual_predictions(symbol, created_at);
        CREATE TABLE IF NOT EXISTS visual_outcomes (
            prediction_id TEXT PRIMARY KEY, evaluated_at TEXT NOT NULL,
            horizon_seconds INTEGER NOT NULL, direction TEXT,
            outcome_label TEXT, realized_r REAL, costs_paid REAL,
            future_window_end TEXT,
            FOREIGN KEY(prediction_id) REFERENCES visual_predictions(prediction_id)
        );
        CREATE TABLE IF NOT EXISTS visual_drift (
            drift_id INTEGER PRIMARY KEY AUTOINCREMENT, measured_at TEXT NOT NULL,
            symbol TEXT, metric TEXT NOT NULL, value REAL, threshold REAL,
            status TEXT NOT NULL, details_json TEXT NOT NULL
        );
        """)

def insert_prediction(db_path: str | Path, prediction: Mapping[str, Any]) -> None:
    ensure_visual_tables(db_path)
    dump = lambda v: json.dumps(v, sort_keys=True, separators=(",", ":"), default=str)
    with _connect(db_path) as conn:
        conn.execute("""
        INSERT OR REPLACE INTO visual_predictions (
            prediction_id, created_at, symbol, session, setup_id, trade_id,
            mode, module_version, model_version, feature_version,
            h1_features_json, m15_features_json, m5_features_json, m1_features_json,
            numeric_features_json, cost_context_json, engine_context_json,
            probabilities_json, confidence, uncertainty, out_of_distribution,
            abstain, reasons_json, input_hash, latency_ms
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            prediction["prediction_id"], prediction["created_at"], prediction["symbol"],
            prediction.get("session"), prediction.get("setup_id"), prediction.get("trade_id"),
            prediction["mode"], prediction["module_version"], prediction["model_version"],
            prediction["feature_version"], dump(prediction["h1_features"]),
            dump(prediction["m15_features"]), dump(prediction["m5_features"]),
            dump(prediction["m1_features"]), dump(prediction.get("numeric_features", {})),
            dump(prediction.get("cost_context", {})), dump(prediction.get("engine_context", {})),
            dump(prediction["probabilities"]), float(prediction["confidence"]),
            float(prediction["uncertainty"]), float(prediction["out_of_distribution"]),
            int(bool(prediction["abstain"])), dump(prediction["reasons"]),
            prediction["input_hash"], float(prediction["latency_ms"]),
        ))

def insert_outcome(db_path: str | Path, *, prediction_id: str,
                   outcome_label: str, realized_r: float | None = None,
                   costs_paid: float | None = None, horizon_seconds: int = 0,
                   direction: str = "", future_window_end: str | None = None,
                   evaluated_at: str | None = None) -> None:
    ensure_visual_tables(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            """INSERT OR REPLACE INTO visual_outcomes(
                prediction_id,evaluated_at,horizon_seconds,direction,
                outcome_label,realized_r,costs_paid,future_window_end
            ) VALUES (?,?,?,?,?,?,?,?)""",
            (
                str(prediction_id),
                evaluated_at or datetime.now(timezone.utc).isoformat(),
                int(horizon_seconds or 0),
                str(direction or ""),
                str(outcome_label or ""),
                float(realized_r) if realized_r is not None else None,
                float(costs_paid) if costs_paid is not None else None,
                future_window_end,
            ),
        )


def insert_drift(db_path: str | Path, *, symbol: str, metric: str,
                 value: float | None, threshold: float | None,
                 status: str, details: Mapping[str, Any] | None = None) -> None:
    ensure_visual_tables(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            """INSERT INTO visual_drift(
                measured_at,symbol,metric,value,threshold,status,details_json
            ) VALUES (?,?,?,?,?,?,?)""",
            (
                datetime.now(timezone.utc).isoformat(),
                str(symbol or ""),
                str(metric or ""),
                float(value) if value is not None else None,
                float(threshold) if threshold is not None else None,
                str(status or "INCONCLUSIVE"),
                json.dumps(details or {}, sort_keys=True, default=str),
            ),
        )


def fetch_visual_summary(db_path: str | Path) -> dict[str, Any]:
    ensure_visual_tables(db_path)
    with _connect(db_path) as conn:
        total = int(conn.execute("SELECT COUNT(*) FROM visual_predictions").fetchone()[0])
        abstained = int(conn.execute(
            "SELECT COUNT(*) FROM visual_predictions WHERE abstain=1"
        ).fetchone()[0])
        row = conn.execute("""
            SELECT prediction_id, created_at, symbol, session, mode, model_version,
                   confidence, uncertainty, out_of_distribution, abstain,
                   reasons_json, probabilities_json, latency_ms
            FROM visual_predictions ORDER BY created_at DESC LIMIT 1
        """).fetchone()
    result = {
        "mode": SHADOW_MODE, "model_version": SHADOW_MODEL_VERSION,
        "feature_version": SHADOW_FEATURE_VERSION, "prediction_count": total,
        "abstention_count": abstained, "verified_profitability": False, "latest": None,
    }
    if row:
        result["latest"] = {
            "prediction_id": row["prediction_id"], "created_at": row["created_at"],
            "symbol": row["symbol"], "session": row["session"], "mode": row["mode"],
            "model_version": row["model_version"], "confidence": row["confidence"],
            "uncertainty": row["uncertainty"], "out_of_distribution": row["out_of_distribution"],
            "abstain": bool(row["abstain"]), "reasons": json.loads(row["reasons_json"]),
            "probabilities": json.loads(row["probabilities_json"]), "latency_ms": row["latency_ms"],
        }
    return result
