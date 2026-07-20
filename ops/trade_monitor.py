#!/usr/bin/env python3
"""Live trade monitor - emits one line per meaningful event so a watcher is
notified as things happen. Runs for 2 hours then exits. Read-only.

Emits on: proposals created (getting close), orders filled (SUCCESS),
rejections/blocks/errors (need attention). Every 10 minutes emits a summary
of activity + the current top "no trade" reasons so we can see how close the
engines are and what's holding entries back. Skips the high-volume monitor
noise (position/portfolio heartbeats)."""
import json
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

DB = "/opt/cipherfx_mt5/state/mt5_state.db"
RUN_SECONDS = 7200          # 2 hours
POLL_SECONDS = 45
SUMMARY_EVERY = 600         # 10 minutes

# Only actionable outcomes - not TRADE_PROPOSAL_CREATED, which re-fires every
# cycle for a persisting setup and gets duplicate-suppressed, creating noise.
# A fill is the success signal; rejections/blocks/errors need attention; a
# close (POSITION_MANAGEMENT_ACTION) reports how a trade resolved.
EMIT_EVENTS = {
    "ORDER_FILLED", "ORDER_REJECTED", "EXECUTION_REJECTED",
    "EXECUTION_BLOCKED", "RUNTIME_ERROR", "POSITION_MANAGEMENT_ERROR",
    "EXECUTION_DRY_RUN", "POSITION_MANAGEMENT_ACTION",
}


def emit(line):
    print(line, flush=True)


def reason_of(payload):
    try:
        d = json.loads(payload)
    except (TypeError, ValueError):
        return ""
    return str(d.get("reason") or d.get("error") or d.get("status") or "")[:120]


def main():
    start = time.monotonic()
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cur = conn.cursor()
    cursor_ts = datetime.now(timezone.utc).isoformat()
    last_summary = start
    emit(f"MONITOR START {cursor_ts} - watching for executions for 2h")

    while time.monotonic() - start < RUN_SECONDS:
        try:
            rows = cur.execute(
                "SELECT event_time, event_type, symbol, payload_json FROM platform_events "
                "WHERE event_time > ? ORDER BY event_time",
                (cursor_ts,),
            ).fetchall()
        except sqlite3.Error as exc:
            emit(f"MONITOR DB-ERROR {exc}")
            time.sleep(POLL_SECONDS)
            continue

        for event_time, event_type, symbol, payload in rows:
            cursor_ts = max(cursor_ts, event_time)
            if event_type not in EMIT_EVENTS:
                continue
            reason = reason_of(payload)
            tag = {
                "ORDER_FILLED": "EXECUTED",
                "EXECUTION_DRY_RUN": "DRY-RUN-FILL",
                "ORDER_REJECTED": "REJECTED",
                "EXECUTION_REJECTED": "REJECTED",
                "EXECUTION_BLOCKED": "BLOCKED",
                "RUNTIME_ERROR": "ERROR",
                "POSITION_MANAGEMENT_ERROR": "MGMT-ERROR",
                "POSITION_MANAGEMENT_ACTION": "CLOSED",
            }.get(event_type, event_type)
            emit(f"{event_time[11:19]} {tag} {symbol or ''} {reason}".rstrip())

        now = time.monotonic()
        if now - last_summary >= SUMMARY_EVERY:
            last_summary = now
            window_start = datetime.now(timezone.utc).isoformat()
            # ISO cutoff (stored event_time uses 'T'; SQLite datetime() uses a
            # space, and 'T' > ' ' makes the naive comparison match everything).
            window_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
            counts = dict(cur.execute(
                "SELECT event_type, COUNT(*) FROM platform_events "
                "WHERE event_time > ? GROUP BY event_type", (window_cutoff,)
            ).fetchall())
            no_trade = cur.execute(
                "SELECT payload_json FROM platform_events "
                "WHERE event_type='NO_TRADE_PROPOSAL' AND event_time > ?", (window_cutoff,)
            ).fetchall()
            reasons = Counter()
            for (pj,) in no_trade:
                try:
                    d = json.loads(pj)
                    r = d.get("report", {}).get("reason") or d.get("reason") or ""
                except (TypeError, ValueError):
                    r = ""
                if r:
                    reasons[str(r)[:40]] += 1
            top = ", ".join(f"{r}({n})" for r, n in reasons.most_common(4)) or "n/a"
            emit(
                f"SUMMARY 10min - proposals={counts.get('TRADE_PROPOSAL_CREATED',0)} "
                f"filled={counts.get('ORDER_FILLED',0)} rejected={counts.get('ORDER_REJECTED',0)} "
                f"blocked={counts.get('EXECUTION_BLOCKED',0)} evaluated={counts.get('ENGINE_EVALUATED',0)} "
                f"top_no_trade=[{top}]"
            )
        time.sleep(POLL_SECONDS)

    emit("MONITOR END - 2h window complete")
    conn.close()


if __name__ == "__main__":
    sys.exit(main())
