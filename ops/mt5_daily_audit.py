#!/usr/bin/env python3
"""Nightly audit of the LIVE Cipher FX platform (cipherfx_platform).

Replaces the legacy audit that inspected the retired mt5_bot.py monolith and
asserted the old limit values. Every check here targets the code and
configuration the running service actually uses.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

APP = Path("/opt/cipherfx_mt5")
DB = APP / "state/mt5_state.db"
ENV = Path("/etc/scalpbot/scalpbot-mt5.env")

sys.path.insert(0, str(APP))
for raw in ENV.read_text().splitlines():
    raw = raw.strip()
    if not raw or raw.startswith("#") or "=" not in raw:
        continue
    key, value = raw.split("=", 1)
    os.environ.setdefault(key.strip(), value.strip().strip(chr(34)))
os.environ.setdefault("CIPHERFX_BROKER_BACKEND", "mt5")
os.environ.setdefault("SCALPBOT_STATE_DB", str(DB))


def env(name: str, default: str = "") -> str:
    return str(os.getenv(name, default) or default).strip()


def market_open_now() -> bool:
    sast = datetime.now(timezone.utc).astimezone(ZoneInfo("Africa/Johannesburg"))
    return sast.weekday() < 5


def audit() -> int:
    checks: dict[str, bool] = {}
    details: dict[str, object] = {}

    # 1. Live service running.
    checks["service_active"] = subprocess.run(
        ["systemctl", "is-active", "--quiet", "scalpbot-mt5.service"]
    ).returncode == 0
    checks["dashboard_active"] = subprocess.run(
        ["systemctl", "is-active", "--quiet", "scalpbot-dashboard-mt5.service"]
    ).returncode == 0

    # 2. The live import chain is intact.
    import_probe = subprocess.run(
        [
            str(APP / ".venv_mt5/bin/python"),
            "-c",
            (
                "import sys; sys.path.insert(0, '/opt/cipherfx_mt5');"
                "from cipherfx_platform.runtime import main;"
                "from cipherfx_platform.execution import ExecutionEngine;"
                "from cipherfx_platform.engines.forex import ForexLearningEngine;"
                "from cipherfx_platform.engines.indices import IndicesLearningEngine;"
                "from cipherfx_platform.engines.metals import MetalsLearningEngine;"
                "print('ok')"
            ),
        ],
        cwd=APP,
        capture_output=True,
        text=True,
        timeout=60,
    )
    checks["live_imports_ok"] = import_probe.returncode == 0
    if import_probe.returncode != 0:
        details["import_error"] = import_probe.stderr.strip()[-500:]

    # 3. Engine architecture: M1 removed, H4 anchor present, in all 3 engines.
    engines_ok = True
    for engine_file in ("forex.py", "indices.py", "metals.py"):
        source = (APP / "cipherfx_platform/engines" / engine_file).read_text()
        if '"M1"' in source.split("TIME_WEIGHTS")[1].split("}")[0]:
            engines_ok = False
            details[f"m1_leak_{engine_file}"] = True
        if 'frames["H4"]' not in source:
            engines_ok = False
            details[f"h4_anchor_missing_{engine_file}"] = True
    checks["engines_m1_removed_h4_anchored"] = engines_ok

    # 4. Risk gates present in the live execution engine.
    execution_source = (APP / "cipherfx_platform/execution.py").read_text()
    checks["risk_gates_present"] = all(
        token in execution_source
        for token in (
            "_win_cooldown_reason",
            "_correlated_exposure_reason",
            "_pyramid_cooldown_reason",
            "MAX_TRADES_PER_SYMBOL",
        )
    )

    # 4b. Entry-timing guard (combo F) wired into every engine.
    guard_ok = (APP / "cipherfx_platform/entry_timing.py").exists()
    for engine_file in ("forex.py", "indices.py", "metals.py"):
        engine_source = (APP / "cipherfx_platform/engines" / engine_file).read_text()
        if "entry_timing.evaluate" not in engine_source or 'entry_guard["blocked"]' not in engine_source:
            guard_ok = False
            details[f"entry_guard_missing_{engine_file}"] = True
    checks["entry_timing_guard_wired"] = guard_ok

    # 4c. Index market hours are not artificially narrowed back down. Fixed
    # 2026-07-20: hardcoded cash-session windows were blocking hours this
    # account had already traded successfully in (verified against real
    # trade_history for US30/GER40/UK100/JP225).
    sessions_source = (APP / "cipherfx_platform/market_sessions.py").read_text()
    checks["index_hours_not_narrowed"] = (
        "US_MARKET_OPEN_LOCAL" not in sessions_source
        and "ASIA_OPEN" not in sessions_source
        and "EUROPE_OPEN" not in sessions_source
    )

    # 4d. Per-position dollar loss cap scales with the trade's own 1R risk, so
    # it can never scalp-cut a swing entry below its price stop.
    management_source = (APP / "cipherfx_platform/management.py").read_text()
    checks["loss_cap_scales_with_risk"] = (
        "_effective_loss_cap" in management_source
        and "initial_risk * 1.5" in management_source
    )

    # 4e. Per-symbol UTC hour/weekday time filters wired into all three
    # engines. Added 2026-07-22 after the deep-history backtest (~15 months
    # / ~11,600 trades via the MQL5 deephistory export) found reproducible
    # bad windows (validated independently in both halves of the sample)
    # for 11 of 14 symbols. Turned the full-universe backtest total from
    # -145.86R to +37.47R with zero symbols made worse. Config lives in
    # config/symbol_time_filters.json. Extended 2026-07-23: hour 0 UTC
    # (02:00 SAST) added for every index symbol including US30 (a fresh
    # entry - previously untouched) after real live losses clustered there,
    # pending a full cross-validated backtest of that specific window like
    # the rest of this file already has. XAUUSD/XAGUSD legitimately have no
    # entries (no qualifying windows found) and must stay untouched.
    try:
        time_filters_config = json.loads((APP / "config/symbol_time_filters.json").read_text())
    except Exception:
        time_filters_config = None
    time_filters_ok = (
        time_filters_config is not None
        and set(time_filters_config.keys())
        == {"EURUSD", "GBPUSD", "USDJPY", "USDCAD", "NAS100", "SPX500",
            "GER40", "UK100", "FRA40", "EU50", "JP225", "US30"}
    )
    for engine_file in ("forex.py", "indices.py", "metals.py"):
        engine_source = (APP / "cipherfx_platform/engines" / engine_file).read_text()
        if "time_filters.blocked(snapshot.symbol" not in engine_source:
            time_filters_ok = False
            details[f"time_filters_missing_{engine_file}"] = True
    checks["symbol_time_filters_wired"] = time_filters_ok

    # 5. Configured limits match the approved values.
    # MT5_MAX_DAILY_LOSS_USD raised 1000->5000 on 2026-07-20 (old cap was
    # silently blocking every symbol for hours with no dashboard indication).
    # MT5_MAX_PYRAMID_TRADES dropped 8->1 on 2026-07-21 (pyramiding disabled
    # entirely after the 8-leg simultaneous burst-fire proved too risky).
    checks["limits_aligned"] = (
        env("MT5_MAX_DAILY_TRADES") == "100"
        and env("MT5_MAX_DAILY_LOSS_USD") == "5000"
        and env("MT5_MAX_DAILY_TRADES_PER_SYMBOL") == "10"
        and env("MT5_MAX_PYRAMID_TRADES") == "1"
    )

    # 5b. Pyramid legs decay in size, and gold stays sized down.
    execution_source_for_sizing = (APP / "cipherfx_platform/execution.py").read_text()
    checks["pyramid_size_decay_wired"] = (
        "pyramid_leg_index" in execution_source_for_sizing
        and "MT5_PYRAMID_SIZE_DECAY" in execution_source_for_sizing
    )
    try:
        gold_risk_pct = float(env("MT5_RISK_PCT_XAUUSD", "0"))
        metals_default = float(env("MT5_RISK_PCT_METALS", "1"))
    except ValueError:
        gold_risk_pct, metals_default = 999.0, 1.0
    checks["gold_sized_conservatively"] = 0 < gold_risk_pct <= metals_default

    # 6. Symbol universe intact (9 canonical instruments). EURJPY removed
    # 2026-07-21 (4.2% all-time win rate, worst forex symbol by a wide
    # margin). AUDUSD/NZDUSD removed 2026-07-22 after a fresh backtest
    # confirmed both still net negative even in the recent out-of-sample
    # window (avg -0.28R and -0.11R/trade) while the rest of forex had
    # turned net positive. EURUSD removed 2026-07-22 after the deep-history
    # backtest (~15 months, 839 trades via the MQL5 deephistory export)
    # showed a dead-stable -0.057R/trade edge in both halves of the sample -
    # the worst and most reliably negative symbol in the whole universe.
    # GER40/FRA40/EU50 removed 2026-07-23 - full-history realized P&L was
    # net negative for every single index symbol (-$7,201.57 combined across
    # all 8), and these three were the worst three (-$1,931.73/-$1,527.16/
    # -$1,292.14, win rates 21%/14%/29%). USDCAD removed 2026-07-23 - 11%
    # win rate (1 win in 9 trades), -$1,830.85, worse than every symbol
    # already removed from forex.
    try:
        catalog = json.loads((APP / "mt5_symbols.json").read_text())
        active = [
            symbol
            for group in ("forex", "metals", "indices")
            for symbol in (catalog.get(group) or [])
        ]
    except Exception:
        active = []
    checks["symbol_universe_9"] = (
        len(active) == 9
        and "EURJPY" not in active
        and "AUDUSD" not in active
        and "NZDUSD" not in active
        and "EURUSD" not in active
        and "GER40" not in active
        and "FRA40" not in active
        and "USDCAD" not in active
        and "EU50" not in active
    )
    details["active_symbols"] = len(active)

    # 7. Database healthy.
    conn = sqlite3.connect(DB)
    quick = str(conn.execute("PRAGMA quick_check").fetchone()[0])
    try:
        service_payload = json.loads(
            dict(conn.execute("SELECT key,value_json FROM platform_status")).get("service") or "{}"
        )
    except Exception:
        service_payload = {}
    conn.close()
    checks["sqlite_writable"] = os.access(DB, os.W_OK) and quick == "ok"

    # 8. Broker connection - only meaningful while the market is open.
    connected = str(service_payload.get("state") or "").upper() == "CONNECTED"
    if market_open_now():
        checks["mt5_connected"] = connected
    else:
        details["mt5_connected_weekend_skip"] = connected

    details.update(
        {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "audited_stack": "cipherfx_platform",
        }
    )

    for name, passed in checks.items():
        print(f"[MT5_DAILY_AUDIT] {name}={'PASS' if passed else 'FAIL'}")
    ok = all(checks.values())
    payload = {"overall": "PASS" if ok else "FAIL", "checks": checks, "details": details}
    try:
        from dashboard.backend import state_store as db

        db.set_status("last_daily_audit", payload)
    except Exception as exc:
        print(f"[MT5_DAILY_AUDIT] status_write_skipped={exc}")
    print(f"[MT5_DAILY_AUDIT] overall={payload['overall']} generated_at={details['generated_at']}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(audit())
