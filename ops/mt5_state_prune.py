#!/usr/bin/env python3
"""Daily retention pruning for the live platform state database.

Keeps mt5_state.db bounded so it cannot fill the disk:
  - platform_events   : 14 days (position-monitor telemetry dominates volume)
  - market_snapshots  : 7 days  (full JSON payloads; the biggest rows)
  - m1_candles        : 14 days
  - candles           : 30 days
  - missed_trades     : 30 days
Trade history, executions, and proposals are never pruned - they are the
account record and stay small.

VACUUM runs only on Saturdays (market closed) because it takes an exclusive
lock; on other days freed pages are simply reused by new writes.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB = Path("/opt/cipherfx_mt5/state/mt5_state.db")

RETENTION = (
    ("platform_events", "event_time", 14),
    ("market_snapshots", "captured_at", 7),
    ("m1_candles", "ts", 14),
    ("candles", "ts", 30),
    ("missed_trades", "created_at", 30),
)


def main() -> int:
    conn = sqlite3.connect(DB, timeout=60)
    conn.execute("PRAGMA busy_timeout = 60000")
    total_deleted = 0
    for table, column, days in RETENTION:
        try:
            columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        except sqlite3.Error:
            continue
        if column not in columns:
            print(f"[MT5_STATE_PRUNE] {table}: column {column} missing, skipped")
            continue
        cur = conn.execute(
            f"DELETE FROM {table} WHERE {column} < datetime('now', ?)",
            (f"-{days} days",),
        )
        conn.commit()
        print(f"[MT5_STATE_PRUNE] {table}: deleted {cur.rowcount} rows older than {days}d")
        total_deleted += max(0, cur.rowcount)

    if datetime.now(timezone.utc).weekday() == 5:  # Saturday: market closed
        before = DB.stat().st_size
        conn.execute("VACUUM")
        after = DB.stat().st_size
        print(f"[MT5_STATE_PRUNE] VACUUM reclaimed {(before - after) / 1e6:.0f} MB")
    conn.close()
    print(f"[MT5_STATE_PRUNE] done deleted_total={total_deleted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
