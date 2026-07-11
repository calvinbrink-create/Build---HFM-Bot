#!/usr/bin/env python3
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3

APP_DIR = Path("/opt/cipherfx_mt5")
STATE_DIR = APP_DIR / "state"
DB_PATH = Path(os.getenv("SCALPBOT_STATE_DB", str(STATE_DIR / "mt5_state.db")))
AUDIT_PATH = Path(os.getenv("MT5_DECISION_AUDIT_LOG", str(STATE_DIR / "mt5_decision_audit.jsonl")))
RUNTIME_PATH = Path(os.getenv("MT5_STATE_FILE", str(STATE_DIR / "mt5_runtime_state.json")))
NEWS_PATH = Path(os.getenv("MT5_NEWS_EVENTS_FILE", str(STATE_DIR / "news_events.json")))
REPORT_PATH = STATE_DIR / "mt5_safety_report_latest.json"
DASH_ENV = Path("/etc/scalpbot/scalpbot-mt5-dashboard.env")
BOT_ENV = Path("/etc/scalpbot/scalpbot-mt5.env")


def _read_env(path: Path) -> dict[str, str]:
    out = {}
    try:
        for raw in path.read_text(errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip().strip('"')
    except Exception:
        pass
    return out


def _connect_ro(path: Path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)


def _status(conn) -> dict[str, str]:
    try:
        return {str(k): str(v) for k, v in conn.execute("SELECT key,value FROM bot_status").fetchall()}
    except Exception:
        return {}


def _fetch(conn, sql: str, params=()):
    try:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    except Exception:
        return []


def _audit_events(limit: int = 5000) -> list[dict]:
    if not AUDIT_PATH.exists():
        return []
    rows = []
    try:
        lines = AUDIT_PATH.read_text(errors="ignore").splitlines()[-limit:]
        for line in lines:
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        pass
    return rows


def main() -> int:
    bot_env = _read_env(BOT_ENV)
    dash_env = _read_env(DASH_ENV)
    report: dict = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "backend": bot_env.get("CIPHERFX_BROKER_BACKEND") or os.getenv("CIPHERFX_BROKER_BACKEND", ""),
        "db_path": str(DB_PATH),
        "db_path_correct": str(DB_PATH) == "/opt/cipherfx_mt5/state/mt5_state.db",
        "trading_disabled_flag_exists": (STATE_DIR / "trading_disabled.flag").exists(),
        "news_gate_mode": bot_env.get("MT5_NEWS_ENTRY_GATE_MODE", os.getenv("MT5_NEWS_ENTRY_GATE_MODE", "")),
        "news_events_file": str(NEWS_PATH),
        "news_events_exists": NEWS_PATH.exists(),
        "dashboard_env_references_scalp_v3": "/opt/scalp_v3" in DASH_ENV.read_text(errors="ignore") if DASH_ENV.exists() else None,
        "venv_mt5_realpath": str(Path("/opt/cipherfx_mt5/.venv_mt5").resolve()) if Path("/opt/cipherfx_mt5/.venv_mt5").exists() else "missing",
        "venv_mt5_is_dedicated": Path("/opt/cipherfx_mt5/.venv_mt5").exists() and "/opt/scalp_v3" not in str(Path("/opt/cipherfx_mt5/.venv_mt5").resolve()),
    }

    if DB_PATH.exists():
        conn = _connect_ro(DB_PATH)
        status = _status(conn)
        report["last_heartbeat"] = status.get("last_heartbeat")
        report["daily_trade_count"] = status.get("daily_trade_count")
        report["daily_closed_pnl"] = None
        trades_today = _fetch(conn, "SELECT * FROM trades WHERE trade_date = date('now') OR substr(opened_at,1,10)=date('now')")
        open_positions = _fetch(conn, "SELECT * FROM trades WHERE status IN ('live','dry_run') ORDER BY opened_at DESC")
        report["open_positions"] = len(open_positions)
        report["trades_today"] = len(trades_today)
        outcomes = Counter(str(row.get("outcome") or row.get("status") or "unknown") for row in trades_today)
        report["today_outcomes"] = dict(outcomes)
        per_symbol_count = Counter(str(row.get("sym") or "") for row in trades_today)
        per_symbol_pnl = defaultdict(float)
        for row in trades_today:
            try:
                per_symbol_pnl[str(row.get("sym") or "")] += float(row.get("realized") or 0.0)
            except Exception:
                pass
        report["per_symbol_trade_count"] = dict(per_symbol_count)
        report["per_symbol_pnl"] = dict(per_symbol_pnl)
        conn.close()

    audit = _audit_events()
    event_counts = Counter(str(item.get("event") or "unknown") for item in audit)
    decision_counts = Counter(f"{item.get('event','unknown')}:{item.get('decision','unknown')}" for item in audit)
    report["audit_event_counts"] = dict(event_counts)
    report["audit_decision_counts"] = dict(decision_counts)
    report["news_gate_decisions"] = {k: v for k, v in decision_counts.items() if k.startswith("news_gate:")}
    report["safety_guard_blocks"] = decision_counts.get("safety_guard:block", 0)
    report["execution_gate"] = {k: v for k, v in decision_counts.items() if k.startswith("execution_gate:")}
    report["latest_20_decision_events"] = audit[-20:]

    try:
        runtime = json.loads(RUNTIME_PATH.read_text()) if RUNTIME_PATH.exists() else {}
    except Exception:
        runtime = {}
    report["runtime_state_keys"] = sorted(runtime.keys()) if isinstance(runtime, dict) else []

    REPORT_PATH.write_text(json.dumps(report, indent=2, sort_keys=True, default=str))

    print("MT5 Safety Report")
    print(f"generated_at_utc: {report['generated_at_utc']}")
    print(f"backend: {report.get('backend')}")
    print(f"db_path: {report.get('db_path')} correct={report.get('db_path_correct')}")
    print(f"trading_disabled_flag_exists: {report.get('trading_disabled_flag_exists')}")
    print(f"news_gate_mode: {report.get('news_gate_mode')}")
    print(f"last_heartbeat: {report.get('last_heartbeat')}")
    print(f"daily_trade_count: {report.get('daily_trade_count')}")
    print(f"open_positions: {report.get('open_positions')}")
    print(f"trades_today: {report.get('trades_today')}")
    print(f"dashboard_env_references_scalp_v3: {report.get('dashboard_env_references_scalp_v3')}")
    print(f"venv_mt5_is_dedicated: {report.get('venv_mt5_is_dedicated')}")
    print(f"news_gate_decisions: {report.get('news_gate_decisions')}")
    print(f"safety_guard_blocks: {report.get('safety_guard_blocks')}")
    print(f"execution_gate: {report.get('execution_gate')}")
    print(f"json_report: {REPORT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
