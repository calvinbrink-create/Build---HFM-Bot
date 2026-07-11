#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

APP = Path("/opt/cipherfx_mt5")
DB = APP / "state/mt5_state.db"
ENV = Path("/etc/scalpbot/scalpbot-mt5.env")
BRIDGE_SOURCE = APP / "mt5_bridge/CipherFxBridge.mq5"
DEPLOYED_SOURCE = Path("/root/.mt5/drive_c/Program Files/MetaTrader 5/MQL5/Experts/CipherFxBridge.mq5")
DEPLOYED_EX5 = DEPLOYED_SOURCE.with_suffix(".ex5")

sys.path.insert(0, str(APP))
for raw in ENV.read_text().splitlines():
    raw = raw.strip()
    if not raw or raw.startswith("#") or "=" not in raw:
        continue
    key, value = raw.split("=", 1)
    os.environ.setdefault(key.strip(), value.strip().strip(chr(34)))
os.environ.setdefault("CIPHERFX_BROKER_BACKEND", "mt5")
os.environ.setdefault("SCALPBOT_STATE_DB", str(DB))

from dashboard.backend import state_store as db
from mt5_xm_config import MT5RuntimeConfig


def fresh(path: Path, seconds: float) -> bool:
    return path.exists() and datetime.now().timestamp() - path.stat().st_mtime <= seconds


def env(name: str, default: str = "") -> str:
    return str(os.getenv(name, default) or default).strip()


def audit() -> int:
    checks: dict[str, bool] = {}
    details: dict[str, object] = {}
    runtime = MT5RuntimeConfig()
    source = (APP / "mt5_bot.py").read_text()
    mql = BRIDGE_SOURCE.read_text()
    bridge_dir = Path(env("MT5_BRIDGE_DIR"))
    visible_path = bridge_dir / "visible_symbols.json"
    account_path = bridge_dir / "account.txt"

    conn = sqlite3.connect(DB)
    status = dict(conn.execute("SELECT key,value FROM bot_status"))
    quick = str(conn.execute("PRAGMA quick_check").fetchone()[0])
    conn.close()

    try:
        export_status = json.loads(status.get("last_export_reconciliation", "{}"))
    except Exception:
        export_status = {}
    try:
        visible = json.loads(visible_path.read_text())
    except Exception:
        visible = {}
    visible_symbols = visible.get("symbols") if isinstance(visible, dict) else []

    checks["service_active"] = subprocess.run(
        ["systemctl", "is-active", "--quiet", "scalpbot-mt5.service"]
    ).returncode == 0
    checks["mt5_connected"] = str(status.get("mt5_connected", "")).lower() == "true"
    checks["bridge_fresh"] = fresh(visible_path, 172800) and fresh(account_path, 30)
    checks["visible_manifest_complete"] = isinstance(visible_symbols, list) and len(visible_symbols) == 17 and bool(visible.get("generated_at"))
    checks["sqlite_writable"] = os.access(DB, os.W_OK) and quick == "ok"
    checks["max_open_aligned"] = (
        env("MT5_MAX_OPEN_TRADES") == "30"
        and env("MT5_MAX_OPEN_TRADES_TOTAL") == "30"
        and runtime.max_open_trades == 30
        and str(status.get("max_open_trades")) == "30"
        and "input int MaxOpenTradesTotal = 30;" in mql
    )
    checks["accepted_daily_limit_aligned"] = (
        env("MT5_MAX_TRADES_PER_DAY") == "60"
        and str(status.get("max_daily_trades")) == "60"
        and "input int MaxTradesPerDay = 60;" in mql
        and "accepted-entry daily trade limit reached" in source
    )
    checks["daily_loss_aligned"] = (
        env("MT5_MAX_DAILY_LOSS_USD") == "1000"
        and "input double DailyLossLimitUSD = 1000.00;" in mql
        and "read_broker_day_pnl" in source
    )
    checks["pyramid_controls_aligned"] = (
        env("MT5_PYRAMID_MAX_LEVELS") == "10"
        and "input int MaxPyramidTrades = 10;" in mql
        and "input int MaxPyramidTradesPerSignal = 10;" in mql
        and "input bool AllowSameCandlePyramids = true;" in mql
        and "def _campaign_cycle" in source
    )
    checks["loss_streak_observe_only"] = (
        'policy = "observe_only"' in source
        and '"event": "LOSS_STREAK_OBSERVE_ONLY"' in source
    )
    checks["exclusive_replacement_routing"] = (
        "replacement strategy engine unavailable; refusing legacy fallback" in source
        and "import scalping_bot_v4" not in source
    )
    checks["broker_geometry_active"] = (
        "order_calc_trade_geometry" in source
        and "BROKER_GEOMETRY_VALIDATED" in source
        and "_management_rr_for_position" in source
    )
    checks["pre_setup_quality_active"] = all(token in source for token in (
        "STRATEGY_V1_PRE_SETUP_QUALITY",
        "STRATEGY_V1_PRE_SETUP_SAFETY",
        "INDEX_EXHAUSTION_NOT_QUALIFIED",
        "CENTRAL_COST_MODEL_AUTHORITATIVE",
    ))
    checks["causal_m1_and_ttl_active"] = (
        "_completed_m1_candle_id" in source
        and "candle_closed_dt <= setup_created_dt" in source
        and "return 90" in source
        and source.count("return 120") >= 3
    )
    checks["last_export_reconciled"] = bool(export_status.get("reconciled"))
    checks["source_deployed"] = DEPLOYED_SOURCE.exists() and BRIDGE_SOURCE.read_bytes() == DEPLOYED_SOURCE.read_bytes()
    checks["compiled_expert_present"] = DEPLOYED_EX5.exists() and DEPLOYED_EX5.stat().st_size > 0
    test_result = subprocess.run(
        [str(APP / ".venv_mt5/bin/python"), str(APP / "test_mt5_replacement_pipeline.py")],
        cwd=APP,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=120,
    )
    checks["replacement_test_suite"] = test_result.returncode == 0
    details.update({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "broker_day": db.broker_trading_today(),
        "accepted_entries_broker_day": db.read_todays_trade_count(),
        "realized_pnl_broker_day": db.read_broker_day_pnl(),
        "visible_symbols": len(visible_symbols or []),
        "test_output": test_result.stdout.strip().splitlines()[-8:],
        "export_status": export_status,
    })

    for name, passed in checks.items():
        print(f"[MT5_DAILY_AUDIT] {name}={'PASS' if passed else 'FAIL'}")
    ok = all(checks.values())
    payload = {"overall": "PASS" if ok else "FAIL", "checks": checks, "details": details}
    db.set_status("last_daily_audit", payload)
    print(f"[MT5_DAILY_AUDIT] overall={payload['overall']} generated_at={details['generated_at']}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(audit())

