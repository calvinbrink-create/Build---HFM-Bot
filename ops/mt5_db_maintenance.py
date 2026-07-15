#!/usr/bin/env python3
"""Controlled SQLite retention and archive maintenance for the live MT5 VPS.

This job never deletes open state or trade evidence. Rows are archived and
verified before deletion. It is intentionally independent of strategy code.
"""
from __future__ import annotations

import argparse
import fcntl
import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DB_PATH = Path(os.environ.get("SCALPBOT_STATE_DB", "/opt/cipherfx_mt5/state/mt5_state.db"))
ARCHIVE_ROOT = Path(os.environ.get("SCALPBOT_DB_ARCHIVE_DIR", "/opt/cipherfx_mt5/state/archive"))
LOCK_PATH = Path("/run/lock/cipherfx-mt5-db-maintenance.lock")
BATCH_SIZE = 2000
DELETE_BATCH_SIZE = 5000

# High-volume diagnostics stay hot for 24h; the compressed VPS archive retains
# the complete forensic rows. This prevents the dashboard/runtime DB growing
# without bound while preserving evidence.
HIGH_VOLUME_TABLES = (
    "trade_permission_decisions",
    "market_data_health",
    "trade_decisions",
    "execution_cost_models",
    "gap_events",
    "liquidity_levels",
    "market_regimes",
    "news_events",
    "portfolio_exposure_snapshots",
    "premarket_plan_events",
    "premarket_plan_versions",
    "premarket_plans",
    "score_adjustments",
    "session_profiles",
    "setup_scores",
    "strategy_permissions",
    "shadow_trades",
    "visual_predictions",
)

# Strategy/trade evidence is kept substantially longer in SQLite. Trade ledger
# tables are deliberately absent and therefore retained indefinitely.
STANDARD_EVENT_TABLES = (
    "broker_symbol_specs",
    "configuration_versions",
    "database_integrity_events",
    "deployment_versions",
    "false_entries",
    "kill_switch_events",
    "live_drift_snapshots",
    "opening_ranges",
    "reconciliation_events",
    "setup_events",
    "system_health_events",
    "trade_attribution",
    "trade_flow_snapshots",
    "trade_replay_events",
    "walk_forward_results",
    "walk_forward_runs",
)

IMMUTABLE_TABLES = {
    "bot_status",
    "news",
    "pending_setups",
    "positions",
    "signals",
    "trades",
    "trade_executions",
    "trade_outcomes",
    "trade_excursions",
    "visual_outcomes",
    "visual_drift",
}

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")

def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'

def json_safe(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"__bytes_base64__": value.hex()}
    return value

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()

def table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute("PRAGMA table_info(%s)" % quote_ident(table))]

def eligible_tables(conn: sqlite3.Connection, requested: tuple[str, ...]) -> list[str]:
    present = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    return [table for table in requested if table in present]

def archive_table(
    conn: sqlite3.Connection,
    table: str,
    time_column: str,
    cutoff: str,
    output: Path,
) -> dict[str, Any]:
    columns = table_columns(conn, table)
    select_columns = ", ".join(quote_ident(column) for column in columns)
    sql = (
        "SELECT %s FROM %s WHERE %s < ? ORDER BY rowid"
        % (select_columns, quote_ident(table), quote_ident(time_column))
    )
    count = 0
    with gzip.open(output, "wt", encoding="utf-8", compresslevel=6) as handle:
        header = {
            "format": "cipherfx-sqlite-row-archive-v1",
            "table": table,
            "columns": columns,
            "time_column": time_column,
            "cutoff": cutoff,
        }
        handle.write(json.dumps({"header": header}, separators=(",", ":")) + "\n")
        cursor = conn.execute(sql, (cutoff,))
        while True:
            rows = cursor.fetchmany(BATCH_SIZE)
            if not rows:
                break
            for row in rows:
                handle.write(
                    json.dumps(
                        {"row": [json_safe(value) for value in row]},
                        separators=(",", ":"),
                        default=str,
                    )
                    + "\n"
                )
                count += 1
    return {"table": table, "columns": columns, "rows": count, "file": str(output)}

def verify_archive(path: Path, expected_rows: int) -> None:
    rows = 0
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        first = json.loads(handle.readline())
        if first.get("header", {}).get("format") != "cipherfx-sqlite-row-archive-v1":
            raise RuntimeError("archive header invalid: %s" % path)
        for line in handle:
            if line.strip():
                record = json.loads(line)
                if "row" not in record:
                    raise RuntimeError("archive row invalid: %s" % path)
                rows += 1
    if rows != expected_rows:
        raise RuntimeError(
            "archive verification row mismatch for %s: expected %s got %s"
            % (path, expected_rows, rows)
        )

def count_eligible(conn: sqlite3.Connection, table: str, time_column: str, cutoff: str) -> int:
    return int(
        conn.execute(
            "SELECT count(*) FROM %s WHERE %s < ?"
            % (quote_ident(table), quote_ident(time_column)),
            (cutoff,),
        ).fetchone()[0]
    )

def delete_eligible(
    conn: sqlite3.Connection, table: str, time_column: str, cutoff: str
) -> int:
    total = 0
    while True:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "DELETE FROM %s WHERE rowid IN "
            "(SELECT rowid FROM %s WHERE %s < ? LIMIT ?)"
            % (
                quote_ident(table),
                quote_ident(table),
                quote_ident(time_column),
            ),
            (cutoff, DELETE_BATCH_SIZE),
        )
        deleted = conn.execute("SELECT changes()").fetchone()[0]
        conn.commit()
        total += deleted
        if deleted < DELETE_BATCH_SIZE:
            return total
        time.sleep(0.05)

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--vacuum", action="store_true")
    parser.add_argument("--high-volume-hours", type=int, default=24)
    parser.add_argument("--standard-days", type=int, default=90)
    parser.add_argument("--m1-days", type=int, default=14)
    parser.add_argument("--candle-days", type=int, default=90)
    args = parser.parse_args()

    if not DB_PATH.exists():
        print("DB_NOT_FOUND", DB_PATH)
        return 2
    if args.high_volume_hours < 6:
        print("REFUSED high-volume retention must be at least 6 hours")
        return 2
    if args.m1_days < 7:
        print("REFUSED M1 retention must be at least 7 days")
        return 2

    ARCHIVE_ROOT.mkdir(parents=True, exist_ok=True)
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("w") as lock_handle:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("MAINTENANCE_ALREADY_RUNNING")
            return 0

        current = now_utc()
        cutoff_high = iso(current - timedelta(hours=args.high_volume_hours))
        cutoff_standard = iso(current - timedelta(days=args.standard_days))
        cutoff_m1 = iso(current - timedelta(days=args.m1_days))
        cutoff_candles = iso(current - timedelta(days=args.candle_days))
        run_id = current.strftime("%Y%m%dT%H%M%SZ")
        run_dir = ARCHIVE_ROOT / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        manifest: dict[str, Any] = {
            "format": "cipherfx-maintenance-manifest-v1",
            "run_id": run_id,
            "started_at": iso(current),
            "db": str(DB_PATH),
            "dry_run": not args.apply,
            "retention": {
                "high_volume_hours": args.high_volume_hours,
                "standard_event_days": args.standard_days,
                "m1_days": args.m1_days,
                "candle_days": args.candle_days,
            },
            "tables": [],
        }

        conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            plans: list[tuple[str, str, str]] = []
            for table in eligible_tables(conn, HIGH_VOLUME_TABLES):
                if "created_at" in table_columns(conn, table):
                    plans.append((table, "created_at", cutoff_high))
            for table in eligible_tables(conn, STANDARD_EVENT_TABLES):
                if "created_at" in table_columns(conn, table):
                    plans.append((table, "created_at", cutoff_standard))
            if "m1_candles" in eligible_tables(conn, ("m1_candles",)):
                plans.append(("m1_candles", "ts", cutoff_m1))
            if "candles" in eligible_tables(conn, ("candles",)):
                plans.append(("candles", "ts", cutoff_candles))

            for table, time_column, cutoff in plans:
                count = count_eligible(conn, table, time_column, cutoff)
                item = {
                    "table": table,
                    "time_column": time_column,
                    "cutoff": cutoff,
                    "eligible_rows": count,
                }
                if args.apply and count:
                    target = run_dir / ("%s.jsonl.gz" % table)
                    item.update(archive_table(conn, table, time_column, cutoff, target))
                    verify_archive(target, count)
                    item["sha256"] = sha256_file(target)
                    item["compressed_bytes"] = target.stat().st_size
                    item["deleted_rows"] = delete_eligible(conn, table, time_column, cutoff)
                manifest["tables"].append(item)
                print(json.dumps(item, sort_keys=True), flush=True)

            if args.apply:
                checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                manifest["wal_checkpoint"] = list(checkpoint) if checkpoint else None
                conn.commit()
                if args.vacuum:
                    free = shutil.disk_usage(DB_PATH.parent).free
                    db_size = DB_PATH.stat().st_size
                    if free < int(db_size * 1.25):
                        manifest["vacuum"] = {
                            "status": "SKIPPED_LOW_FREE_SPACE",
                            "free_bytes": free,
                            "db_bytes": db_size,
                        }
                    else:
                        print("VACUUM_START", flush=True)
                        conn.execute("VACUUM")
                        manifest["vacuum"] = {"status": "PASS"}
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            manifest["finished_at"] = iso(now_utc())
            manifest["archive_dir"] = str(run_dir)
            manifest_path = run_dir / "manifest.json"
            manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
            print("MANIFEST", manifest_path, flush=True)
            return 0
        finally:
            conn.close()

if __name__ == "__main__":
    raise SystemExit(main())
