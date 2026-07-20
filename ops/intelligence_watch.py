#!/usr/bin/env python3
"""Cipher FX intelligence watch: a continuous read-only layer over the live
platform. Watches everything - trades, risk-gate activity, system health,
config - and produces a findings report. It never writes to the trading
engine, execution config, or broker; it only reads and reports, so a bad
finding can never become a bad trade. A human reviews the report and decides
what (if anything) to act on, the same process used manually throughout the
2026-07-19 rebuild session.

Run standalone: .venv_mt5/bin/python ops/intelligence_watch.py
Scheduled via scalpbot-mt5-intelligence.timer (every 6 hours).
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

APP = Path("/opt/cipherfx_mt5")
DB = APP / "state/mt5_state.db"
REPORT_DIR = APP / "state/intelligence_reports"
ENV = Path("/etc/scalpbot/scalpbot-mt5.env")

# Trades before this cutoff ran under the old scoring/exit/gate config and
# would make any "trailing win rate" check meaningless noise if mixed in
# with post-rebuild trades. Update this if the engines change again.
REBUILT_SYSTEM_SINCE = "2026-07-19T14:34:00+00:00"

# Known-good baselines this watch checks against. These are the values
# approved 2026-07-19; a finding here means live config has drifted from
# what was actually decided, not necessarily that the value is wrong.
EXPECTED = {
    "MT5_MAX_DAILY_TRADES": "100",
    "MT5_MAX_DAILY_LOSS_USD": "1000",
    "MT5_MAX_DAILY_TRADES_PER_SYMBOL": "10",
    "MT5_MAX_PYRAMID_TRADES": "8",
    "MT5_PYRAMID_SIZE_DECAY": "0.70",
    "MT5_RISK_PCT_XAUUSD": "0.30",
}


def env_file_values() -> dict:
    values = {}
    for raw in ENV.read_text().splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        values.setdefault(key.strip(), value.strip())
    return values


def bot_process_env() -> dict:
    pid_result = subprocess.run(["pgrep", "-f", "run_mt5_bot.py"], capture_output=True, text=True)
    pid = pid_result.stdout.split()[0] if pid_result.stdout.split() else None
    if not pid:
        return {}
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes().decode(errors="ignore")
    except OSError:
        return {}
    values = {}
    for line in raw.split("\0"):
        if "=" in line:
            k, v = line.split("=", 1)
            values[k] = v
    return values


def findings_config_drift() -> list[dict]:
    findings = []
    file_env = env_file_values()
    proc_env = bot_process_env()
    for key, expected in EXPECTED.items():
        file_val = file_env.get(key)
        proc_val = proc_env.get(key)
        if file_val != expected:
            findings.append({
                "severity": "warning",
                "area": "config",
                "summary": f"{key} in the env file is {file_val!r}, expected {expected!r}",
            })
        if proc_env and proc_val != expected:
            findings.append({
                "severity": "critical",
                "area": "config",
                "summary": f"{key} in the RUNNING process is {proc_val!r}, expected {expected!r} "
                           f"- the live bot has not picked up the approved value (needs a restart)",
            })
    return findings


def findings_trading_activity(conn: sqlite3.Connection) -> list[dict]:
    findings = []
    cur = conn.cursor()
    today = datetime.now(timezone.utc).date().isoformat()

    row = cur.execute(
        "SELECT COUNT(*), COALESCE(SUM(realized_pnl), 0) FROM trade_history WHERE DATE(closed_at) = ?",
        (today,),
    ).fetchone()
    trades_today, pnl_today = row[0], row[1]

    daily_cap = float(EXPECTED["MT5_MAX_DAILY_LOSS_USD"])
    if pnl_today <= -0.8 * daily_cap:
        findings.append({
            "severity": "critical",
            "area": "risk",
            "summary": f"Today's realized P&L is ${pnl_today:,.0f}, {abs(pnl_today) / daily_cap * 100:.0f}% "
                       f"of the ${daily_cap:,.0f} daily loss cap - close to a full stop for the day",
        })
    max_trades = int(EXPECTED["MT5_MAX_DAILY_TRADES"])
    if trades_today >= 0.8 * max_trades:
        findings.append({
            "severity": "warning",
            "area": "activity",
            "summary": f"{trades_today} trades today, {trades_today / max_trades * 100:.0f}% "
                       f"of the {max_trades}/day cap",
        })

    # Recent win rate vs the trailing-30-trades baseline, per engine. Only
    # counts trades closed under the current engine config (see
    # REBUILT_SYSTEM_SINCE) - mixing in pre-rebuild trades would compare
    # today's system against a scoring/exit config that no longer exists.
    # trade_history.asset_class is stored lowercase/singular: forex/index/metal.
    for engine, asset_class in (("FOREX", "forex"), ("INDICES", "index"), ("METALS", "metal")):
        rows = cur.execute(
            """SELECT realized_pnl FROM trade_history
               WHERE asset_class = ? AND closed_at >= ? ORDER BY closed_at DESC LIMIT 30""",
            (asset_class, REBUILT_SYSTEM_SINCE),
        ).fetchall()
        if len(rows) < 10:
            continue
        pnls = [r[0] for r in rows]
        wins = sum(1 for p in pnls if p > 0)
        win_rate = 100 * wins / len(pnls)
        if win_rate < 25:
            findings.append({
                "severity": "warning",
                "area": "performance",
                "summary": f"{engine} trailing {len(pnls)}-trade win rate is {win_rate:.0f}% - "
                           f"worth a fresh backtest against current live data before the next round of tuning",
            })
    return findings


def findings_gate_activity(conn: sqlite3.Connection) -> list[dict]:
    """Confirm the risk gates are actually firing, not silently dead."""
    findings = []
    cur = conn.cursor()
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    rows = cur.execute(
        "SELECT payload_json FROM platform_events WHERE event_time >= ? AND event_type LIKE '%REJECT%'",
        (since,),
    ).fetchall()
    reasons = {}
    for (payload,) in rows:
        try:
            data = json.loads(payload)
        except (TypeError, ValueError):
            continue
        reason = str(data.get("reason") or "")
        for tag in ("WIN_COOLDOWN", "CORRELATED_EXPOSURE", "MAX_PYRAMID", "MAX_TRADES_PER_SYMBOL",
                    "ENTRY_STRETCHED", "ENTRY_CHASE", "ENTRY_BAR_EXTREME", "H1_MOMENTUM_MISALIGNED"):
            if tag in reason:
                reasons[tag] = reasons.get(tag, 0) + 1
    trade_count = cur.execute(
        "SELECT COUNT(*) FROM trade_history WHERE closed_at >= ?", (since,)
    ).fetchone()[0]
    if trade_count >= 20 and not reasons:
        findings.append({
            "severity": "info",
            "area": "gates",
            "summary": f"{trade_count} trades closed in the last 7 days with zero recorded gate "
                       f"rejections - only worth a look if that seems too clean for the trade volume",
        })
    return findings


def findings_system_health() -> list[dict]:
    findings = []
    for svc in ("scalpbot-mt5", "scalpbot-dashboard-mt5", "nginx"):
        active = subprocess.run(["systemctl", "is-active", "--quiet", svc]).returncode == 0
        if not active:
            findings.append({"severity": "critical", "area": "system", "summary": f"{svc} is not active"})
    failed = subprocess.run(
        ["systemctl", "list-units", "--state=failed", "--plain", "--no-legend"],
        capture_output=True, text=True,
    ).stdout.strip()
    if failed:
        findings.append({"severity": "warning", "area": "system", "summary": f"failed systemd units: {failed}"})
    disk = subprocess.run(["df", "--output=pcent", "/"], capture_output=True, text=True).stdout
    pct = int(disk.strip().splitlines()[-1].rstrip("%")) if disk.strip() else 0
    if pct >= 85:
        findings.append({"severity": "warning", "area": "system", "summary": f"disk usage at {pct}%"})
    return findings


def main() -> int:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    findings = (
        findings_config_drift()
        + findings_trading_activity(conn)
        + findings_gate_activity(conn)
        + findings_system_health()
    )
    conn.close()

    severity_rank = {"critical": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: severity_rank.get(f["severity"], 3))
    overall = "CRITICAL" if any(f["severity"] == "critical" for f in findings) else \
              "WARNING" if any(f["severity"] == "warning" for f in findings) else "OK"

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "overall": overall,
        "finding_count": len(findings),
        "findings": findings,
    }
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (REPORT_DIR / f"{stamp}.json").write_text(json.dumps(report, indent=2))
    (REPORT_DIR / "latest.json").write_text(json.dumps(report, indent=2))

    # Prune old reports - keep 30 days.
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    for path in REPORT_DIR.glob("*.json"):
        if path.name == "latest.json":
            continue
        try:
            stamp_dt = datetime.strptime(path.stem, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if stamp_dt < cutoff:
            path.unlink(missing_ok=True)

    print(f"[INTEL_WATCH] overall={overall} findings={len(findings)}")
    for f in findings:
        print(f"[INTEL_WATCH] {f['severity'].upper():8s} [{f['area']}] {f['summary']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
