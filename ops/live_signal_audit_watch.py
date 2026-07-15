#!/usr/bin/env python3
"""Observe the live decision stream without affecting trading."""
from __future__ import annotations

import argparse
import json
import os
import signal
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

TRACKED_EVENTS = {
    "HTF_CONTEXT_REJECTED", "HTF_CONTEXT_CREATED", "M5_TRIGGER_REJECTED",
    "SETUP_SCORE_REJECTED", "STRATEGY_V1_DECISION", "NOT_QUALIFIED",
    "SETUP_CREATED", "M1_CANDLE_CHECKED", "M1_CONFIRMATION_REJECTED",
    "SETUP_CONFIRMED", "SETUP_CONFIRMED_FRESH", "SETUP_EXPIRED",
    "SETUP_EXPIRED_NO_1M_CONFIRMATION", "SETUP_INVALIDATED", "SETUP_CANCELLED",
    "EXECUTION_CLAIMED", "EXECUTION_CHECK_FAILED", "ORDER_SUBMITTED", "ORDER_SENT",
    "ORDER_ACCEPTED", "ORDER_FILLED", "ORDER_REJECTED", "DUPLICATE_SUPPRESSED",
    "MT5_DATA_FRESHNESS", "DATA_STALE", "DATA_MISSING_HTF_CONTEXT",
}
TERMINAL_EVENTS = {
    "SETUP_EXPIRED", "SETUP_EXPIRED_NO_1M_CONFIRMATION", "SETUP_INVALIDATED",
    "SETUP_CANCELLED", "EXECUTION_CHECK_FAILED", "ORDER_FILLED", "ORDER_REJECTED",
}
ORDER_EVENTS = {"ORDER_SUBMITTED", "ORDER_SENT", "ORDER_ACCEPTED", "ORDER_FILLED", "ORDER_REJECTED"}


def event_time(row: dict) -> float:
    raw = row.get("ts") or row.get("timestamp") or row.get("generated_at") or ""
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError):
        return time.time()


def atomic_json(path: Path, value: dict) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temp, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default="/opt/cipherfx_mt5/state/mt5_decision_audit.jsonl")
    parser.add_argument("--output-dir", default="/opt/cipherfx_mt5/audit_artifacts/live_signal_watch_20260713")
    parser.add_argument("--until-utc", default="2026-07-13T20:00:00+00:00")
    args = parser.parse_args()

    source = Path(args.source)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    events_path = output_dir / "events.jsonl"
    summary_path = output_dir / "summary.json"
    alerts_path = output_dir / "alerts.jsonl"
    stop_at = datetime.fromisoformat(args.until_utc).timestamp()
    started_at = time.time()
    running = True

    def stop(*_args):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    counts = Counter()
    symbol_counts = defaultdict(Counter)
    engine_counts = defaultdict(Counter)
    chains: dict[str, dict] = {}
    alerts: dict[str, dict] = {}
    offset = source.stat().st_size if source.exists() else 0

    def chain_key(row: dict) -> str:
        setup_id = str(row.get("setup_id") or row.get("idempotency_key") or "").strip()
        if setup_id:
            return setup_id
        symbol = str(row.get("symbol") or row.get("sym") or "UNKNOWN").upper()
        return f"unbound:{symbol}:{int(event_time(row) // 300)}"

    def record_alert(key: str, kind: str, chain: dict, now: float) -> None:
        alert_id = f"{kind}:{key}"
        if alert_id in alerts:
            return
        alert = {
            "alert_id": alert_id,
            "kind": kind,
            "setup_id": key,
            "symbol": chain.get("symbol"),
            "side": chain.get("side"),
            "engine": chain.get("engine"),
            "last_event": chain.get("last_event"),
            "last_event_at": chain.get("last_event_at"),
            "detected_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        }
        alerts[alert_id] = alert
        with alerts_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(alert, sort_keys=True) + "\n")

    while running and time.time() < stop_at:
        if source.exists():
            size = source.stat().st_size
            if size < offset:
                offset = 0
            if size > offset:
                with source.open("r", encoding="utf-8", errors="ignore") as handle:
                    handle.seek(offset)
                    lines = handle.readlines()
                    offset = handle.tell()
                with events_path.open("a", encoding="utf-8") as output:
                    for line in lines:
                        try:
                            row = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        event = str(row.get("event") or row.get("type") or "").upper()
                        if event not in TRACKED_EVENTS:
                            continue
                        ts = event_time(row)
                        if ts < started_at - 2:
                            continue
                        symbol = str(row.get("symbol") or row.get("sym") or "UNKNOWN").upper()
                        engine = str(row.get("engine") or "UNKNOWN")
                        counts[event] += 1
                        symbol_counts[symbol][event] += 1
                        engine_counts[engine][event] += 1
                        key = chain_key(row)
                        chain = chains.setdefault(key, {
                            "setup_id": key, "symbol": symbol,
                            "side": row.get("side") or row.get("direction") or "",
                            "engine": engine, "events": [], "created_at_epoch": None,
                            "confirmed_at_epoch": None, "order_sent_at_epoch": None,
                            "terminal": False,
                        })
                        chain["symbol"] = symbol
                        chain["side"] = row.get("side") or row.get("direction") or chain.get("side") or ""
                        chain["engine"] = engine if engine != "UNKNOWN" else chain.get("engine")
                        chain["last_event"] = event
                        chain["last_event_at"] = datetime.fromtimestamp(ts, timezone.utc).isoformat()
                        chain["events"].append({"event": event, "ts": chain["last_event_at"]})
                        chain["events"] = chain["events"][-30:]
                        if event == "SETUP_CREATED":
                            chain["created_at_epoch"] = ts
                        if event in {"SETUP_CONFIRMED", "SETUP_CONFIRMED_FRESH"}:
                            chain["confirmed_at_epoch"] = ts
                        if event in {"ORDER_SUBMITTED", "ORDER_SENT"}:
                            chain["order_sent_at_epoch"] = ts
                        if event in TERMINAL_EVENTS:
                            chain["terminal"] = True
                        output.write(json.dumps(row, sort_keys=True) + "\n")

        now = time.time()
        for key, chain in list(chains.items()):
            if chain.get("terminal"):
                continue
            confirmed = chain.get("confirmed_at_epoch")
            sent = chain.get("order_sent_at_epoch")
            created = chain.get("created_at_epoch")
            observed_events = {item["event"] for item in chain.get("events", [])}
            if sent and now - sent > 30 and not (observed_events & {"ORDER_ACCEPTED", "ORDER_FILLED", "ORDER_REJECTED"}):
                record_alert(key, "ORDER_SENT_NO_BROKER_OUTCOME_30S", chain, now)
            elif confirmed and now - confirmed > 10 and not (observed_events & ORDER_EVENTS):
                record_alert(key, "CONFIRMED_NO_EXECUTION_OUTCOME_10S", chain, now)
            elif created and now - created > 180 and not (observed_events & TERMINAL_EVENTS):
                record_alert(key, "SETUP_NO_TERMINAL_OUTCOME_180S", chain, now)

        summary = {
            "observer_only": True,
            "started_at": datetime.fromtimestamp(started_at, timezone.utc).isoformat(),
            "stop_at": datetime.fromtimestamp(stop_at, timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "source_offset": offset,
            "counts": dict(counts),
            "by_symbol": {key: dict(value) for key, value in symbol_counts.items()},
            "by_engine": {key: dict(value) for key, value in engine_counts.items()},
            "chains": chains,
            "alerts": list(alerts.values()),
        }
        atomic_json(summary_path, summary)
        time.sleep(1)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
