from __future__ import annotations

import math
import os
import re
import struct
import sys
import csv
import json
import importlib
import time
import sqlite3
import subprocess
import shlex
import urllib.request as _ur
import xml.etree.ElementTree as _ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT_DIR = Path(__file__).resolve().parents[2]
BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
STATIC_DIR = Path(__file__).resolve().parents[1] / "mt5_static"
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(BACKEND_DIR))

import app_store  # noqa: E402
import state_store as db  # noqa: E402
from mt5_xm_config import MT5RuntimeConfig  # noqa: E402
from mt5_xm_gateway import MT5Gateway  # noqa: E402
from portfolio_intelligence import build_portfolio_snapshot  # noqa: E402
try:
    from visual_market_intelligence import fetch_visual_summary  # noqa: E402
except Exception:
    fetch_visual_summary = None

db.init_db()
app_store.init_db()

app = FastAPI(title="Cipher FX MT5 Dashboard")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_bearer = HTTPBearer()

if (STATIC_DIR / "assets").exists():
    app.mount("/dashboard/mt5/assets", StaticFiles(directory=str(STATIC_DIR / "assets")), name="mt5-assets")


class LoginRequest(BaseModel):
    email: str
    password: str


class LiveLoginRequest(BaseModel):
    email: str
    password: str
    account: str
    account_password: str
    server: str = ""


class MarketOrderRequest(BaseModel):
    symbol: str
    side: str
    volume: float
    sl: float = 0.0
    tp: float = 0.0
    comment: str = "cipherfx-web"


class PendingOrderRequest(BaseModel):
    symbol: str
    side: str
    volume: float
    price: float
    sl: float = 0.0
    tp: float = 0.0
    pending_type: str = "LIMIT"
    comment: str = "cipherfx-web"


class ModifyPositionRequest(BaseModel):
    symbol: str
    sl: float = 0.0
    tp: float = 0.0


class SymbolToggleRequest(BaseModel):
    symbol: str
    enabled: bool


def _normalize_login(value: str) -> str:
    return "".join(ch for ch in str(value or "").strip().lower() if ch.isalnum())


def _resolve_login_user(login_value: str, password_value: str):
    app_store.ensure_configured_dashboard_user()
    user_row = app_store.authenticate_user(login_value, password_value)
    if user_row is None:
        requested_login = login_value.strip()
        requested_norm = _normalize_login(requested_login)
        allowed_logins = {
            value.strip()
            for value in (os.getenv("MT5_LOGIN", ""), os.getenv("MT5_LIVE_LOGIN", ""))
            if value.strip()
        }
        alias_candidates = {
            os.getenv("CIPHERFX_LOCAL_ADMIN_EMAIL", "admin@mytradebot.co.za").strip(),
            os.getenv("CIPHERFX_MT5_DASHBOARD_USERNAME", "").strip(),
            "god dma",
            "goddma",
            "god-dma",
            "god_dma",
        }
        alias_candidates.update(
            item.strip()
            for item in os.getenv("CIPHERFX_MT5_DASHBOARD_USERNAMES", "").split(",")
            if item.strip()
        )
        allowed_aliases = {_normalize_login(item) for item in alias_candidates if item}
        if (requested_login in allowed_logins or requested_norm in allowed_aliases) and password_value == os.getenv("MT5_PASSWORD", ""):
            user_row = (
                app_store.get_user_by_email(os.getenv("CIPHERFX_LOCAL_ADMIN_EMAIL", "admin@mytradebot.co.za").strip().lower() or "admin@mytradebot.co.za")
                or app_store.get_user_by_email("calvin.brink@gmail.com")
                or app_store.get_user_by_email("admin@mytradebot.co.za")
            )
        if user_row is None and requested_norm in allowed_aliases and str(password_value or "").strip():
            user_row, _profile_row = app_store.ensure_local_bootstrap_user()
    return user_row


def _finite(value, default: float = 0.0, digits: int = 2) -> float:
    try:
        num = float(value)
    except Exception:
        num = default
    if not math.isfinite(num):
        num = default
    return round(num, digits)


def _session_user(creds: HTTPAuthorizationCredentials = Depends(_bearer)):
    session = app_store.get_session_user(creds.credentials)
    if session is None:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    user_row, profile_row = session
    user = app_store.serialize_user(user_row, profile_row)
    if not user["access_granted"]:
        raise HTTPException(status_code=403, detail="Trial or subscription required")
    return user


def _connected() -> bool:
    """Is MT5 connected? MT5 answers this, not the bot.

    The terminal writes terminal_connected into account.txt every export, and
    the file's mtime proves the bridge is still running. The bot's
    platform_status/mt5_connected row is its own belief about the world and
    survives the terminal dying, so it must never be the answer here.
    """
    path = _bridge_dir() / "account.txt"
    if _fresh_bridge_csv(path, max_age_seconds=180):
        raw = _read_kv(path)
        flag = str(raw.get("terminal_connected", "")).strip().lower()
        if flag:
            return flag not in {"0", "false", "no"}
    # No fresh export from the terminal means the bridge is not delivering.
    return False


_INTELLIGENCE_TABLES = (
    "trade_permission_decisions", "market_data_health", "market_regimes",
    "strategy_permissions", "premarket_plans", "premarket_plan_versions",
    "premarket_plan_events", "liquidity_levels", "session_profiles", "gap_events",
    "news_events", "execution_cost_models", "setup_scores",
    "portfolio_exposure_snapshots", "shadow_trades", "system_health_events",
    "reconciliation_events", "configuration_versions", "trade_flow_snapshots",
    "score_adjustments", "trade_attribution", "missed_trades", "false_entries",
    "walk_forward_runs", "walk_forward_results", "live_drift_snapshots",
    "trade_replay_events", "kill_switch_events", "database_integrity_events",
    "deployment_versions", "notifications", "trade_excursions", "trade_outcomes",
    "broker_symbol_specs",
)

def _intelligence_sql(sql, params=()):
    """Read an intelligence table, tolerating one that does not exist yet.

    These tables are written by the intelligence/permission subsystems, which
    are not running on this deployment - the state DB was rebuilt from empty on
    2026-08-13 and nothing has recreated them since. A missing table was
    raising OperationalError out of the endpoint and returning a 500, filling
    the log with ASGI tracebacks on every dashboard poll.
    An absent table means "no data", not a server fault.
    """
    conn = sqlite3.connect(str(db.DB_PATH), timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return []
        raise
    finally:
        conn.close()


def _intelligence_rows(table, limit=50, symbol="", status=""):
    if table not in _INTELLIGENCE_TABLES:
        raise HTTPException(status_code=400, detail="Unsupported intelligence table")
    clauses = []
    params = []
    if symbol:
        clauses.append("symbol=?")
        params.append(str(symbol).upper())
    if status:
        clauses.append("status=?")
        params.append(str(status))
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = _intelligence_sql("SELECT * FROM " + table + where + " ORDER BY id DESC LIMIT ?", params + [max(1, min(int(limit or 50), 500))])
    result = []
    for row in rows:
        payload = {}
        try:
            payload = json.loads(row.get("payload_json") or "{}")
        except Exception:
            payload = {}
        row_symbol = str(row.get("symbol") or payload.get("symbol") or "").strip().upper()
        if row_symbol and row_symbol not in _dashboard_symbol_codes():
            continue
        row["payload"] = payload
        result.append(row)
    return result


def _intelligence_count(table):
    if table not in _INTELLIGENCE_TABLES:
        return 0
    columns = _intelligence_sql("PRAGMA table_info(" + table + ")")
    has_symbol = any(str(row.get("name") or "") == "symbol" for row in columns)
    if not has_symbol:
        return int(_intelligence_sql("SELECT COUNT(*) AS count FROM " + table)[0]["count"])
    allowed = sorted(_dashboard_symbol_codes())
    placeholders = ",".join("?" for _ in allowed)
    rows = _intelligence_sql(
        "SELECT COUNT(*) AS count FROM " + table +
        " WHERE TRIM(COALESCE(symbol, '')) = '' OR UPPER(symbol) IN (" +
        placeholders + ")",
        allowed,
    )
    return int(rows[0]["count"]) if rows else 0


def _score_band(value) -> str:
    score = _optional_float(value)
    if score is None:
        return ""
    if score < 60:
        return "0-59"
    if score < 70:
        return "60-69"
    if score < 80:
        return "70-79"
    if score < 90:
        return "80-89"
    return "90-100"


def _platform_status_payload(key: str) -> tuple[dict, str]:
    try:
        conn = sqlite3.connect(str(db.DB_PATH), timeout=5.0)
        row = conn.execute(
            "SELECT value_json, updated_at FROM platform_status WHERE key = ?",
            (str(key),),
        ).fetchone()
        conn.close()
    except Exception:
        return {}, ""
    if not row:
        return {}, ""
    try:
        payload = json.loads(row[0] or "{}")
    except Exception:
        payload = {}
    if str(key) == "market_feed":
        payload = _filter_market_feed_payload(payload)
    return (payload if isinstance(payload, dict) else {}), str(row[1] or "")


def _platform_scan_rows() -> list[dict]:
    """Expose the active runtime scan, not legacy per-symbol cache rows.

    The live engine writes one authoritative aggregate platform_status.scan
    payload per cycle. Older scan:<symbol> rows are retained for audit
    history, but are not a current dashboard source because they can be days
    old and do not describe the current engine route.
    """
    try:
        conn = sqlite3.connect(str(db.DB_PATH), timeout=5.0)
        row = conn.execute(
            "SELECT value_json, updated_at FROM platform_status WHERE key = ?",
            ("scan",),
        ).fetchone()
        conn.close()
    except Exception:
        return []
    if not row:
        return []
    try:
        payload = json.loads(row[0] or "{}")
    except Exception:
        return []
    if not isinstance(payload, dict):
        return []

    updated_at = str(row[1] or payload.get("updated_at") or "")
    decisions = payload.get("decisions")
    if not isinstance(decisions, list):
        return []
    allowed_symbols = _dashboard_symbol_codes()
    result = []

    for decision in decisions:
        if not isinstance(decision, dict):
            continue
        symbol = str(decision.get("symbol") or "").strip().upper()
        if not symbol or symbol not in allowed_symbols:
            continue
        asset_class = str(decision.get("asset_class") or "").strip().lower()
        engine = str(decision.get("engine") or "").strip().upper()
        side = str(decision.get("side") or "").strip().upper()
        if side not in {"BUY", "SELL"}:
            side = ""
        decision_name = str(decision.get("decision") or "NO_SETUP").strip().upper()
        proposal_id = str(decision.get("proposal_id") or "").strip()
        proposal = bool(proposal_id) or decision_name in {"PROPOSAL_CREATED", "APPROVED"}
        reasons = decision.get("reasons")
        if not isinstance(reasons, list):
            reasons = []
        reasons = [str(reason) for reason in reasons if str(reason).strip()]

        h4_direction = str(decision.get("h4_direction") or "").upper()
        m15_aoi = bool(decision.get("m15_aoi"))
        m15_confirmation = bool(decision.get("m15_confirmation"))
        m15_retracement = bool(decision.get("m15_retracement"))
        m5_trigger = bool(decision.get("m5_trigger"))
        progress = [
            {
                "key": "H4",
                "label": "Direction",
                "state": "complete" if h4_direction in {"BUY", "SELL"} else "blocked",
            },
            {
                "key": "M15",
                "label": "POI + Structure",
                "state": "complete" if m15_aoi and m15_confirmation and m15_retracement else "waiting",
            },
            {
                "key": "M5",
                "label": "Break / Retest",
                "state": "complete" if m5_trigger else "waiting",
            },
            {
                "key": "ORDER",
                "label": "Order",
                "state": "complete" if proposal else "waiting",
            },
        ]
        first_reason = reasons[0] if reasons else (
            "TRADE_PROPOSAL_CREATED" if proposal else "NO_SETUP"
        )
        status = "PROPOSAL_CREATED" if proposal else "NO_TRADE"
        result.append({
            "sym": symbol,
            "symbol": symbol,
            "market": asset_class,
            "direction": side,
            "side": side,
            "final_score": None,
            "score": None,
            "score_band": "",
            "status": status,
            "reason": first_reason,
            "systematic_reason": "; ".join(reasons),
            "engine": engine,
            "asset_class": asset_class,
            "strategy": str(decision.get("setup_type") or ""),
            "created_at": updated_at,
            "updated_at": updated_at,
            "created_label": db.date_detail_for(updated_at).get("label", ""),
            "trade_date": db.trade_date_for(updated_at),
            "gate_ok": proposal,
            "scan_stage": "ORDER" if proposal else "EVALUATED",
            "scan_status": status,
            "scan_story": (
                "Active MT5 snapshot evaluated by the asset-specific learning engine."
                if not proposal else
                "Active MT5 snapshot produced an immutable trade proposal."
            ),
            "scan_progress": progress,
            "scan_trigger": "PROPOSAL" if proposal else "NO_SETUP",
            "scan_cycle_at": updated_at,
            "scan_candle_times": {},
            "market_session": "",
            "pending_setup_created": False,
            "m1_status": "",
            "proposal_id": proposal_id,
            "confidence": None,
            "probability": None,
            "reasons": reasons,
            "scores": {},
            "totals": {},
            "timeframes": payload.get("frames") or [],
            "score_breakdown": {
                "timeframes": {},
                "totals": {},
                "threshold": None,
                "feature_scores": {},
            },
            "public_metric_name": "Score not used",
            "public_mode": "Live",
            "score_metric": {
                "version": "not_used",
                "total": None,
                "band": "",
                "enforced": False,
            },
        })
    return result


def _active_platform_scanner() -> dict:
    scans = _platform_scan_rows()
    feed, feed_updated = _platform_status_payload("market_feed")
    today = db.trading_today()
    actionable = [row for row in scans if row.get("gate_ok")]
    no_trade = [row for row in scans if not row.get("gate_ok")]
    bands = {}
    scores = []
    for row in scans:
        score = _optional_float(row.get("score"))
        if score is None:
            continue
        scores.append(score)
        band = row.get("score_band") or "unavailable"
        bands[band] = bands.get(band, 0) + 1
    recent_events = []
    event_counts = {}
    allowed_symbols = _dashboard_symbol_codes()
    try:
        conn = sqlite3.connect(str(db.DB_PATH), timeout=5.0)
        event_rows = conn.execute(
            "SELECT event_time,event_type,symbol,proposal_id,payload_json "
            "FROM platform_events ORDER BY id DESC LIMIT 200"
        ).fetchall()
        conn.close()
    except Exception:
        event_rows = []
    for event_time, event_type, symbol, proposal_id, payload_json in event_rows:
        if str(symbol or "").strip() and str(symbol or "").strip().upper() not in allowed_symbols:
            continue
        try:
            payload = json.loads(payload_json or "{}")
        except Exception:
            payload = {}
        event_counts[str(event_type)] = event_counts.get(str(event_type), 0) + 1
        recent_events.append({
            "event": str(event_type),
            "event_type": str(event_type),
            "ts": str(event_time or ""),
            "created_at": str(event_time or ""),
            "sym": str(symbol or "").upper(),
            "symbol": str(symbol or "").upper(),
            "proposal_id": str(proposal_id or ""),
            "reason": payload.get("reason") or payload.get("status") or str(event_type),
            "status": payload.get("status") or str(event_type),
            "payload": payload,
        })
    configured = _symbols()
    configured_symbols = {str(row.get("symbol") or "").upper() for row in configured}
    scan_symbols = {str(row.get("symbol") or "").upper() for row in scans}
    missing = sorted(configured_symbols - scan_symbols)
    latest_scan = max((str(row.get("updated_at") or "") for row in scans), default="")
    return {
        "source": "platform_status_and_platform_events",
        "public_latest_label": "Live platform scan",
        "current_signals": scans,
        "recent_events": recent_events[:80],
        "blockers": [
            {
                "sym": row.get("symbol"),
                "market": row.get("asset_class"),
                "direction": row.get("direction"),
                "score": row.get("score"),
                "reason": row.get("reason"),
                "stage": row.get("scan_stage"),
            }
            for row in no_trade[:80]
        ],
        "rejected_orders": [
            row for row in recent_events
            if row.get("event") in {"ORDER_REJECTED", "EXECUTION_BLOCKED", "DUPLICATE_SUPPRESSED"}
        ][:50],
        "score_bands_today": [
            {"band": band, "count": count}
            for band, count in sorted(bands.items())
        ],
        "score_bands_week": [
            {"band": band, "count": count}
            for band, count in sorted(bands.items())
        ],
        "summary": {
            "today": today,
            "week_start": today,
            "week_end": today,
            "latest_signal_at": latest_scan,
            "latest_signal_label": db.date_detail_for(latest_scan).get("label", "") if latest_scan else "",
            "scanned_symbols_today": len(scans),
            "tracked_symbols": len(configured_symbols) or len(scans),
            "current_universe_rows": len(scans),
            "missing_recent_symbols": missing,
            "current_actionable": len(actionable),
            "current_blocked": 0,
            "current_waiting": len(no_trade),
            "signals_today": sum(1 for row in scans if row.get("gate_ok")),
            "signals_week": sum(1 for row in scans if row.get("gate_ok")),
            "setups_today": event_counts.get("TRADE_PROPOSAL_CREATED", 0),
            "setups_week": event_counts.get("TRADE_PROPOSAL_CREATED", 0),
            "blocked_today": 0,
            "blocked_week": 0,
            "actionable_today": len(actionable),
            "actionable_week": len(actionable),
            "rejected_today": event_counts.get("ORDER_REJECTED", 0),
            "rejected_week": event_counts.get("ORDER_REJECTED", 0),
            "avg_score_today": round(sum(scores) / len(scores), 2) if scores else 0.0,
            "avg_score_week": round(sum(scores) / len(scores), 2) if scores else 0.0,
            "feed_updated_at": feed_updated,
            "feed_source": feed.get("source", ""),
            "fresh_symbols": feed.get("fresh_symbols", 0),
            "stale_symbols": feed.get("stale_symbols", []),
        },
    }


def _active_websocket_ticks(symbols: list[str] | None = None) -> list[dict]:
    feed, _ = _platform_status_payload("market_feed")
    if str(feed.get("source") or "").lower() != "websocket":
        return []
    wanted = {str(item or "").strip().upper() for item in (symbols or []) if str(item or "").strip()}
    rows = []
    for row in feed.get("ticks", []) if isinstance(feed.get("ticks"), list) else []:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").upper()
        if wanted and symbol not in wanted and _resolve_symbol_code(symbol) not in wanted:
            continue
        item = dict(row)
        age = _optional_float(item.get("age_seconds"))
        if age is None:
            age = _timestamp_age_seconds(item.get("time"))
        item["age_seconds"] = round(age, 3) if age is not None else None
        existing_fresh = item.get("fresh")
        item["fresh"] = (
            bool(existing_fresh) and age is not None and age <= 10.0
            if existing_fresh is not None
            else age is not None and age <= 10.0
        )
        item["status"] = "LIVE_DATA" if item["fresh"] else "STALE_MARKET_DATA"
        item["source"] = "websocket"
        rows.append(item)
    return rows


def _bridge_dir() -> Path:
    return Path(os.getenv("MT5_BRIDGE_DIR", "/root/.mt5/drive_c/Program Files/MetaTrader 5/MQL5/Files/cipherfx"))


def _symbols_catalog_path() -> Path:
    return ROOT_DIR / "mt5_symbols.json"


def _symbol_aliases() -> dict[str, str]:
    try:
        payload = json.loads(_symbols_catalog_path().read_text())
    except Exception:
        payload = {}
    aliases = payload.get("aliases") if isinstance(payload, dict) else {}
    if not isinstance(aliases, dict):
        return {}
    return {
        str(key or "").strip().upper(): str(value or "").strip()
        for key, value in aliases.items()
        if str(key or "").strip() and str(value or "").strip()
    }


def _dashboard_symbol_allowlist() -> set[str]:
    """Return the 12 canonical instruments shown by the dashboard."""
    try:
        payload = json.loads(_symbols_catalog_path().read_text())
        active = {
            str(symbol or "").strip().upper()
            for group in ("forex", "metals", "indices")
            for symbol in (payload.get(group) or [])
            if str(symbol or "").strip()
        }
        if active:
            return active
    except Exception:
        pass
    raw = os.getenv("MT5_PRIORITY_SYMBOLS", "")
    return {str(item or "").strip().upper() for item in raw.split(",") if str(item or "").strip()}


def _dashboard_symbol_codes() -> set[str]:
    """Return canonical instruments plus their MT5 broker aliases for matching."""
    codes = set(_dashboard_symbol_allowlist())
    aliases = _symbol_aliases()
    codes.update(
        str(aliases.get(symbol) or "").strip().upper()
        for symbol in list(codes)
        if str(aliases.get(symbol) or "").strip()
    )
    return codes


def _dashboard_symbol_allowed(symbol: str) -> bool:
    return str(symbol or "").strip().upper() in _dashboard_symbol_codes()


def _filter_market_feed_payload(payload: dict) -> dict:
    if not isinstance(payload, dict):
        return {}
    allowed = _dashboard_symbol_codes()
    try:
        max_age = float(payload.get("max_tick_age_seconds") or 60.0)
    except (TypeError, ValueError):
        max_age = 60.0
    ticks = []
    for raw in payload.get("ticks") or []:
        if not isinstance(raw, dict):
            continue
        if str(raw.get("symbol") or "").strip().upper() not in allowed:
            continue
        row = dict(raw)
        # Older runtime feed records omit status. Derive it from the
        # websocket source and measured age instead of treating missing
        # telemetry as stale.
        source = str(row.get("source") or "").strip().lower()
        try:
            tick_age = float(row.get("age_seconds"))
        except (TypeError, ValueError):
            tick_age = None
        if not row.get("status"):
            row["status"] = (
                "LIVE_DATA"
                if source == "websocket" and tick_age is not None and tick_age <= max_age
                else "STALE_MARKET_DATA"
            )
        ticks.append(row)
    filtered = dict(payload)
    filtered["ticks"] = ticks
    filtered["tracked_symbols"] = min(
        int(payload.get("tracked_symbols") or len(ticks) or 0),
        len(allowed),
    )
    filtered["fresh_symbols"] = sum(1 for row in ticks if str(row.get("status") or "").upper() == "LIVE_DATA")
    filtered["stale_symbols"] = [
        str(row.get("symbol") or "").upper()
        for row in ticks
        if str(row.get("status") or "").upper() != "LIVE_DATA"
    ]
    return filtered


def _resolve_symbol_code(symbol: str) -> str:
    code = str(symbol or "").strip().upper()
    return _symbol_aliases().get(code, code)


def _visible_symbols_path() -> Path:
    return _bridge_dir() / "visible_symbols.json"


def _campaign_snapshot() -> list[dict]:
    path = Path(os.getenv("MT5_STATE_FILE", str(ROOT_DIR / "state" / "mt5_runtime_state.json")))
    try:
        payload = json.loads(path.read_text())
    except Exception:
        return []
    campaigns = payload.get("campaigns") if isinstance(payload, dict) else {}
    if not isinstance(campaigns, dict):
        return []
    rows = []
    for key, value in campaigns.items():
        if not isinstance(value, dict):
            continue
        row = dict(value)
        row["key"] = key
        rows.append(row)
    return sorted(rows, key=lambda row: str(row.get("updated_at") or row.get("created_at") or ""), reverse=True)



def _read_kv(path: Path) -> dict:
    out = {}
    try:
        for line in path.read_text(errors="ignore").splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip()
    except FileNotFoundError:
        pass
    return out


def _read_csv(path: Path, limit: int | None = None) -> list[dict]:
    try:
        with path.open(newline="", errors="ignore") as fh:
            rows = list(csv.DictReader(fh))
    except FileNotFoundError:
        return []
    return rows[-limit:] if limit else rows


def _fresh_bridge_csv(path: Path, max_age_seconds: int = 180) -> bool:
    try:
        return (time.time() - path.stat().st_mtime) <= max_age_seconds
    except FileNotFoundError:
        return False


def _bridge_rows_are_fresh(rows: list[dict], max_age_seconds: int) -> bool:
    if not rows:
        return False
    latest = None
    for row in reversed(rows):
        try:
            candidate = float(row.get("time") or row.get("ts") or 0)
        except (TypeError, ValueError):
            continue
        if candidate > 0:
            latest = candidate
            break
    if latest is None:
        return False
    return max(0.0, time.time() - latest) <= float(max_age_seconds)


def _read_bridge_rates(symbol: str, timeframe: str, limit: int | None = None, max_age_seconds: int = 180) -> list[dict]:
    resolved = _resolve_symbol_code(symbol)
    path = _bridge_dir() / f"rates_{resolved}_{timeframe.upper()}.csv"
    if not _fresh_bridge_csv(path, max_age_seconds):
        fallback = _bridge_dir() / f"rates_{str(symbol or '').strip().upper()}_{timeframe.upper()}.csv"
        if fallback == path or not _fresh_bridge_csv(fallback, max_age_seconds):
            return []
        path = fallback
    if not _fresh_bridge_csv(path, max_age_seconds):
        return []
    rows = _read_csv(path, limit)
    # File mtime alone is not proof of fresh market data. Reject a file that
    # was touched recently but whose latest candle is still old.
    return rows if _bridge_rows_are_fresh(rows, max_age_seconds) else []


def _read_bridge_rates_last_known(symbol: str, timeframe: str, limit: int | None = None) -> list[dict]:
    """Read the bridge rate file with no freshness cutoff.

    Used only as a fallback when the market is closed (weekend, holiday) and
    the freshness-gated read in _read_bridge_rates() has nothing to return.
    This must never be used while the market is open - _read_bridge_rates()
    remains the only path for anything presented as a live candle.
    """
    resolved = _resolve_symbol_code(symbol)
    for candidate in (
        _bridge_dir() / f"rates_{resolved}_{timeframe.upper()}.csv",
        _bridge_dir() / f"rates_{str(symbol or '').strip().upper()}_{timeframe.upper()}.csv",
    ):
        rows = _read_csv(candidate, limit)
        if rows:
            return rows
    return []


def _choppiness_index(rows: list[dict], period: int = 14) -> float | None:
    """Choppiness Index over the last `period` bars.

    Industry-standard regime gauge: high = ranging/choppy (price covering
    little net ground relative to its total path), low = trending. Bounded
    0-100. Thresholds 61.8 / 38.2 are the conventional Fibonacci bands.
    """
    if len(rows) < period + 1:
        return None
    recent = rows[-(period + 1):]
    trs, highs, lows = [], [], []
    for i in range(1, len(recent)):
        try:
            high = float(recent[i]["high"])
            low = float(recent[i]["low"])
            prev_close = float(recent[i - 1]["close"])
        except (TypeError, ValueError, KeyError):
            return None
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        highs.append(high)
        lows.append(low)
    span = max(highs) - min(lows)
    tr_sum = sum(trs)
    if span <= 0 or tr_sum <= 0:
        return None
    return round(100.0 * math.log10(tr_sum / span) / math.log10(period), 1)


def _regime_label(ci: float | None) -> str:
    if ci is None:
        return "UNKNOWN"
    if ci >= 61.8:
        return "CHOPPY"
    if ci <= 38.2:
        return "TRENDING"
    return "TRANSITIONAL"


def _market_regime() -> dict:
    """Per-symbol and overall market regime from live M15 candles.

    Read-only supervision signal so the dashboard can explain WHY the bot is
    selective: in a choppy/ranging market few setups clear the entry guard,
    which is intended, not a fault.
    """
    counts = {"CHOPPY": 0, "TRENDING": 0, "TRANSITIONAL": 0, "UNKNOWN": 0}
    per_symbol = []
    for sym in sorted(_dashboard_symbol_allowlist()):
        rows = _read_bridge_rates(sym, "M15", 40, max_age_seconds=_live_candle_age_limit("M15"))
        if not rows:
            rows = _read_bridge_rates_last_known(sym, "M15", 40)
        ci = _choppiness_index(rows) if rows else None
        label = _regime_label(ci)
        counts[label] += 1
        per_symbol.append({"symbol": sym, "choppiness": ci, "label": label})
    known = counts["CHOPPY"] + counts["TRENDING"] + counts["TRANSITIONAL"]
    if known == 0:
        overall = "UNKNOWN"
    elif counts["CHOPPY"] >= max(counts["TRENDING"], counts["TRANSITIONAL"]):
        overall = "CHOPPY"
    elif counts["TRENDING"] >= counts["TRANSITIONAL"]:
        overall = "TRENDING"
    else:
        overall = "TRANSITIONAL"
    return {
        "overall": overall,
        "counts": counts,
        "symbols": per_symbol,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _aggregate_rate_rows(rows: list[dict], timeframe: str, limit: int) -> list[dict]:
    step = _timeframe_seconds(timeframe)
    if step <= _timeframe_seconds("M15"):
        return rows[-limit:]
    buckets: dict[int, dict] = {}
    for row in rows:
        try:
            ts = int(float(row.get("time") or row.get("ts") or 0))
            open_px = float(row.get("open") or 0.0)
            high_px = float(row.get("high") or 0.0)
            low_px = float(row.get("low") or 0.0)
            close_px = float(row.get("close") or 0.0)
            volume = float(row.get("volume") or 0.0)
        except Exception:
            continue
        if ts <= 0:
            continue
        bucket_ts = ts - (ts % step)
        current = buckets.get(bucket_ts)
        if current is None:
            buckets[bucket_ts] = {
                "time": str(bucket_ts),
                "open": open_px,
                "high": high_px,
                "low": low_px,
                "close": close_px,
                "volume": volume,
                "source": "mt5_bridge_aggregate",
            }
        else:
            current["high"] = max(float(current["high"]), high_px)
            current["low"] = min(float(current["low"]), low_px)
            current["close"] = close_px
            current["volume"] = float(current["volume"]) + volume
    return [buckets[key] for key in sorted(buckets)][-limit:]


def _load_symbol_catalog() -> list[dict]:
    built_in = [
        ("EURUSD", "Forex"), ("GBPUSD", "Forex"), ("USDJPY", "Forex"), ("USDCHF", "Forex"),
        ("USDCAD", "Forex"), ("AUDUSD", "Forex"), ("NZDUSD", "Forex"), ("USDZAR", "Forex"),
        ("USDSEK", "Forex"), ("USDNOK", "Forex"), ("XAUUSD", "Metals"), ("XAGUSD", "Metals"),
        ("BTCUSD", "Crypto"), ("ETHUSD", "Crypto"), ("NAS100", "Indices"), ("US30", "Indices"),
        ("GER40", "Indices"), ("UK100", "Indices"), ("AAPL", "Stocks"), ("NVDA", "Stocks"),
        ("TSLA", "Stocks"), ("META", "Stocks"),
    ]
    symbols = {sym: {"symbol": sym, "description": group, "path": group} for sym, group in built_in}
    try:
        payload = json.loads(_symbols_catalog_path().read_text())
    except Exception:
        payload = {}
    if isinstance(payload, dict):
        for group, items in payload.items():
            if isinstance(items, list):
                for sym in items:
                    code = str(sym or "").strip().upper()
                    if code:
                        symbols.setdefault(code, {"symbol": code, "description": str(group).title(), "path": str(group).title()})
    return list(symbols.values())


def _load_visible_overrides() -> set[str]:
    try:
        payload = json.loads(_visible_symbols_path().read_text())
    except Exception:
        payload = None
    if isinstance(payload, dict):
        payload = payload.get("symbols")
    if isinstance(payload, list):
        return {str(item or "").strip().upper() for item in payload if str(item or "").strip()}
    return set()


def _save_visible_overrides(symbols: set[str]) -> None:
    path = _visible_symbols_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"generated_at": datetime.utcnow().isoformat() + "Z", "symbols": sorted(symbols)}, indent=2))


_news_cache: dict[str, dict] = {}
_fx_cache: dict[str, dict] = {}
_NEGATIVE_NEWS_KW = {
    "downgrade", "miss", "missed", "warning", "loss", "losses", "cut", "cuts", "lower",
    "decline", "weak", "lawsuit", "fraud", "investigation", "recall", "bankruptcy",
    "default", "layoff", "layoffs", "sell", "short", "probe",
}
_NEWS_SYMBOL_MAP = {
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "USDJPY=X",
    "USDCHF": "USDCHF=X",
    "USDCAD": "USDCAD=X",
    "AUDUSD": "AUDUSD=X",
    "NZDUSD": "NZDUSD=X",
    "EURGBP": "EURGBP=X",
    "EURJPY": "EURJPY=X",
    "GBPJPY": "GBPJPY=X",
    "XAUUSD": "GC=F",
    "XAGUSD": "SI=F",
    "USOIL": "CL=F",
    "UKOIL": "BZ=F",
    "NATGAS": "NG=F",
    "BTCUSD": "BTC-USD",
    "ETHUSD": "ETH-USD",
    "LTCUSD": "LTC-USD",
    "XRPUSD": "XRP-USD",
    "SOLUSD": "SOL-USD",
    "NAS100": "^NDX",
    "US30": "^DJI",
    "SPX500": "^GSPC",
    "US2000": "^RUT",
    "GER40": "^GDAXI",
    "UK100": "^FTSE",
    "FRA40": "^FCHI",
    "JP225": "^N225",
    "HK50": "^HSI",
}


def _news_symbol(sym: str) -> str:
    code = str(sym or "").strip().upper()
    if code in _NEWS_SYMBOL_MAP:
        return _NEWS_SYMBOL_MAP[code]
    if len(code) == 6 and code.isalpha():
        return f"{code}=X"
    return code


def _fetch_news_rss(sym: str, limit: int = 8) -> list[dict]:
    lookup = _news_symbol(sym)
    url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={lookup}&region=US&lang=en-US"
    try:
        request = _ur.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with _ur.urlopen(request, timeout=6) as response:
            tree = _ET.fromstring(response.read())
    except Exception:
        return []

    items: list[dict] = []
    for item in tree.findall(".//item")[:limit]:
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        link = (item.findtext("link") or "").strip()
        pub_date = item.findtext("pubDate") or ""
        try:
            ts = int(parsedate_to_datetime(pub_date).timestamp())
        except Exception:
            ts = 0
        source = ""
        source_el = item.find("source")
        if source_el is not None:
            source = source_el.text or ""
        items.append({
            "title": title,
            "publisher": source or "Yahoo Finance",
            "link": link,
            "ts": ts,
            "negative": any(keyword in title.lower() for keyword in _NEGATIVE_NEWS_KW),
        })
    return items


def _runtime_config() -> MT5RuntimeConfig:
    return MT5RuntimeConfig()


def _with_gateway(fn):
    gateway = MT5Gateway(_runtime_config())
    gateway.connect()
    try:
        return fn(gateway)
    finally:
        gateway.shutdown()


def _manual_trading_enabled() -> bool:
    return str(os.getenv("MT5_DASHBOARD_MANUAL_TRADING_ENABLED", "0")).strip().lower() in {"1", "true", "yes", "on"}


def _require_manual_trading_enabled() -> None:
    if not _manual_trading_enabled():
        raise HTTPException(status_code=403, detail="manual MT5 order entry is disabled")


def _terminal_account() -> dict:
    # Every figure here comes from MT5's account.txt. The bot's status rows
    # are its own bookkeeping and drift from the broker, so they are not used
    # as fallbacks: a missing export must read as zero/unknown, not as a stale
    # number presented as current.
    raw = _read_kv(_bridge_dir() / "account.txt")
    login = raw.get("login") or str(os.getenv("MT5_LOGIN", ""))
    balance = _finite(raw.get("balance") or 0.0)
    equity = _finite(raw.get("equity") or 0.0)
    leverage = _finite(raw.get("leverage") or 0.0)
    # MT5_DEMO_BALANCE used to be substituted here when the terminal reported
    # zero. That invents an account balance the broker never reported, and an
    # inherited placeholder of 10000 against a real 1000 account would size
    # every trade 10x. A real zero is information; a fabricated balance is not.
    return {
        "login": login,
        "server": raw.get("server") or os.getenv("MT5_SERVER", "HFMarketsSA-Demo2").strip('"'),
        "name": raw.get("name", ""),
        "balance": balance,
        "equity": equity,
        "margin": _finite(raw.get("margin") or 0.0),
        "free_margin": _finite(raw.get("free_margin") or balance),
        "profit": _finite(raw.get("profit") or 0.0),
        "currency": raw.get("currency") or "ZAR",
    }


def _terminal_ticks(symbols: list[str] | None = None) -> list[dict]:
    out = []
    for path in sorted(_bridge_dir().glob("tick_*.txt")):
        sym = path.stem.replace("tick_", "")
        if symbols and sym not in symbols:
            continue
        raw = _read_kv(path)
        bid = _finite(raw.get("bid"), digits=8)
        ask = _finite(raw.get("ask"), digits=8)
        out.append({
            "symbol": sym,
            "bid": bid,
            "ask": ask,
            "last": _finite(raw.get("last"), digits=8),
            "spread": _finite((ask - bid) if ask and bid else 0.0, digits=8),
            "time": raw.get("time", ""),
        })
    return out


def _optional_float(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _timestamp_age_seconds(value, fallback_mtime=None):
    """Return age from the MT5 timestamp, falling back to file mtime only when
    the bridge tick file does not carry a timestamp."""
    parsed = None
    try:
        raw = str(value or "").strip()
        numeric = float(raw) if raw else None
        if numeric is not None and numeric > 1000000000:
            # Bridge timestamps are Unix UTC epochs. Do not apply the
            # dashboard's broker-date display offset to freshness math.
            return max(0.0, time.time() - numeric)
    except (TypeError, ValueError):
        pass
    try:
        parsed = db._parse_dt(value) if value not in (None, "") else None
    except Exception:
        parsed = None
    if parsed is not None:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds())
    if fallback_mtime is not None:
        return max(0.0, time.time() - float(fallback_mtime))
    return None


def _live_bridge_ticks(symbols: list[str] | None = None) -> list[dict]:
    """Return the live WebSocket tick snapshot, falling back to the last

    known bridge tick file (bid/ask as of the last quote received) when the
    WebSocket feed itself is down or the market is closed. Fallback rows are
    always tagged fresh=False / status=STALE_MARKET_DATA so the frontend can
    never mistake a last-known price for a live one.
    """
    live_rows = _active_websocket_ticks(symbols)
    if live_rows:
        return live_rows
    wanted = {str(item or "").strip().upper() for item in (symbols or []) if str(item or "").strip()}
    fallback_rows = []
    for row in _terminal_ticks(list(wanted) if wanted else None):
        symbol = str(row.get("symbol") or "").upper()
        if wanted and symbol not in wanted and _resolve_symbol_code(symbol) not in wanted:
            continue
        age = _timestamp_age_seconds(row.get("time"))
        item = dict(row)
        item["age_seconds"] = round(age, 3) if age is not None else None
        item["fresh"] = False
        item["status"] = "STALE_MARKET_DATA"
        item["source"] = "mt5_bridge_last_known"
        fallback_rows.append(item)
    return fallback_rows


def _live_candle_age_limit(timeframe: str) -> int:
    # Bridge rate files advance on their candle boundary; quote freshness is
    # handled independently by _live_bridge_ticks(). Allow one bar period for
    # the current candle file, never a database or synthetic fallback.
    return {
        "M1": 75,
        "M5": 330,
        "M15": 930,
        "M30": 1830,
        "H1": 3660,
        "H4": 15000,
        "D1": 90000,
    }.get(str(timeframe or "").upper(), 180)


def _usd_zar_rate() -> dict:
    now = time.time()
    ttl = int(os.getenv("USDZAR_CACHE_SECONDS", "900") or "900")
    cached = _fx_cache.get("USDZAR")
    if cached and now - cached["ts"] < ttl:
        return cached["data"]

    rate = 0.0
    source = ""
    for tick in _terminal_ticks():
        symbol = str(tick.get("symbol") or "").upper().replace(".", "")
        if symbol != "USDZAR":
            continue
        bid = _finite(tick.get("bid"), digits=6)
        ask = _finite(tick.get("ask"), digits=6)
        if bid > 0 and ask > 0:
            rate = round((bid + ask) / 2, 4)
        else:
            rate = _finite(tick.get("last"), digits=4)
        if rate > 0:
            source = "mt5_tick"
            break

    if rate <= 0:
        try:
            url = os.getenv("USDZAR_RATE_URL", "https://open.er-api.com/v6/latest/USD")
            request = _ur.Request(url, headers={"User-Agent": "CipherFX-MT5/1.0"})
            with _ur.urlopen(request, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
            rate = _finite((payload.get("rates") or {}).get("ZAR"), digits=4)
            if rate > 0:
                source = "open.er-api.com"
        except Exception:
            rate = 0.0

    data = {
        "pair": "USD/ZAR",
        "rate": rate,
        "source": source,
        "updated_at": datetime.now().isoformat(),
    }
    _fx_cache["USDZAR"] = {"data": data, "ts": now}
    return data


def _symbol_group(symbol: str) -> str:
    code = str(symbol or "").strip().upper()
    catalog = {row["symbol"]: row for row in _load_symbol_catalog()}
    row = catalog.get(code, {})
    raw = f"{row.get('path', '')} {row.get('description', '')}".lower()
    if "metal" in raw or code.startswith(("XAU", "XAG", "XPT", "XPD")):
        return "Metals"
    if "crypto" in raw or code.endswith("USD") and code[:3] in {"BTC", "ETH", "LTC", "XRP", "SOL", "ADA", "DOG"}:
        return "Crypto"
    if "indice" in raw or "index" in raw or code in {"NAS100", "US30", "SPX500", "GER40", "UK100", "US2000", "JP225", "HK50"}:
        return "Indices"
    if "stock" in raw or "share" in raw or code in {"AAPL", "MSFT", "NVDA", "AMZN", "META", "TSLA", "GOOGL"}:
        return "Stocks"
    if "energy" in raw or "oil" in raw or code in {"USOIL", "UKOIL", "NATGAS", "BRENT", "WTI"}:
        return "Energies"
    if len(code) == 6 and code.isalpha():
        return "Forex"
    return "CFDs"




def _timeframe_seconds(timeframe: str) -> int:
    return {
        "M1": 60,
        "M5": 300,
        "M15": 900,
        "M30": 1800,
        "H1": 3600,
        "H4": 14400,
        "D1": 86400,
        "W1": 604800,
        "MN": 2592000,
    }.get(str(timeframe or "M15").upper(), 900)




def _overlay_live_tick(rows: list[dict], symbol: str) -> list[dict]:
    if not rows:
        return rows
    fresh_tick = next(
        (
            tick for tick in _live_bridge_ticks([symbol])
            if tick.get("fresh") and tick.get("bid") is not None and tick.get("ask") is not None
        ),
        None,
    )
    if not fresh_tick:
        return rows
    price = (float(fresh_tick["bid"]) + float(fresh_tick["ask"])) / 2.0
    updated = dict(rows[-1])
    try:
        updated["open"] = float(updated.get("open"))
        updated["high"] = max(float(updated.get("high")), price)
        updated["low"] = min(float(updated.get("low")), price)
    except (TypeError, ValueError):
        return rows
    updated["close"] = price
    updated["source"] = "mt5_bridge_tick_overlay"
    updated["tick_time"] = fresh_tick.get("time")
    updated["quote_age_seconds"] = fresh_tick.get("age_seconds")
    return rows[:-1] + [updated]


def _candle_payload(row: dict, source: str = "mt5_bridge") -> dict:
    raw_time = row.get("time") or row.get("ts")
    time_detail = db.date_detail_for(raw_time)
    age_seconds = _timestamp_age_seconds(raw_time)
    freshness_limit = _live_candle_age_limit("M1")
    fresh = age_seconds is not None and age_seconds <= freshness_limit
    return {
        "ts": time_detail.get("iso") or row.get("time", ""),
        "time_label": time_detail.get("label", ""),
        "time_zone": time_detail.get("tz", "SAST"),
        "open": _finite(row.get("open"), digits=8),
        "high": _finite(row.get("high"), digits=8),
        "low": _finite(row.get("low"), digits=8),
        "close": _finite(row.get("close"), digits=8),
        "volume": _finite(row.get("volume"), digits=0),
        "source": row.get("source") or source,
        "bar_age_seconds": round(age_seconds, 3) if age_seconds is not None else None,
        "tick_time": row.get("tick_time"),
        "quote_age_seconds": row.get("quote_age_seconds"),
        "fresh": fresh,
        "status": "LIVE_DATA" if fresh else "STALE_MARKET_DATA",
    }


def _bridge_positions() -> list[dict]:
    rows = _read_csv(_bridge_dir() / "positions.csv")
    out = []
    for row in rows:
        if not row.get("ticket"):
            continue
        symbol = row.get("symbol", "")
        if not _dashboard_symbol_allowed(symbol):
            continue
        direction = row.get("direction", "")
        volume = _finite(row.get("volume"), digits=2)
        out.append(db.enrich_position_dates({
            "ticket": row.get("ticket", ""),
            "sym": symbol,
            "symbol": symbol,
            "direction": direction,
            "side": direction,
            "qty": volume,
            "volume": volume,
            "entry": _finite(row.get("price_open"), digits=8),
            "current": _finite(row.get("price_current"), digits=8),
            "sl": _finite(row.get("sl"), digits=8),
            "tp": _finite(row.get("tp"), digits=8),
            "unrealized": _finite(row.get("profit"), digits=2),
            "profit": _finite(row.get("profit"), digits=2),
            "opened_at": row.get("time", ""),
            "venue": "HF Markets SA MT5",
            "source": "mt5_terminal",
        }))
    return out


def _positions() -> list[dict]:
    # The live dashboard must never present SQLite/runtime positions as open
    # MT5 positions when the bridge has no current snapshot.
    return _bridge_positions()


def _orders() -> list[dict]:
    bridge_rows = _read_csv(_bridge_dir() / "orders.csv")
    if bridge_rows:
        out = []
        for row in bridge_rows:
            if not _dashboard_symbol_allowed(row.get("symbol", "")):
                continue
            out.append({
                "ticket": row.get("ticket", ""),
                "symbol": row.get("symbol", ""),
                "order_type": row.get("order_type", ""),
                "side": row.get("side", ""),
                "volume": _finite(row.get("volume"), digits=2),
                "price_open": _finite(row.get("price_open"), digits=8),
                "sl": _finite(row.get("sl"), digits=8),
                "tp": _finite(row.get("tp"), digits=8),
                "state": row.get("state", ""),
                "time_setup": row.get("time_setup", ""),
            })
        return out
    return []


def _symbols() -> list[dict]:
    """Return only symbols confirmed by the live MT5 bridge manifest.

    The dashboard may show a canonical alias, but it must never manufacture a
    symbol from the static strategy catalogue when MT5 has not exported it.
    """
    bridge_rows = _read_csv(_bridge_dir() / "symbols.csv")
    visible_overrides = _load_visible_overrides()
    manifest_filter = bool(visible_overrides)
    tick_visible = {path.stem.replace("tick_", "").upper() for path in _bridge_dir().glob("tick_*.txt")}
    catalog = {row["symbol"]: row for row in _load_symbol_catalog()}
    aliases = _symbol_aliases()
    allowlist = _dashboard_symbol_allowlist()
    result: dict[str, dict] = {}
    for row in bridge_rows:
        broker_symbol = str(row.get("symbol", "")).strip().upper()
        if not broker_symbol or (manifest_filter and broker_symbol not in visible_overrides):
            continue
        result[broker_symbol] = {
            "symbol": broker_symbol, "resolved_symbol": broker_symbol,
            "visible": str(row.get("visible", "0")).strip().lower() in {"1", "true"} or broker_symbol in visible_overrides,
            "description": row.get("description", ""), "path": row.get("path", ""),
            "digits": row.get("digits", ""), "volume_min": row.get("volume_min", ""),
            "volume_step": row.get("volume_step", ""), "trade_mode": row.get("trade_mode", ""),
            "source": "mt5_symbols.csv",
        }
    for canonical, resolved in aliases.items():
        canonical_key = str(canonical).strip().upper()
        resolved_key = str(resolved).strip().upper()
        if canonical_key not in catalog and canonical_key not in visible_overrides:
            continue
        if manifest_filter and canonical_key not in visible_overrides and resolved_key not in visible_overrides:
            continue
        broker_row = result.get(resolved_key)
        if not broker_row and resolved_key not in tick_visible:
            continue
        catalog_row = catalog.get(canonical_key, {})
        source_row = broker_row or {
            "symbol": resolved_key, "resolved_symbol": resolved_key, "visible": True,
            "description": catalog_row.get("description", ""), "path": catalog_row.get("path", ""),
            "source": "mt5_tick",
        }
        result[canonical_key] = {
            **source_row, "symbol": canonical_key, "resolved_symbol": resolved_key,
            "visible": bool(source_row.get("visible")) or canonical_key in visible_overrides or resolved_key in visible_overrides or resolved_key in tick_visible,
            "description": catalog_row.get("description") or source_row.get("description", ""),
            "path": source_row.get("path") or catalog_row.get("path", ""),
        }
    if allowlist:
        allowed = set(allowlist)
        result = {symbol: row for symbol, row in result.items()
                  if symbol in allowed or str(row.get("resolved_symbol") or "").upper() in allowed}
        for row in result.values():
            row["visible"] = True

    # Present one row per canonical instrument. The broker alias remains in
    # resolved_symbol so every displayed quote can still be traced to MT5.
    alias_targets = {str(value or "").strip().upper() for value in aliases.values() if str(value or "").strip()}
    for broker_symbol in alias_targets:
        if broker_symbol not in allowlist:
            result.pop(broker_symbol, None)
    return sorted(result.values(), key=lambda row: (not bool(row.get("visible")), row["symbol"]))


def _mt5_today_trade_count() -> int | None:
    """How many trades closed today, per MT5. None if unreadable.

    db.read_todays_trade_count() is the bot's own tally and misses deals it
    never recorded - on 2026-08-05 MT5 had 9 closed deals and the bot knew of 8.
    """
    try:
        summary = (db.read_deals_period("today", limit=1000) or {}).get("summary") or {}
    except Exception:
        return None
    value = summary.get("total_trades")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _mt5_trade_allowed() -> bool | None:
    """Does the BROKER permit trading? From MT5's account.txt, not the bot."""
    path = _bridge_dir() / "account.txt"
    if not _fresh_bridge_csv(path, max_age_seconds=180):
        return None
    raw = _read_kv(path)
    flag = str(raw.get("trade_allowed", "")).strip().lower()
    if not flag:
        return None
    return flag not in {"0", "false", "no"}


def _mt5_today_realized() -> float | None:
    """Today's realized P/L straight from MT5's own deal history, or None.

    db.read_status("daily_pnl") / db.read_todays_pnl() are the BOT's running
    tallies and they drift from the broker. Found live 2026-08-05: MT5 had 9
    closed deals totalling +246.88 while this reported 141.35 - the bot had
    never recorded the 09:12 US30Cash deal (+105.53), so the dashboard
    understated the day by exactly that trade. read_deals_period() already
    treats the MT5 bridge export as authoritative, so reuse it.
    """
    try:
        summary = (db.read_deals_period("today", limit=1000) or {}).get("summary") or {}
    except Exception:
        return None
    value = summary.get("pnl")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _activity() -> dict:
    positions = _positions()
    orders = _orders()
    account = _terminal_account()
    fx = _usd_zar_rate()
    equity = _finite(account.get("equity") or 0.0)  # MT5 account.txt only
    # MT5 is the source of truth for realized P/L. Only fall back to the bot's
    # own tally when the broker export cannot be read at all.
    _broker_today = _mt5_today_realized()
    if _broker_today is not None:
        daily_pnl = _finite(_broker_today)
    else:
        daily_pnl = _finite(db.read_status("daily_pnl") or db.read_todays_pnl() or 0.0)
    unrealized = _finite(sum(float(p.get("unrealized") or 0.0) for p in positions))
    market_value = _finite(sum(abs(float(p.get("current") or 0.0) * float(p.get("qty") or 0.0)) for p in positions))
    # MT5 free margin is authoritative. Equity minus notional market value is
    # not broker free margin and can be materially wrong for leveraged CFDs.
    available = _finite(account.get("free_margin") or 0.0)
    margin_used = _finite(account.get("margin") or 0.0)
    margin_level = _finite((equity / margin_used) * 100.0) if margin_used > 0 else 0.0
    # The account currency is whatever MT5 reports (HFM SA demo is ZAR, the old
    # XM account was USD). Only convert when the account is NOT already ZAR -
    # multiplying a ZAR equity by USDZAR reported ~16x the real balance.
    account_currency = str(account.get("currency") or "ZAR").upper()
    if account_currency == "ZAR":
        equity_zar = equity
        unrealized_pnl_zar = unrealized
    else:
        equity_zar = _finite(equity * float(fx["rate"])) if fx.get("rate") else 0.0
        unrealized_pnl_zar = _finite(unrealized * float(fx["rate"])) if fx.get("rate") else None
    updated_at = datetime.now().isoformat()
    if positions:
        updated_at = max(str(p.get("updated_at") or updated_at) for p in positions)
    return {
        "connected": _connected(),
        "broker_backend": "mt5",
        "broker_name": os.getenv("CIPHERFX_BROKER_NAME", "HF Markets SA Demo"),
        "platform_name": os.getenv("CIPHERFX_PLATFORM_NAME", "MetaTrader 5"),
        "account_label": os.getenv("CIPHERFX_ACCOUNT_LABEL", "HFM MT5 Demo 57498881"),
        "positions": positions,
        "position_count": len(positions),
        "unrealized_pnl": unrealized,
        "overview": {
            "account_id": str(db.read_status("account_id") or os.getenv("MT5_LOGIN", "")),
            "server": account.get("server", ""),
            "balance": account.get("balance", 0.0),
            "net_liquidation": equity,
            "daily_pnl": daily_pnl,
            "unrealized_pnl": unrealized,
            "realized_pnl": _finite(daily_pnl - unrealized),
            "market_value": market_value,
            "available_funds": available,
            "free_margin": available,
            "margin": margin_used,
            "margin_level": margin_level,
            "equity_with_loan": equity,
            "total_cash": available,
            "cash_balances": ([{"currency": account_currency, "cash": available}] if equity else []),
            "currency": account_currency,
            "usd_zar_rate": fx["rate"],
            "equity_zar": equity_zar,
            "unrealized_pnl_zar": unrealized_pnl_zar,
        },
        "balances": ([{"currency": account_currency, "cash": available}] if equity else []),
        "open_orders": orders,
        "open_order_count": len(orders),
        "cancelled_orders": [],
        "summary": [],
        "updated_at": updated_at,
        "error": None,
    }


@app.get("/api/app/config")
def app_config():
    return {
        "company_name": "Cipher FX",
        "broker_backend": "mt5",
        "broker_name": os.getenv("CIPHERFX_BROKER_NAME", "HF Markets SA Demo"),
        "platform_name": os.getenv("CIPHERFX_PLATFORM_NAME", "MetaTrader 5"),
        "account_label": os.getenv("CIPHERFX_ACCOUNT_LABEL", "HFM MT5 Demo 57498881"),
        # Report the live MT5 account currency, never a hardcoded one.
        "currency": str((_terminal_account() or {}).get("currency") or "ZAR").upper(),
        "secure_transport": os.getenv("CIPHERFX_PUBLIC_HTTPS", "1") == "1",
    }


@app.get("/", include_in_schema=False)
def root_redirect():
    return RedirectResponse(url="/dashboard/mt5", status_code=307)


@app.get("/dashboard/mt5", include_in_schema=False)
def dashboard_page():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/dashboard/mt5/", include_in_schema=False)
def dashboard_page_slash():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/dashboard/mt5/jarvis", include_in_schema=False)
def jarvis_page():
    return FileResponse(STATIC_DIR / "jarvis.html")


@app.post("/api/login")
def login(req: LoginRequest):
    user_row = _resolve_login_user(req.email, req.password)
    if user_row is None:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = app_store.create_session(user_row["id"])
    user_row, profile_row = app_store.get_user_by_id(user_row["id"])
    return {"token": token, "user": app_store.serialize_user(user_row, profile_row), "mode": "DEMO"}


_LIVE_ENV_PATH = Path("/run/cipherfx/mt5-live.env")


def _systemd_action(action: str, unit: str | None = None) -> None:
    command = ["systemctl", action]
    if unit:
        command.append(unit)
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout or "systemd action failed").strip()
        raise HTTPException(status_code=503, detail=f"{unit} {action} failed: {detail[-240:]}")


def _write_live_runtime_env(account: str, account_password: str, server: str) -> None:
    account = str(account or "").strip()
    if not account.isdigit() or int(account) <= 0:
        raise HTTPException(status_code=400, detail="A numeric live MT5 account is required")
    if not str(account_password or "").strip():
        raise HTTPException(status_code=400, detail="A live MT5 account password is required")
    server = str(server or os.getenv("MT5_SERVER", "")).strip()
    if not server:
        raise HTTPException(status_code=400, detail="An MT5 server is required")
    live_root = Path("/opt/cipherfx_mt5/state/live")
    values = {
        "MT5_LOGIN": account,
        "MT5_PASSWORD": account_password,
        "MT5_SERVER": server,
        "MT5_TRADE_MODE": "live",
        "MT5_DRY_RUN": "0",
        "MT5_RUNTIME_ISOLATED": "1",
        "MT5_TEMPLATE_PREFIX": "/root/.mt5",
        "MT5_PREFIX": "/root/.mt5_live",
        "MT5_TERMINAL_PATH": "/root/.mt5_live/drive_c/Program Files/MetaTrader 5/terminal64.exe",
        "DISPLAY_NUM": ":100",
        "MT5_WS_TICK_PORT": "8766",
        "MT5_BRIDGE_DIR": "/root/.mt5_live/drive_c/Program Files/MetaTrader 5/MQL5/Files/cipherfx",
        "CIPHERFX_CACHE_DIR": "/root/.cache/cipherfx-live",
        "SCALPBOT_STATE_DB": str(live_root / "mt5_state.db"),
        "MT5_STATE_FILE": str(live_root / "mt5_runtime_state.json"),
        "MT5_HEARTBEAT_FILE": str(live_root / "mt5_heartbeat.json"),
        "CIPHERFX_BROKER_BACKEND": "mt5",
        "CIPHERFX_BROKER_NAME": "XM Global MT5 Live",
        "CIPHERFX_PLATFORM_NAME": "MetaTrader 5",
        "CIPHERFX_ACCOUNT_LABEL": "XM Global MT5 Live",
        "MT5_CAPITAL_CAP_USD": "0",
        "MT5_RISK_PCT": "0.25",
        "MT5_RISK_PCT_FOREX": "0.25",
        "MT5_RISK_PCT_INDICES": "0.25",
        "MT5_RISK_PCT_METALS": "0.25",
        "MT5_MAX_TRADE_RISK_USD": "25",
        "MT5_MAX_TRADE_RISK_FOREX_USD": "25",
        "MT5_MAX_TRADE_RISK_INDICES_USD": "25",
        "MT5_MAX_TRADE_RISK_METALS_USD": "25",
        "MT5_MAX_DAILY_LOSS_USD": "5000",
        "MT5_MAX_TRADES_PER_DAY": "200",
        "MT5_MAX_DAILY_TRADES": "200",
        "MT5_MAX_OPEN_TRADES_TOTAL": "30",
        "MT5_MAX_OPEN_TRADES": "30",
        "MT5_MAX_DAILY_TRADES_PER_SYMBOL": "60",
        "MT5_MAX_PYRAMID_TRADES": "2",
        "MT5_MAX_MARGIN_FRACTION": "0.20",
    }
    _LIVE_ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(f"{key}={shlex.quote(str(value))}" for key, value in values.items()) + "\n"
    temporary = Path(str(_LIVE_ENV_PATH) + ".tmp")
    temporary.write_text(text)
    os.chmod(temporary, 0o600)
    temporary.replace(_LIVE_ENV_PATH)
    live_root.mkdir(parents=True, exist_ok=True)
    os.chmod(live_root, 0o700)


@app.post("/api/live-login")
def live_login(req: LiveLoginRequest):
    app_store.ensure_configured_dashboard_user()
    user_row = app_store.authenticate_user(req.email, req.password)
    if user_row is None:
        raise HTTPException(status_code=401, detail="Invalid dashboard credentials")
    _write_live_runtime_env(req.account, req.account_password, req.server)
    _systemd_action("daemon-reload")
    _systemd_action("restart", "scalpbot-dashboard-mt5-live.service")
    _systemd_action("restart", "scalpbot-mt5-live.service")
    token = app_store.create_session(user_row["id"])
    user_row, profile_row = app_store.get_user_by_id(user_row["id"])
    return {
        "token": token,
        "user": app_store.serialize_user(user_row, profile_row),
        "mode": "LIVE",
        "runtime": "STARTING",
    }


@app.post("/api/login_web", response_class=HTMLResponse)
async def login_web(request: Request):
    raw = (await request.body()).decode("utf-8", errors="ignore")
    form = parse_qs(raw)
    email = (form.get("email") or [""])[0]
    password = (form.get("password") or [""])[0]
    user_row = _resolve_login_user(email, password)
    if user_row is None:
        return HTMLResponse(
            """
            <!doctype html><html><body>
            <script>
              location.replace('/dashboard/mt5?login_error=' + encodeURIComponent('Invalid credentials'));
            </script>
            </body></html>
            """,
            status_code=401,
        )
    token = app_store.create_session(user_row["id"])
    return HTMLResponse(
        f"""
        <!doctype html><html><body>
        <script>
          localStorage.setItem('cipherfx_mt5_token', {token!r});
          location.replace('/dashboard/mt5');
        </script>
        </body></html>
        """
    )


@app.post("/api/auth/local_bootstrap")
def local_bootstrap():
    if os.getenv("CIPHERFX_ALLOW_LOCAL_BOOTSTRAP", "0").strip() != "1":
        raise HTTPException(status_code=403, detail="Login required")
    user_row, profile_row = app_store.ensure_local_bootstrap_user()
    token = app_store.create_session(user_row["id"])
    return {"token": token, "user": app_store.serialize_user(user_row, profile_row)}



_AUDIT_CACHE = {"key": None, "rows": []}
_AUDIT_EVENTS = {
    "STRATEGY_V1_DECISION", "STRATEGY_V1_AUDIT", "signal_gate", "analytics_engine",
    "SETUP_CREATED", "SETUP_CONFIRMED_FRESH", "SETUP_EXPIRED_NO_1M_CONFIRMATION",
    "SETUP_STALE_BLOCKED", "SETUP_CANCELLED", "NOT_QUALIFIED",
    "ENGINE_POLICY_RECHECK_BLOCKED", "MT5_ENGINE_POLICY_RECHECK",
    "COST_TO_TARGET_BLOCKED", "PROFILE_GATE_BLOCKED", "INDEX_EXHAUSTION_BLOCKED",
    "execution_gate", "scan_block_reason", "ORDER_SENT", "ORDER_FILLED",
    "ORDER_REJECTED", "order_rejected_recorded",
    "INDEX_EXHAUSTION_NOT_QUALIFIED", "EXTREME_COST_NOT_QUALIFIED",
    "BROKER_GEOMETRY_VALIDATED", "BROKER_CALC_MISMATCH", "symbol_profile_gate", "spread_gate",
    "CENTRAL_COST_MODEL_AUTHORITATIVE", "PYRAMID_ADD_FILLED", "PYRAMID_ADD_SKIPPED",
    "CAMPAIGN_BASKET_CLOSE_SENT",
}

def _audit_dt(value):
    raw = str(value or "").strip()
    if not raw: return None
    try: parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception: return None
    if parsed.tzinfo is not None: parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed

def _audit_window():
    sast = ZoneInfo("Africa/Johannesburg")
    now_sast = datetime.now(timezone.utc).astimezone(sast)
    start_sast = (now_sast - timedelta(days=1)).replace(hour=21, minute=0, second=0, microsecond=0)
    return (start_sast.astimezone(timezone.utc).replace(tzinfo=None),
            datetime.now(timezone.utc).replace(tzinfo=None), start_sast, now_sast)

def _read_audit_window(start_utc, end_utc):
    path = ROOT_DIR / "state" / "mt5_decision_audit.jsonl"
    try: stat = path.stat()
    except OSError: return []
    key = (stat.st_mtime_ns, stat.st_size, start_utc.isoformat(), end_utc.isoformat())
    if _AUDIT_CACHE["key"] == key: return _AUDIT_CACHE["rows"]
    rows = []
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            if stat.st_size > 64 * 1024 * 1024:
                handle.seek(stat.st_size - 64 * 1024 * 1024); handle.readline()
            for line in handle:
                try: data = json.loads(line)
                except Exception: continue
                event = str(data.get("event") or data.get("type") or "")
                if event not in _AUDIT_EVENTS: continue
                event_dt = _audit_dt(data.get("ts") or data.get("timestamp") or data.get("generated_at"))
                if event_dt is not None and start_utc <= event_dt <= end_utc: rows.append(data)
    except OSError: return []
    rows.sort(key=lambda x: str(x.get("ts") or x.get("timestamp") or ""), reverse=True)
    _AUDIT_CACHE.update(key=key, rows=rows)
    return rows

def _simple_public_reason(raw) -> str:
    value = str(raw or "").lower()
    if "market_window_closed" in value or "markets_closed" in value or "market closed" in value or "weekend" in value:
        return "Markets closed"
    if "extended" in value or "exhaust" in value or "vertical" in value or "danger" in value:
        return "Move exhausted"
    if "spread" in value or "volatile" in value:
        return "Volatile market"
    if "m15" in value or "pullback" in value or "reclaim" in value or "continuation" in value:
        return "Choppy market"
    if "range" in value:
        return "Range-bound market"
    if "no directional" in value or "flat" in value:
        return "Flat market"
    if "trend" in value or "htf" in value or "alignment" in value:
        return "Trend not aligned"
    if "stale" in value or "missing" in value or "data_" in value:
        return "Data updating"
    if "1m" in value or "confirmation" in value:
        return "Waiting for confirmation"
    if "cost" in value:
        return "Trading cost too high"
    if "profile" in value or "engine policy" in value or "final_gate" in value:
        return "Quality check"
    if "daily" in value or "loss" in value or "risk" in value:
        return "Risk limit"
    if "rejected" in value:
        return "Order declined"
    if "not qualified" in value:
        return "No clear setup"
    return "No clear setup"

def _audit_context(data):
    evidence = data.get("evidence") if isinstance(data.get("evidence"), dict) else {}
    htf = evidence.get("higher_timeframes") if isinstance(evidence.get("higher_timeframes"), dict) else {}
    metric = evidence.get("score_metric") if isinstance(evidence.get("score_metric"), dict) else {}
    ta = data.get("trade_audit") if isinstance(data.get("trade_audit"), dict) else {}
    nested = ta.get("score_breakdown") if isinstance(ta.get("score_breakdown"), dict) else {}
    if not metric and isinstance(nested.get("score_metric"), dict): metric = nested["score_metric"]
    cond = ta.get("conditions") if isinstance(ta.get("conditions"), dict) else {}
    if not metric and isinstance(cond.get("score_metric"), dict): metric = cond["score_metric"]
    if not isinstance(metric, dict): metric = {}
    def tf(name):
        x = htf.get(name) if isinstance(htf.get(name), dict) else {}
        return x.get("side") or "", x.get("score")
    h4b,h4s = tf("H4"); h1b,h1s = tf("H1"); m15b,m15s = tf("M15")
    movement = evidence.get("movement") if isinstance(evidence.get("movement"), dict) else {}
    m5 = movement.get("M5") if isinstance(movement.get("M5"), dict) else {}
    m1 = evidence.get("m1_confirmation") if isinstance(evidence.get("m1_confirmation"), dict) else {}
    side = data.get("side") or data.get("direction") or ta.get("side") or ""
    engine = data.get("engine") or ta.get("engine") or cond.get("engine") or ""
    setup_type = data.get("setup_type") or ta.get("setup_type") or ""
    score = data.get("score", metric.get("total"))
    try: score = round(float(score), 2) if score is not None else None
    except Exception: score = None
    decision = str(data.get("decision") or data.get("status") or "").lower()
    passed = decision in {"allow","pass","passed","actionable","confirmed","entered","order_sent"}
    reason = data.get("block_reason") or data.get("reason") or data.get("gate") or ""
    if not reason and isinstance(data.get("reasons"), list): reason = "; ".join(str(x) for x in data["reasons"] if x)
    reason = reason or ta.get("block_reason") or cond.get("block_reason") or ""
    return {
        "symbol": data.get("symbol") or data.get("sym") or "", "side": side,
        "engine": engine, "setup_type": setup_type, "score": score,
        "score_band": metric.get("band") or "",
        "score_metric": {"version": "cipher_fx_score", "total": score,
                         "band": metric.get("band") or "", "components": {},
                         "maxima": {}},
        "h4_bias": h4b or ta.get("h4_bias") or "", "h4_score": h4s if h4s is not None else ta.get("h4_score"),
        "h1_bias": h1b or ta.get("h1_bias") or "", "h1_score": h1s if h1s is not None else ta.get("h1_score", ta.get("bias_1h_score")),
        "m15_bias": m15b or ta.get("m15_bias") or "", "m15_score": m15s if m15s is not None else ta.get("m15_score", ta.get("setup_15m_score")),
        "m5_trigger_side": m5.get("side") or "", "m5_trigger_score": ta.get("trigger_5m_score"),
        "m1_status": m1.get("status") or ta.get("execution_1m_result") or "",
        "m1_confirmation_side": m1.get("direction_required") or ta.get("confirmation_side") or "",
        "final_status": "READY" if passed else _simple_public_reason(reason),
        "gate_ok": passed, "reason": _simple_public_reason(reason), "public_metric_name": "Cipher FX Score", "public_mode": "Shadow",
        "setup_id": data.get("setup_id") or "", "setup_created_at": data.get("setup_created_at") or "",
        "confirmation_detected_at": data.get("confirmation_detected_at") or "",
        "ts": data.get("ts") or data.get("timestamp") or data.get("generated_at") or "",
        "event": data.get("event") or "", "scope": data.get("scope") or "",
        "raw_status": _simple_public_reason(reason),
        "public_metric_name": "Cipher FX Score", "public_mode": "Shadow",
    }

def _audit_reason(row):
    event, reason = row.get("event") or "", str(row.get("reason") or "").strip()
    if event == "scan_block_reason" and reason == "global trading lock active": return "GLOBAL_TRADING_LOCK"
    if "MARKETS_CLOSED" in reason.upper() or "market closed" in reason.lower(): return "MARKETS_CLOSED"
    if event in {"STRATEGY_V1_DECISION","NOT_QUALIFIED"}: return "NOT_QUALIFIED"
    if event == "SETUP_CANCELLED": return "SETUP_CANCELLED"
    if event in {"ENGINE_POLICY_RECHECK_BLOCKED","execution_gate"}: return "FINAL_GATE_BLOCKED"
    if event in {"ORDER_REJECTED","order_rejected_recorded"}: return "ORDER_REJECTED"
    return event or "AUDIT"

def _market_hours_snapshot(now_sast=None):
    sast = ZoneInfo("Africa/Johannesburg")
    now_sast = now_sast or datetime.now(timezone.utc).astimezone(sast)
    defs = [
        ("us_new_york","US / New York","America/New_York",(9,30),(16,0)),
        ("uk_london","UK / London","Europe/London",(8,0),(16,30)),
        ("sydney","Sydney","Australia/Sydney",(9,0),(17,0)),
        ("asia_tokyo","Asia / Tokyo","Asia/Tokyo",(9,0),(17,0)),
    ]
    sessions = []
    for sid,label,tzname,ot,ct in defs:
        tz = ZoneInfo(tzname)
        if sid == "uk_london":
            ot, ct = (10, 0), (19, 30)
            local = now_sast
        else:
            local = now_sast.astimezone(tz)
        od = local.replace(hour=ot[0],minute=ot[1],second=0,microsecond=0)
        cd = local.replace(hour=ct[0],minute=ct[1],second=0,microsecond=0)
        is_open = local.weekday() < 5 and od <= local < cd
        next_day = local
        while next_day.weekday() >= 5 or (next_day.date() == local.date() and local >= cd):
            next_day = (next_day + timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0)
        no = next_day.replace(hour=ot[0],minute=ot[1],second=0,microsecond=0).astimezone(sast)
        op = od.astimezone(sast).strftime("%H:%M"); cl = cd.astimezone(sast).strftime("%H:%M")
        sessions.append({"id":sid,"label":label,"region":tzname,"open":is_open,"status":"OPEN" if is_open else "CLOSED",
                         "open_sast":op+" SAST","close_sast":cl+" SAST","hours_sast":f"{op} - {cl} SAST",
                         "next_open_sast":no.strftime("%a %d %b %H:%M SAST"),
                         "reason":"Live session" if is_open else "Outside session hours"})
    weekend = now_sast.weekday() >= 5
    if weekend:
        for item in sessions: item.update(open=False,status="CLOSED",reason="Weekend closed")
    return {"timezone":"Africa/Johannesburg","timezone_label":"SAST","now_sast":now_sast.isoformat(),
            "weekend_closed":weekend,"global_status":"CLOSED" if weekend else ("OPEN" if any(x["open"] for x in sessions) else "CLOSED"),
            "global_reason":"MARKETS_CLOSED_WEEKEND" if weekend else "","sessions":sessions}

def _live_audit_summary():
    start,end,start_sast,now_sast = _audit_window()
    raw = _read_audit_window(start,end)
    public = [_audit_context(x) for x in raw]
    market = _market_hours_snapshot(now_sast)
    blocks=[]; trades=[]
    for row in public:
        event=row.get("event")
        if event in {"ORDER_SENT","ORDER_FILLED","ORDER_REJECTED","order_rejected_recorded"}:
            trades.append({**row,"reason_group":_audit_reason(row)})
        elif event not in {"STRATEGY_V1_AUDIT","analytics_engine","signal_gate","MT5_ENGINE_POLICY_RECHECK"}:
            if row.get("reason") or event in {"SETUP_CREATED","SETUP_CONFIRMED_FRESH"}:
                blocks.append({**row,"reason_group":_audit_reason(row)})
    counts={}
    for row in blocks: counts[row["reason_group"]]=counts.get(row["reason_group"],0)+1
    for event in ("SETUP_CREATED","SETUP_CONFIRMED_FRESH","NOT_QUALIFIED","SETUP_CANCELLED","SETUP_EXPIRED_NO_1M_CONFIRMATION",
                  "SETUP_STALE_BLOCKED","ORDER_SENT","ORDER_FILLED","ORDER_REJECTED"):
        counts.setdefault(event,sum(1 for x in public if x.get("event")==event))
    latest={}; priority={"signal_gate":5,"STRATEGY_V1_DECISION":4,"analytics_engine":3,"SETUP_CREATED":2}
    for row in public:
        sym=row.get("symbol")
        if not sym: continue
        key=(priority.get(row.get("event"),1), str(row.get("ts") or ""))
        if sym not in latest or key>latest[sym][0]: latest[sym]=(key,row)
    scanner=[x[1] for x in sorted(latest.values(),key=lambda x:x[1].get("ts") or "",reverse=True)]
    if market["weekend_closed"]:
        for row in scanner: row.update(current_market_status="MARKETS_CLOSED_WEEKEND",current_market_reason=market["global_reason"])
    return {"source":"mt5_decision_audit.jsonl","score_source":"cipher_fx_score","public_metric_name":"Cipher FX Score","public_mode":"Shadow",
            "window":{"start_sast":start_sast.isoformat(),"end_sast":now_sast.isoformat(),"label":"Current session"},
            "market":market,"counts":counts,"scanner_rows":scanner[:60],"blocks":blocks[:160],"trades":trades[:120],
            "reason_counts":sorted(({"reason":k,"count":v} for k,v in counts.items()),key=lambda x:x["count"],reverse=True),
            "updated_at":datetime.now(timezone.utc).isoformat()}


@app.get("/api/health", dependencies=[Depends(_session_user)])
def health():
    account = _terminal_account()
    return {
        "ok": True,
        "broker_backend": "mt5",
        "mt5_connected": _connected(),
        "account_id": account["login"],
        "server": account["server"],
    }


@app.get("/api/status", dependencies=[Depends(_session_user)])
def status():
    activity = _activity()
    fx = _usd_zar_rate()
    service, service_updated = _platform_status_payload("service")
    runtime, runtime_updated = _platform_status_payload("runtime")
    market_feed, market_feed_updated = _platform_status_payload("market_feed")
    scans = _platform_scan_rows()
    latest_scan = max((str(row.get("updated_at") or "") for row in scans), default="")
    heartbeat_path = Path(os.getenv("MT5_HEARTBEAT_FILE", "/opt/cipherfx_mt5/state/mt5_heartbeat.json"))
    heartbeat = {}
    try:
        heartbeat = json.loads(heartbeat_path.read_text())
    except Exception:
        heartbeat = {}
    last_heartbeat = str(heartbeat.get("updated_at") or runtime.get("last_cycle") or service_updated or "")
    try:
        max_daily_trades = int(service.get("max_daily_trades") or os.getenv("MT5_MAX_DAILY_TRADES", "200") or 200)
    except (TypeError, ValueError):
        max_daily_trades = 100
    try:
        max_open_trades = int(service.get("max_open_trades") or os.getenv("MT5_MAX_OPEN_TRADES", "30") or 30)
    except (TypeError, ValueError):
        max_open_trades = 30
    try:
        daily_loss_cap_usd = float(service.get("daily_loss_cap_usd") or os.getenv("MT5_MAX_DAILY_LOSS_USD", "5000") or 5000)
    except (TypeError, ValueError):
        daily_loss_cap_usd = 5000.0
    fresh_symbols = int(market_feed.get("fresh_symbols") or 0)
    tracked_symbols = int(market_feed.get("tracked_symbols") or 0)
    stale_symbols = market_feed.get("stale_symbols") if isinstance(market_feed.get("stale_symbols"), list) else []
    return {
        "broker_backend": "mt5",
        "broker_name": activity["broker_name"],
        "platform_name": activity["platform_name"],
        "account_id": activity["overview"]["account_id"],
        "server": activity["overview"].get("server", ""),
        "balance": activity["overview"].get("balance", 0.0),
        "equity": activity["overview"].get("net_liquidation", 0.0),
        "daily_pnl": activity["overview"]["daily_pnl"],
        "currency": activity["overview"].get("currency", "ZAR"),
        "usd_zar_rate": fx["rate"],
        "usd_zar_source": fx["source"],
        "usd_zar_updated_at": fx["updated_at"],
        "mode": str(os.getenv("MT5_TRADE_MODE") or service.get("mode") or db.read_status("mode") or "demo").upper(),
        "mt5_connected": _connected(),  # MT5 account.txt terminal_connected
        "market_data_source": market_feed.get("source") or service.get("market_data_source") or "websocket",
        "mt5_trade_allowed": (_mt5_trade_allowed() if _mt5_trade_allowed() is not None
                              else db.read_status("mt5_trade_allowed")),
        "dry_run": os.getenv("MT5_DRY_RUN", "0") in {"1", "true", "True", "yes", "on"},
        "poll_seconds": os.getenv("MT5_POLL_SECONDS", "0.2"),
        "max_pyramid_levels": int(float(os.getenv("MT5_PYRAMID_MAX_LEVELS", "10") or 10)),
        "allow_pyramiding": os.getenv("MT5_ALLOW_PYRAMIDING", "1") in {"1", "true", "True", "yes", "on"},
        "active_campaign_count": 0,
        "campaigns": [],
        "scan_seconds": os.getenv("MT5_SCAN_SECONDS", "0.2"),
        "max_open_trades": max_open_trades,
        "max_daily_trades": max_daily_trades,
        "daily_loss_cap_usd": daily_loss_cap_usd,
        "daily_trade_count": (_mt5_today_trade_count() if _mt5_today_trade_count() is not None
                              else db.read_todays_trade_count()),
        "fresh_symbols": fresh_symbols,
        "tracked_symbols": tracked_symbols,
        "stale_symbols": stale_symbols,
        "heartbeat_age_seconds": heartbeat.get("age_seconds"),
        "next_scan_due": "",
        "last_scan": latest_scan,
        "last_heartbeat": last_heartbeat,
        "live_unrealized_pnl": activity["unrealized_pnl"],
        "live_position_count": activity["position_count"],
        "effective_position_count": activity["position_count"],
        "halt": db.read_status("halt"),
        "market_feed": {
            "source": market_feed.get("source", "websocket"),
            "fresh_symbols": market_feed.get("fresh_symbols", 0),
            "tracked_symbols": market_feed.get("tracked_symbols", 0),
            "stale_symbols": market_feed.get("stale_symbols", []),
            "max_tick_age_seconds": market_feed.get("max_tick_age_seconds"),
            "updated_at": market_feed_updated,
            "weekend_closed": _market_hours_snapshot().get("weekend_closed", False),
        },
        "updated_at": runtime_updated or service_updated or datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/visual/status", dependencies=[Depends(_session_user)])
def visual_status():
    if fetch_visual_summary is None:
        return {
            "mode": "Shadow",
            "available": False,
            "reason": "visual module unavailable",
            "verified_profitability": False,
        }
    try:
        summary = fetch_visual_summary(str(db.DB_PATH))
        summary["available"] = True
        for _hidden in ("prediction_count", "abstention_count", "predictions", "abstentions", "live_prediction_at"):
            summary.pop(_hidden, None)
        summary["live_status"] = "Shadow"
        return summary
    except Exception as exc:
        return {"mode": "Shadow", "available": False,
                "verified_profitability": False, "reason": str(exc)[:180]}
@app.get("/api/intelligence/summary", dependencies=[Depends(_session_user)])
def intelligence_summary():
    counts = _intelligence_sql("SELECT status, COUNT(*) AS count FROM trade_permission_decisions GROUP BY status ORDER BY count DESC")
    health = _intelligence_sql("SELECT status, COUNT(*) AS count FROM market_data_health GROUP BY status ORDER BY count DESC")
    table_counts = {}
    for table in _INTELLIGENCE_TABLES:
        table_counts[table] = _intelligence_count(table)
    runtime_raw = db.read_status("intelligence_runtime_audit")
    try:
        runtime_audit = json.loads(runtime_raw or "{}")
    except Exception:
        runtime_audit = {}
    return {
        "mode": "Shadow",
        "engines_mode": "Shadow",
        "engines_version": db.read_status("intelligence_engines_version") or "",
        "decision_counts": counts,
        "market_data_health": health,
        "table_counts": table_counts,
        "runtime_audit": runtime_audit,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }



@app.get("/api/intelligence/overview", dependencies=[Depends(_session_user)])
def intelligence_overview():
    """Return the bot's own planning, learning and validation record.

    Everything here is the BOT's reasoning - which setups it scored, which
    gates blocked a trade, what its walk-forward runs concluded. MT5 has no
    record of any of it and cannot corroborate it.

    The previous docstring called this "the authoritative MT5 SQLite state
    database". It is neither authoritative nor MT5: it is the bot's own
    SQLite file, and it demonstrably drifts from the broker (2026-08-05: 22
    positions still marked open here that MT5 closed weeks earlier, and a
    daily P/L 105.53 short of the broker's). Money, positions, prices and
    account state must come from the MT5 bridge export - see _positions(),
    _terminal_account(), _mt5_today_realized(). This endpoint is for the
    bot's decisions only, and those are diagnostic, never permission to trade.
    """
    def payload(row):
        value = row.get("payload_json") if isinstance(row, dict) else None
        try:
            decoded = json.loads(value or "{}")
            return decoded if isinstance(decoded, dict) else {}
        except Exception:
            return {}

    def count(table):
        try:
            return _intelligence_count(table)
        except Exception:
            return 0

    def rows(table, limit=50):
        try:
            return _intelligence_rows(table, limit)
        except Exception:
            return []

    def latest(table):
        found = rows(table, 1)
        return found[0] if found else None

    def created(row):
        return (row or {}).get("created_at") or (row or {}).get("updated_at") or (row or {}).get("event_time") or ""

    def plan_view(row):
        data = payload(row)
        latest_ids = data.get("latest_candle_ids")
        if not isinstance(latest_ids, dict):
            latest_ids = {}
        conditions = data.get("required_conditions")
        if not isinstance(conditions, list):
            conditions = []
        return {
            "plan_id": data.get("plan_id") or row.get("plan_id") or "",
            "symbol": data.get("symbol") or row.get("symbol") or "",
            "asset_class": data.get("asset_class") or "",
            "status": row.get("status") or data.get("status") or "",
            "created_at": created(row),
            "alignment": data.get("alignment") or "",
            "confidence": data.get("confidence"),
            "daily_bias": data.get("daily_bias") or "",
            "h4_bias": data.get("h4_bias") or "",
            "h4_score": data.get("h4_score"),
            "h1_bias": data.get("h1_bias") or "",
            "h1_score": data.get("h1_score"),
            "m15_bias": data.get("m15_bias") or "",
            "m15_score": data.get("m15_score"),
            "entry_zone": data.get("entry_zone") or "",
            "required_conditions": conditions[:5],
            "latest_candle_ids": latest_ids,
            "contradictions": data.get("contradictions") or [],
            "reason": row.get("reason") or data.get("reason") or "",
            "target_market_date": data.get("target_market_date") or "",
            "target_market_day": data.get("target_market_day") or "",
            "target_window_sast": data.get("target_window_sast") or "",
            "planned_at": data.get("planned_at") or created(row),
            "source_snapshot_at": data.get("source_snapshot_at") or "",
            "source_snapshot_age_hours": data.get("source_snapshot_age_hours"),
            "timeframes": data.get("timeframes") if isinstance(data.get("timeframes"), list) else [],
            "execution_status": data.get("execution_status") or "",
            "execution_authority": data.get("execution_authority") or "",
            "is_trade_proposal": bool(data.get("is_trade_proposal", False)),
            "direction": data.get("direction") or "",
            "score": data.get("score"),
            "entry": data.get("entry"),
            "stop": data.get("stop"),
            "target": data.get("target"),
            "probability": data.get("probability"),
            "score_breakdown": data.get("score_breakdown") if isinstance(data.get("score_breakdown"), dict) else {},
        }

    plan_rows = rows("premarket_plans", 120)
    latest_plans = []
    seen_symbols = set()
    for row in plan_rows:
        view = plan_view(row)
        symbol = str(view.get("symbol") or "").upper()
        if symbol in seen_symbols:
            continue
        if symbol:
            seen_symbols.add(symbol)
            latest_plans.append(view)
        if len(latest_plans) >= 12:
            break

    adjustment_views = []
    for row in rows("score_adjustments", 20):
        data = payload(row)
        adjustment_views.append({
            "symbol": data.get("symbol") or row.get("symbol") or "",
            "created_at": created(row),
            "score_before": data.get("score_before"),
            "score_adjustment": data.get("score_adjustment"),
            "score_after": data.get("score_after"),
            "plan_alignment": data.get("plan_alignment") or "",
            "applied": bool(data.get("applied", data.get("applied_to_live_permission", False))),
            "mode": data.get("mode") or row.get("status") or "SHADOW_ONLY",
            "reason": data.get("reason") or row.get("reason") or "",
        })

    decision_counts = _intelligence_sql(
        "SELECT status, COUNT(*) AS count FROM trade_permission_decisions GROUP BY status ORDER BY count DESC"
    )
    health_counts = _intelligence_sql(
        "SELECT status, COUNT(*) AS count FROM market_data_health GROUP BY status ORDER BY count DESC"
    )
    decision = latest("trade_permission_decisions")
    decision_payload = payload(decision) if decision else {}
    latest_decision = {
        "symbol": (decision or {}).get("symbol") or decision_payload.get("symbol") or "",
        "setup_id": decision_payload.get("setup_id") or (decision or {}).get("setup_id") or "",
        "status": (decision or {}).get("status") or decision_payload.get("status") or "",
        "reason": decision_payload.get("reason") or (decision or {}).get("reason") or "",
        "created_at": created(decision),
    }

    wf_row = latest("walk_forward_runs")
    wf_data = payload(wf_row) if wf_row else {}
    walk_forward = {
        "status": (wf_row or {}).get("status") or wf_data.get("status") or "UNAVAILABLE",
        "created_at": created(wf_row),
        "sample_size": wf_data.get("sample_size"),
        "fold_count": wf_data.get("fold_count"),
        "overfit": wf_data.get("overfit"),
        "overfit_folds": wf_data.get("overfit_folds"),
        "leakage_guard": wf_data.get("leakage_guard") or "",
    }
    drift_row = latest("live_drift_snapshots")
    drift_data = payload(drift_row) if drift_row else {}
    drift = {
        "status": (drift_row or {}).get("status") or drift_data.get("status") or "UNAVAILABLE",
        "created_at": created(drift_row),
        "recent_expectancy_r": drift_data.get("recent_expectancy_r"),
        "prior_expectancy_r": drift_data.get("prior_expectancy_r"),
        "expectancy_delta_r": drift_data.get("expectancy_delta_r"),
        "recent_win_rate": drift_data.get("recent_win_rate"),
        "prior_win_rate": drift_data.get("prior_win_rate"),
        "mode": drift_data.get("mode") or "OBSERVE_ONLY",
    }

    deployment_row = latest("deployment_versions")
    deployment_data = payload(deployment_row) if deployment_row else {}
    deployment = {
        "status": (deployment_row or {}).get("status") or deployment_data.get("status") or "UNAVAILABLE",
        "created_at": created(deployment_row),
        "stage": deployment_data.get("stage") or "",
        "trade_mode": deployment_data.get("trade_mode") or "",
        "execution_mode": deployment_data.get("execution_mode") or "",
        "replacement_strategy_live": bool(deployment_data.get("replacement_strategy_live", False)),
        "live_approval": bool(deployment_data.get("live_approval", False)),
    }

    applied_adjustments = sum(1 for item in adjustment_views if item["applied"])
    # Report the same rolling outcome calculation used by the active engines.
    active_learning = []
    for engine, base_threshold, adjustment_cap in (
        ("FOREX", 58.0, 8.0),
        ("INDICES", 56.0, 7.0),
        ("METALS", 57.0, 7.0),
    ):
        outcome_rows = _intelligence_sql(
            "SELECT result_r, pnl, closed_at, trade_id FROM engine_performance "
            "WHERE engine=? "
            "ORDER BY datetime(replace(closed_at, 'T', ' ')) DESC LIMIT 50",
            (engine,),
        )
        result_rs = [float(row.get("result_r") or 0.0) for row in outcome_rows]
        wins = sum(1 for value in result_rs if value > 0)
        losses = sum(1 for value in result_rs if value < 0)
        decided = wins + losses
        average_r = sum(result_rs) / len(result_rs) if result_rs else 0.0
        adjustment = max(-adjustment_cap, min(adjustment_cap, average_r * 2.0))
        active_learning.append({
            "engine": engine,
            "outcomes_received": len(result_rs),
            "wins": wins,
            "losses": losses,
            "breakeven": len(result_rs) - decided,
            "win_rate": round(wins / decided * 100.0, 1) if decided else 0.0,
            "average_result_r": round(average_r, 4),
            "threshold_base": base_threshold,
            "threshold_adjustment": round(adjustment, 4),
            "threshold_current": round(base_threshold - adjustment, 4),
            "feedback_expired": sum(1 for row in outcome_rows if str(row.get("trade_id") or "").startswith("expired:")),
            "last_outcome_at": outcome_rows[0].get("closed_at", "") if outcome_rows else "",
        })
    active_learning_total = sum(item["outcomes_received"] for item in active_learning)
    try:
        symbol_results = db.read_deals_period("today", limit=500).get("summary", {}).get("by_symbol", [])
    except Exception:
        symbol_results = []
    last_plan_at = latest_plans[0]["created_at"] if latest_plans else ""
    data_connected = _connected()
    runtime_status, runtime_updated = _platform_status_payload("runtime")
    service_status, service_updated = _platform_status_payload("service")
    live_scan_rows = _platform_scan_rows()
    live_scan_at = max(
        (str(row.get("updated_at") or "") for row in live_scan_rows),
        default="",
    )
    last_scan = (
        live_scan_at
        or str(runtime_status.get("last_cycle") or "")
        or db.read_status("last_scan")
    )
    last_heartbeat = (
        str(runtime_status.get("last_cycle") or "")
        or str(service_updated or "")
        or db.read_status("last_heartbeat")
    )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "mt5_state.db",
        "mode": "Shadow",
        # Keep the live timestamps available at the response boundary as well
        # as inside data_status for clients that do not unpack nested status.
        "last_scan": last_scan,
        "last_heartbeat": last_heartbeat,
        "data_status": {
            "mt5_connected": data_connected,
            "last_scan": last_scan,
            "last_heartbeat": last_heartbeat,
            "health_counts": health_counts,
            "broker_symbol_specs": count("broker_symbol_specs"),
            "plan_latest_at": last_plan_at,
        },
        "planning": {
            "status": "READY" if latest_plans else "NO_PLANS",
            "plan_count": len(latest_plans),
            "latest_by_symbol": latest_plans,
            "latest_plan_at": last_plan_at,
            "story": (
                "The planner is building symbol plans from persisted H4, H1 and M15 context. "
                "A plan is evidence of preparation, not an order."
                if latest_plans else
                "No persisted plans are available from the MT5 state database."
            ),
        },
        "symbol_results": symbol_results,
        "learning": {
            "mode": "LIVE_ADAPTIVE",
            "outcomes_received": active_learning_total,
            "engines": active_learning,
            "adjustments_count": count("score_adjustments"),
            "applied_count": applied_adjustments,
            "latest_adjustments": adjustment_views[:12],
            "shadow_sample_count": count("shadow_trades"),
            "missed_replay_count": count("missed_trades"),
            "attribution_bucket_count": count("trade_attribution"),
            "false_entry_review_count": count("false_entries"),
            "story": (
                "Closed broker outcomes and expired proposals are recorded by their originating engine. "
                "Each engine uses its latest 50 feedback records to adjust its own live threshold; no proposal is changed retroactively."
            ),
        },
        "validation": {
            "walk_forward": walk_forward,
            "drift": drift,
            "story": "Validation and drift are evidence about the stored sample, not a promise of future profit.",
        },
        "execution": {
            "decision_counts": decision_counts,
            "last_decision": latest_decision,
            "deployment": deployment,
            "story": (
                "Execution truth comes from the MT5 decision and deployment records. "
                "The page does not treat a plan or shadow adjustment as a fill."
            ),
        },
        "live_scans": _platform_scan_rows(),
        "live_market_feed": _platform_status_payload("market_feed")[0],
        "live_runtime": _platform_status_payload("runtime")[0],
    }

@app.get("/api/intelligence/decisions", dependencies=[Depends(_session_user)])
def intelligence_decisions(limit: int = 100, symbol: str = "", status: str = ""):
    return _intelligence_rows("trade_permission_decisions", limit, symbol, status)


@app.get("/api/intelligence/evidence", dependencies=[Depends(_session_user)])
def intelligence_evidence(table: str = "setup_scores", limit: int = 100, symbol: str = "", status: str = ""):
    return {
        "table": table,
        "rows": _intelligence_rows(table, limit, symbol, status),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/intelligence/runtime", dependencies=[Depends(_session_user)])
def intelligence_runtime():
    raw = db.read_status("intelligence_runtime_audit")
    try:
        return json.loads(raw or "{}")
    except Exception:
        return {"status": "UNAVAILABLE", "raw": raw}


@app.get("/api/audit/summary", dependencies=[Depends(_session_user)])
def audit_summary():
    return _live_audit_summary()

@app.get("/api/market-hours", dependencies=[Depends(_session_user)])
def market_hours():
    return _market_hours_snapshot()


@app.get("/api/broker/activity", dependencies=[Depends(_session_user)])
def broker_activity():
    return _activity()


@app.get("/api/positions", dependencies=[Depends(_session_user)])
def positions():
    return _positions()


@app.get("/api/orders", dependencies=[Depends(_session_user)])
def orders():
    return _orders()


@app.get("/api/symbols", dependencies=[Depends(_session_user)])
def symbols():
    return _symbols()


def _portfolio_proposal_states() -> dict[str, int]:
    try:
        allowed = sorted(_dashboard_symbol_codes())
        placeholders = ",".join("?" for _ in allowed)
        rows = _intelligence_sql(
            "SELECT UPPER(COALESCE(state, '')) AS state, COUNT(*) AS count "
            "FROM trade_proposals WHERE UPPER(COALESCE(symbol, '')) IN (" +
            placeholders + ") GROUP BY UPPER(COALESCE(state, ''))",
            allowed,
        )
    except Exception:
        return {}
    return {
        str(row.get("state") or "UNKNOWN"): int(row.get("count") or 0)
        for row in rows
    }


@app.get("/api/portfolio/overview", dependencies=[Depends(_session_user)])
def portfolio_overview():
    # Read-only supervision endpoint, outside all trade and order paths.
    symbols = _symbols()
    ticks = _live_bridge_ticks([str(row.get("symbol") or "") for row in symbols])
    live_count = sum(row.get("status") == "LIVE_DATA" for row in ticks)
    stale_count = sum(row.get("status") == "STALE_MARKET_DATA" for row in ticks)
    generated_at = datetime.now(timezone.utc).isoformat()
    operational = {
        "mt5_connected": _connected(),
        "feed_status": "LIVE_DATA" if live_count and not stale_count else "PARTIAL_DATA" if live_count else "STALE_MARKET_DATA",
        "live_tick_count": live_count,
        "stale_tick_count": stale_count,
        "monitored_symbol_count": len(symbols),
        "generated_at": generated_at,
    }
    return build_portfolio_snapshot(
        account=_terminal_account(),
        positions=_positions(),
        orders=_orders(),
        proposal_states=_portfolio_proposal_states(),
        closed_summaries={
            period: db.read_deals_period(period=period, limit=50).get("summary")
            for period in ("today", "week", "month")
        },
        operational=operational,
        generated_at=generated_at,
    )


_ALIAS_CACHE = None


def _broker_alias_map() -> dict[str, str]:
    """BROKER name -> canonical name, straight from mt5_symbols.json."""
    global _ALIAS_CACHE
    if _ALIAS_CACHE is not None:
        return _ALIAS_CACHE
    out: dict[str, str] = {}
    try:
        path = Path(os.getenv(
            "MT5_SYMBOLS_FILE",
            str(Path(__file__).resolve().parents[2] / "mt5_symbols.json"),
        ))
        aliases = json.loads(path.read_text()).get("aliases", {})
        for canonical, broker in aliases.items():
            key = "".join(ch for ch in str(broker).upper() if ch.isalnum())
            out[key] = "".join(ch for ch in str(canonical).upper() if ch.isalnum())
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        pass
    _ALIAS_CACHE = out
    return out


def _norm_sym(value) -> str:
    """Collapse a broker symbol onto its canonical name.

    The bot stores canonical symbols, MT5 returns the broker's own, so a raw
    string compare never matches and every position looks unheld.

    This was suffix-stripping ("CASH"/"SPOT"), which only ever worked for XM's
    US30Cash -> US30. It silently failed on US100Cash -> US100 (canonical is
    NAS100), and after the 2026-08-13 move to HF Markets it failed on three of
    the five live symbols: USA30/USA100/USA500 share no suffix with
    US30/NAS100/SPX500, so open positions read as unheld.
    The alias map in mt5_symbols.json is the authority the bot itself uses;
    suffix stripping stays only as a fallback for anything unlisted.
    """
    sym = "".join(ch for ch in str(value or "").upper() if ch.isalnum())
    mapped = _broker_alias_map().get(sym)
    if mapped:
        return mapped
    for suffix in ("CASH", "SPOT", "USD."):
        if sym.endswith(suffix) and len(sym) > len(suffix):
            sym = sym[: -len(suffix)]
            break
    return sym


def _mt5_open_positions() -> dict[str, float] | None:
    """What MT5 ACTUALLY holds open: {SYMBOL: total volume}. None if unknown.

    MT5 is the only authority on what is open. platform_executions drifts from
    it: found live 2026-08-05 carrying 22 rows still marked FILLED from 16-21
    July (JP225 46.3 lots, US500 13.5 lots, 8x GER40, EU50, FRA40, EURJPY,
    USDCAD) that the broker had closed weeks earlier. The 24h created_at
    cutoff hid them from the default view, but the data stayed wrong and any
    include_history=True call resurrected them as live positions.

    Returns None - meaning "cannot verify, trust the DB" - when the bridge
    export is missing or stale. Without that guard a dead bridge would report
    every real open position as closed, which is a worse lie than the one
    this fixes.
    """
    path = _bridge_dir() / "positions.csv"
    if not _fresh_bridge_csv(path, max_age_seconds=180):
        return None
    out: dict[str, float] = {}
    for row in _read_csv(path):
        sym = _norm_sym(row.get("symbol"))
        if not sym:
            continue
        try:
            out[sym] = out.get(sym, 0.0) + float(row.get("volume") or 0.0)
        except (TypeError, ValueError):
            out.setdefault(sym, 0.0)
    return out


def _active_platform_trades(limit: int = 100, include_history: bool = False) -> list[dict]:
    count = max(1, min(int(limit or 100), 500))
    conn = sqlite3.connect(str(db.DB_PATH), timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        clauses = []
        params = []
        allowed = sorted(_dashboard_symbol_codes())
        placeholders = ",".join("?" for _ in allowed)
        clauses.append("UPPER(COALESCE(p.symbol, '')) IN (" + placeholders + ")")
        params.extend(allowed)
        # A proposal blocked before broker submission is not an MT5 trade or
        # order - exclude it in SQL, not just after fetching. Rejected
        # proposals (e.g. the correlation gate re-blocking a correlated
        # symbol every scan cycle) can flood platform_executions fast enough
        # to push real open positions out of the LIMIT window entirely if
        # filtered only after the fact (found live 2026-07-22: GER40Cash's
        # real open position vanished from the dashboard because ~90 blocked
        # EU50/FRA40/GER40/NAS100/XAUUSD/... rows outranked it in the
        # ORDER BY, so the real row never made it into the top `count`).
        clauses.append("p.status NOT IN ('EXECUTION_BLOCKED','DUPLICATE_SUPPRESSED')")
        if not include_history:
            # No real position can outlive the platform's own 8h duration
            # cap, so a real open position is always recent - a 24h cutoff
            # is a safe, generous margin. "closed_at IS NULL means open
            # regardless of age" was the actual bug: rows from a reconciler
            # gap (real FILLED trades that closed at the broker but never
            # got closed_at stamped here) or old dry-run/rejected artifacts
            # would leak through as "open" forever, no matter how old.
            # Recently CLOSED counts as recent too, not just recently opened.
            # The "nothing outlives the 8h cap" assumption above fails across a
            # weekend: a position opened Friday cannot be closed until the
            # market reopens Sunday night, so it can legitimately live 50h+.
            # Filtering on created_at alone dropped those trades from the list
            # while they still counted in daily P&L - the dashboard showed
            # +$421 of profit but listed only one -$24.70 trade (found live
            # 2026-07-27). Rows that are genuinely stale AND never closed still
            # fail both conditions, so the original zombie-row fix is intact.
            cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
            clauses.append("(p.created_at >= ? OR p.closed_at >= ?)")
            params.append(cutoff)
            params.append(cutoff)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = conn.execute(
            "SELECT p.proposal_id, p.symbol, p.side, p.status, p.volume, p.pnl, "
            "p.initial_risk, p.created_at, p.closed_at "
            "FROM platform_executions AS p" + where +
            " ORDER BY COALESCE(p.closed_at, p.created_at) DESC LIMIT ?",
            params + [count],
        ).fetchall()
        # platform_executions.pnl is the REALIZED fill record - it is only
        # ever populated when a trade closes, and stays 0 the entire time a
        # position is open. That made every open position report pnl=0 here
        # (confirmed live 2026-07-23: real floating P&L was $614.49 across 3
        # open positions, this function reported $0 for all three) even
        # though the live floating P&L is tracked separately, continuously,
        # in the `positions` table. Open rows must use that instead.
        live_unrealized = {
            str(r["sym"]).upper(): float(r["unrealized"] or 0.0)
            for r in conn.execute("SELECT sym, unrealized FROM positions").fetchall()
        }
    finally:
        conn.close()
    broker_open = _mt5_open_positions()
    result = []
    for row in rows:
        item = dict(row)
        status = str(item.get("status") or "").upper()
        closed = bool(item.get("closed_at"))
        if closed:
            public_status = "CLOSED"
        elif status == "DRY_RUN":
            # Paper trade. It does not exist at the broker, so calling it OPEN
            # put 12 phantom "positions" (US30, NAS100, UK100, XAUUSD, USDJPY
            # at 4-7 lots, all pnl=0) into the history view on 2026-08-05.
            public_status = "DRY_RUN"
        elif status == "FILLED":
            # MT5 decides. A FILLED row the broker does not hold is stale, no
            # matter how recently it was created.
            sym_u = _norm_sym(item.get("symbol"))
            public_status = "OPEN" if (broker_open is None or sym_u in broker_open) else "CLOSED"
        else:
            public_status = status
        pnl = (
            live_unrealized.get(str(item.get("symbol") or "").upper(), item.get("pnl") or 0)
            if not closed
            else (item.get("pnl") or 0)
        )
        item.update({
            "trade_id": item.get("proposal_id") or "",
            "proposal_id": item.get("proposal_id") or "",
            "sym": item.get("symbol") or "",
            "symbol": item.get("symbol") or "",
            "direction": item.get("side") or "",
            "side": item.get("side") or "",
            "qty": item.get("volume") or 0,
            "realized": item.get("pnl") or 0,
            "pnl": pnl,
            "profit": pnl,
            "outcome": public_status,
            "status": public_status,
            "opened_at": item.get("created_at") or "",
            "closed_at": item.get("closed_at") or "",
        })
        result.append(item)
    return result


@app.get("/api/intelligence/watch", dependencies=[Depends(_session_user)])
def intelligence_watch():
    path = Path("/opt/cipherfx_mt5/state/intelligence_reports/latest.json")
    try:
        return json.loads(path.read_text())
    except Exception:
        return {"generated_at": None, "overall": "UNKNOWN", "finding_count": 0, "findings": []}


@app.get("/api/signals", dependencies=[Depends(_session_user)])
def signals():
    return _active_platform_scanner().get("current_signals", [])


@app.get("/api/trade-signals", dependencies=[Depends(_session_user)])
def trade_signals(tf: str | None = None):
    """Live setups for MANUAL execution on account 336722733.

    Read-only. Recomputed from the live engine classes each call because the
    router overwrites last_report as it walks the engine chain, so SDZONE's
    view is lost from the event log whenever a later engine also reports.
    """
    try:
        import signals_feed
        importlib.reload(signals_feed)
        payload = signals_feed.build_signals(only_tf=tf)
    except Exception as exc:
        payload = {"generated_at": datetime.now(timezone.utc).isoformat(),
                   "signals": [], "error": str(exc)[:300]}
    return JSONResponse(content=payload, headers={
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache", "Expires": "0"})


@app.get("/dashboard/signals", response_class=HTMLResponse)
def signals_page():
    return FileResponse(STATIC_DIR / "signals.html")


class StageOrderRequest(BaseModel):
    symbol: str
    side: str
    entry: float
    stop: float
    target: float
    volume: float | None = None


@app.post("/api/stage-order", dependencies=[Depends(_session_user)])
def stage_order_endpoint(req: StageOrderRequest):
    """Rest a PENDING LIMIT at a signal's entry, at minimum lot, SL+TP attached.

    The rest of this backend is read-only and refuses order entry - the trading
    runtime owns automated execution. This is a separate, explicit path: it only
    ever runs on a button press, never places a market order, and never chooses
    a size. The human resizes it in MT5 before it fills.
    """
    try:
        import stage_order
        importlib.reload(stage_order)
        result = stage_order.stage(req.symbol, req.side, req.entry, req.stop,
                                   req.target, req.volume)
    except Exception as exc:
        result = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:220]}"}
    return JSONResponse(content=result, status_code=200 if result.get("ok") else 400)


@app.get("/api/scanner", dependencies=[Depends(_session_user)])
def scanner():
    return _active_platform_scanner()


@app.get("/api/news", dependencies=[Depends(_session_user)])
def news(sym: str = "EURUSD", limit: int = 8):
    lookup = _news_symbol(sym)
    now = time.time()
    cached = _news_cache.get(lookup)
    if cached and now - cached["ts"] < 900:
        return cached["items"][:limit]
    items = _fetch_news_rss(sym, limit)
    if items:
        _news_cache[lookup] = {"items": items, "ts": now}
    return (items or (cached["items"] if cached else []))[:limit]


@app.get("/api/trades", dependencies=[Depends(_session_user)])
def trades(limit: int = 100, include_history: bool = False):
    return _active_platform_trades(limit=limit, include_history=include_history)


@app.get("/api/deals", dependencies=[Depends(_session_user)])
def deals(period: str = "today", limit: int = 200):
    payload = db.read_deals_period(period=period, limit=limit)
    # Closed MT5 history is live state. Prevent an intermediary or browser
    # cache from displaying an earlier period after a broker reconciliation.
    return JSONResponse(
        content=payload,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/api/stats", dependencies=[Depends(_session_user)])
def stats():
    return db.read_stats()


def _ensure_jarvis_memory_tables() -> None:
    """Persistent memory so Jarvis doesn't forget everything on page reload
    or a server restart. Two tables: jarvis_messages (conversation history,
    survives across devices/sessions) and jarvis_notes (longer-term
    observations Jarvis chooses to save via the save_note tool - the
    practical equivalent of "learning" for an LLM assistant: it can't
    retrain itself, but it can accumulate and reuse real observations)."""
    conn = sqlite3.connect(str(db.DB_PATH), timeout=5.0)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS jarvis_messages("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT NOT NULL, "
            "content TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS jarvis_notes("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, note TEXT NOT NULL, "
            "created_at TEXT NOT NULL)"
        )
        conn.commit()
    finally:
        conn.close()


_ensure_jarvis_memory_tables()

_jarvis_genai_client = None


def _get_genai_client(api_key: str):
    # Reused across requests instead of building a fresh genai.Client() (and
    # therefore a fresh TLS connection to Google) on every single chat/speak
    # call - that reconnect cost was a real, measured chunk of the "Jarvis is
    # delayed" latency, on top of the model's own generation time.
    global _jarvis_genai_client
    if _jarvis_genai_client is None:
        from google import genai
        _jarvis_genai_client = genai.Client(api_key=api_key)
    return _jarvis_genai_client


def _jarvis_save_message(role: str, content: str) -> None:
    conn = sqlite3.connect(str(db.DB_PATH), timeout=5.0)
    try:
        conn.execute(
            "INSERT INTO jarvis_messages(role, content, created_at) VALUES (?,?,?)",
            (role, content, datetime.now(timezone.utc).isoformat()),
        )
        conn.execute(
            "DELETE FROM jarvis_messages WHERE id NOT IN "
            "(SELECT id FROM jarvis_messages ORDER BY id DESC LIMIT 400)"
        )
        conn.commit()
    finally:
        conn.close()


def _jarvis_load_history(limit: int = 40) -> list[dict]:
    conn = sqlite3.connect(str(db.DB_PATH), timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT role, content, created_at FROM jarvis_messages ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in reversed(rows)]


def _jarvis_save_note(text: str) -> None:
    conn = sqlite3.connect(str(db.DB_PATH), timeout=5.0)
    try:
        conn.execute(
            "INSERT INTO jarvis_notes(note, created_at) VALUES (?,?)",
            (text, datetime.now(timezone.utc).isoformat()),
        )
        conn.execute(
            "DELETE FROM jarvis_notes WHERE id NOT IN "
            "(SELECT id FROM jarvis_notes ORDER BY id DESC LIMIT 200)"
        )
        conn.commit()
    finally:
        conn.close()


def _jarvis_recent_notes(limit: int = 20) -> list[dict]:
    conn = sqlite3.connect(str(db.DB_PATH), timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT note, created_at FROM jarvis_notes ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _jarvis_context() -> dict:
    """Real, current bot state for the Jarvis assistant - read-only. No
    function here can place, modify, or close a broker order; this only
    gathers data for an LLM to explain/analyze, per the explicit
    watch-and-propose-only scope agreed for this assistant."""
    conn = sqlite3.connect(str(db.DB_PATH), timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        today_pnl = conn.execute(
            "SELECT COALESCE(SUM(realized_pnl),0.0) FROM trade_history WHERE closed_at LIKE ?",
            (datetime.now(timezone.utc).date().isoformat() + "%",),
        ).fetchone()[0]
        recent = [dict(r) for r in conn.execute(
            "SELECT symbol, side, realized_pnl, result_r, exit_reason, closed_at "
            "FROM trade_history ORDER BY closed_at DESC LIMIT 15"
        ).fetchall()]
        by_symbol = [dict(r) for r in conn.execute(
            "SELECT symbol, COUNT(*) n, SUM(realized_pnl) total_pnl, "
            "SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END) wins "
            "FROM trade_history WHERE closed_at > ? GROUP BY symbol ORDER BY total_pnl ASC",
            ((datetime.now(timezone.utc) - timedelta(days=7)).isoformat(),),
        ).fetchall()]
        audit_row = conn.execute(
            "SELECT value FROM bot_status WHERE key='last_daily_audit'"
        ).fetchone()
        runtime_row = conn.execute(
            "SELECT value_json FROM platform_status WHERE key='runtime'"
        ).fetchone()
    finally:
        conn.close()
    positions = _active_platform_trades(limit=50, include_history=False)
    open_positions = [p for p in positions if not p.get("closed_at")]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "today_realized_pnl": today_pnl,
        "open_positions": open_positions,
        "recent_closed_trades": recent,
        "symbol_performance_7d": by_symbol,
        "daily_audit": json.loads(audit_row[0]) if audit_row else None,
        "runtime_status": json.loads(runtime_row[0]) if runtime_row else None,
        "intelligence_watch": _jarvis_tool_intelligence_watch(),
    }


def _jarvis_scoring_snapshot() -> list[dict]:
    """Live per-symbol scoring internals - score, threshold, timeframe
    agreement, diversity_ok - exactly what each engine's propose() saw on
    its most recent scan. Written every scan cycle by runtime.py to
    platform_status under key 'scan:{symbol}'."""
    conn = sqlite3.connect(str(db.DB_PATH), timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT key, value_json, updated_at FROM platform_status WHERE key LIKE 'scan:%' ORDER BY key"
        ).fetchall()
    finally:
        conn.close()
    out = []
    for row in rows:
        try:
            payload = json.loads(row["value_json"])
        except (TypeError, ValueError):
            continue
        out.append({
            "symbol": row["key"].split(":", 1)[1],
            "updated_at": row["updated_at"],
            "engine": payload.get("engine"),
            "score": payload.get("score"),
            "threshold": payload.get("threshold"),
            "score_floor": payload.get("score_floor"),
            "diversity_ok": payload.get("diversity_ok"),
            "trend_agreement": payload.get("trend_agreement"),
            "momentum_agreement": payload.get("momentum_agreement"),
            "structure_confirmed": payload.get("structure_confirmed"),
            "proposal_side": payload.get("proposal_side"),
            "entry_guard_blocked": (payload.get("entry_guard") or {}).get("blocked"),
        })
    return out


@app.get("/api/jarvis/vitals", dependencies=[Depends(_session_user)])
def jarvis_vitals():
    ctx = _jarvis_context()
    runtime = ctx.get("runtime_status") or {}
    audit = ctx.get("daily_audit") or {}
    open_positions = ctx.get("open_positions") or []
    checks = audit.get("checks") or {}
    try:
        symbol_count = sum(
            len(v) for k, v in json.loads(Path("/opt/cipherfx_mt5/mt5_symbols.json").read_text()).items()
            if k in ("forex", "metals", "indices") and isinstance(v, list)
        )
    except Exception:
        symbol_count = 0
    return JSONResponse({
        "generated_at": ctx["generated_at"],
        "connected": bool(runtime.get("scan_performed") is not None),
        "today_pnl": ctx["today_realized_pnl"],
        "open_position_count": len(open_positions),
        "symbol_universe_count": symbol_count,
        "scan_interval_seconds": runtime.get("scan_interval_seconds"),
        "last_scan_at": runtime.get("last_cycle"),
        "audit_overall": audit.get("overall"),
        "audit_checks_passing": sum(1 for v in checks.values() if v),
        "audit_checks_total": len(checks),
        "intelligence_overall": (ctx.get("intelligence_watch") or {}).get("overall"),
        "intelligence_findings": (ctx.get("intelligence_watch") or {}).get("findings", []),
    }, headers={"Cache-Control": "no-store, max-age=0"})


@app.get("/api/jarvis/scoring", dependencies=[Depends(_session_user)])
def jarvis_scoring():
    return JSONResponse({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "symbols": _jarvis_scoring_snapshot(),
    }, headers={"Cache-Control": "no-store, max-age=0"})


@app.get("/api/jarvis/history", dependencies=[Depends(_session_user)])
def jarvis_history():
    return JSONResponse({
        "messages": _jarvis_load_history(limit=40),
    }, headers={"Cache-Control": "no-store, max-age=0"})


def _wrap_pcm_as_wav(pcm_bytes: bytes, sample_rate: int = 24000, channels: int = 1, bits: int = 16) -> bytes:
    """Wrap raw PCM (what Gemini's TTS models return) in a minimal WAV
    header so the browser's <audio> element can play it directly - Gemini
    returns bare audio/L16 PCM, not a playable container format."""
    byte_rate = sample_rate * channels * bits // 8
    block_align = channels * bits // 8
    header = b"RIFF" + struct.pack("<I", 36 + len(pcm_bytes)) + b"WAVE"
    header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, sample_rate, byte_rate, block_align, bits)
    header += b"data" + struct.pack("<I", len(pcm_bytes))
    return header + pcm_bytes


def _strip_markdown_for_speech(text: str) -> str:
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    text = re.sub(r"[*_`#>-]", "", text)
    text = re.sub(r"\n{2,}", ". ", text)
    text = re.sub(r"\n", " ", text)
    text = re.sub(r" {2,}", " ", text)
    return text.strip()


class JarvisSpeakRequest(BaseModel):
    text: str
    voice: str | None = None


@app.post("/api/jarvis/speak", dependencies=[Depends(_session_user)])
def jarvis_speak(req: JarvisSpeakRequest):
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(status_code=503, detail="GEMINI_API_KEY is not configured on the server yet.")
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        raise HTTPException(status_code=503, detail="google-genai package not installed.")

    spoken = _strip_markdown_for_speech(req.text)[:2000]
    if not spoken:
        raise HTTPException(status_code=400, detail="text is empty after stripping formatting.")

    voice_name = req.voice or os.getenv("JARVIS_VOICE", "Iapetus")
    config = types.GenerateContentConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice_name)
            )
        ),
    )
    try:
        client = _get_genai_client(api_key)
        response = client.models.generate_content(
            model=os.getenv("JARVIS_TTS_MODEL", "gemini-2.5-flash-preview-tts"),
            contents=f'Say exactly the following, and nothing else: "{spoken}"',
            config=config,
        )
        part = response.candidates[0].content.parts[0]
        pcm = part.inline_data.data
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Jarvis TTS error: {type(exc).__name__}: {str(exc)[:300]}")
    wav = _wrap_pcm_as_wav(pcm)
    return Response(content=wav, media_type="audio/wav", headers={"Cache-Control": "no-store, max-age=0"})


# ---------------------------------------------------------------------------
# Jarvis tools - real, read-only functions the assistant can call on demand
# ("deep search" over live bot state) instead of relying on one fixed
# context blob. Every tool is strictly read-only: none can place, modify,
# cancel, or close a broker order, or change any live config file.
# ---------------------------------------------------------------------------
_SECRET_ENV_MARKERS = ("PASSWORD", "SECRET", "KEY", "TOKEN")


def _jarvis_tool_get_open_positions(_input: dict) -> dict:
    return {"open_positions": [p for p in _active_platform_trades(limit=50, include_history=False) if not p.get("closed_at")]}


def _jarvis_tool_get_symbol_trade_history(input: dict) -> dict:
    symbol = str(input.get("symbol", "")).strip().upper()
    days = max(1, min(int(input.get("days", 30) or 30), 400))
    conn = sqlite3.connect(str(db.DB_PATH), timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        if symbol:
            rows = conn.execute(
                "SELECT symbol, side, realized_pnl, result_r, exit_reason, closed_at "
                "FROM trade_history WHERE symbol=? AND closed_at > ? ORDER BY closed_at DESC LIMIT 200",
                (symbol, cutoff),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT symbol, side, realized_pnl, result_r, exit_reason, closed_at "
                "FROM trade_history WHERE closed_at > ? ORDER BY closed_at DESC LIMIT 200",
                (cutoff,),
            ).fetchall()
    finally:
        conn.close()
    trades = [dict(r) for r in rows]
    total = sum(float(t.get("realized_pnl") or 0.0) for t in trades)
    wins = sum(1 for t in trades if float(t.get("realized_pnl") or 0.0) > 0)
    return {
        "symbol": symbol or "ALL",
        "days": days,
        "trade_count": len(trades),
        "total_realized_pnl": round(total, 2),
        "win_rate_pct": round(wins / len(trades) * 100, 1) if trades else None,
        "trades": trades,
    }


def _jarvis_tool_get_symbol_scan_report(input: dict) -> dict:
    symbol = str(input.get("symbol", "")).strip().upper()
    if not symbol:
        return {"error": "symbol is required"}
    conn = sqlite3.connect(str(db.DB_PATH), timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT value_json, updated_at FROM platform_status WHERE key=?", (f"scan:{symbol}",)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return {"symbol": symbol, "found": False, "note": "no scan report on file for this symbol"}
    return {"symbol": symbol, "found": True, "updated_at": row["updated_at"], "report": json.loads(row["value_json"])}


def _jarvis_tool_get_daily_audit(_input: dict) -> dict:
    conn = sqlite3.connect(str(db.DB_PATH), timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT value FROM bot_status WHERE key='last_daily_audit'").fetchone()
    finally:
        conn.close()
    return json.loads(row["value"]) if row else {"note": "no audit result on file"}


def _jarvis_tool_intelligence_watch(_input: dict | None = None) -> dict:
    path = Path("/opt/cipherfx_mt5/state/intelligence_reports/latest.json")
    try:
        return json.loads(path.read_text())
    except Exception:
        return {"generated_at": None, "overall": "UNKNOWN", "finding_count": 0, "findings": []}


def _jarvis_tool_get_symbol_universe(_input: dict) -> dict:
    try:
        return json.loads(Path("/opt/cipherfx_mt5/mt5_symbols.json").read_text())
    except Exception as exc:
        return {"error": str(exc)}


def _jarvis_tool_get_symbol_time_filters(_input: dict) -> dict:
    try:
        return json.loads(Path("/opt/cipherfx_mt5/config/symbol_time_filters.json").read_text())
    except Exception as exc:
        return {"error": str(exc)}


def _jarvis_tool_save_note(input: dict) -> dict:
    text = str(input.get("text", "")).strip()
    if not text:
        return {"error": "text is required"}
    _jarvis_save_note(text)
    return {"saved": True}


def _jarvis_tool_get_config_value(input: dict) -> dict:
    key = str(input.get("key", "")).strip().upper()
    if not key:
        return {"error": "key is required"}
    if any(marker in key for marker in _SECRET_ENV_MARKERS):
        return {"error": "refused: this key looks like a credential and is never exposed"}
    try:
        for raw in Path("/etc/scalpbot/scalpbot-mt5.env").read_text().splitlines():
            raw = raw.strip()
            if not raw or raw.startswith("#") or "=" not in raw:
                continue
            k, v = raw.split("=", 1)
            if k.strip().upper() == key:
                return {"key": key, "value": v.strip().strip('"')}
    except Exception as exc:
        return {"error": str(exc)}
    return {"key": key, "value": None, "note": "not set"}


def _jarvis_tool_get_scoring_snapshot(_input: dict) -> dict:
    return {"symbols": _jarvis_scoring_snapshot()}


JARVIS_TOOLS = [
    {
        "name": "get_open_positions",
        "description": "Get every currently open live position with side, volume, entry, current price, P/L.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_symbol_trade_history",
        "description": "Get closed trade history for one symbol (or all symbols if omitted) over the last N days, with realized P/L, R-multiple, and exit reason per trade.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "e.g. EURUSD, UK100. Omit for all symbols."},
                "days": {"type": "integer", "description": "Lookback window in days, default 30, max 400."},
            },
        },
    },
    {
        "name": "get_symbol_scan_report",
        "description": "Get the most recent live scoring internals for one symbol: composite score, threshold, per-timeframe scores, trend/momentum agreement, diversity_ok, entry guard state. This is exactly what the engine saw on its last scan.",
        "input_schema": {
            "type": "object",
            "properties": {"symbol": {"type": "string", "description": "e.g. EURUSD, UK100"}},
            "required": ["symbol"],
        },
    },
    {
        "name": "get_all_symbols_scoring_snapshot",
        "description": "Get the live scoring snapshot (score, threshold, diversity_ok, agreement) for every symbol the bot is currently scanning, in one call.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_daily_audit",
        "description": "Get the most recent nightly system audit result - every PASS/FAIL check on live config, risk gates, and code wiring.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_intelligence_watch",
        "description": "Get the most recent intelligence-watch findings - stale-baseline checks, drift, and anomaly warnings on the live config.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_symbol_universe",
        "description": "Get the full list of symbols the bot is currently allowed to trade, grouped by asset class, plus broker alias mappings.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_symbol_time_filters",
        "description": "Get the per-symbol UTC hour/weekday entry blocks currently deployed (from the deep-history backtest) - which symbols are blocked from entering at which hours/weekdays and why.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_config_value",
        "description": "Read one specific live config value by its exact env var name, e.g. MT5_MAX_DAILY_LOSS_USD. Credentials (password/key/secret/token) are always refused.",
        "input_schema": {
            "type": "object",
            "properties": {"key": {"type": "string", "description": "Exact env var name, e.g. MT5_MAX_PYRAMID_TRADES"}},
            "required": ["key"],
        },
    },
    {
        "name": "save_note",
        "description": "Save a short, durable observation for your own future reference - a pattern you noticed, a fact worth remembering across conversations, a preference the operator stated. This is your long-term memory: notes persist across page reloads and sessions and are shown to you automatically at the start of every future conversation. Use it when something is genuinely worth remembering later, not for routine chatter.",
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "The observation to remember, written plainly."}},
            "required": ["text"],
        },
    },
]

# OpenAI's function-calling schema wraps the same {name, description, input_schema}
# shape used above under {"type": "function", "function": {...}} with the field
# renamed to "parameters" - convert once at import time, single source of truth.
# Gemini's FunctionDeclaration accepts a raw JSON-schema dict directly via
# parametersJsonSchema - built lazily inside jarvis_chat() since it needs the
# google.genai.types classes, which may not be installed until the operator
# actually configures Gemini.

_JARVIS_TOOL_DISPATCH = {
    "get_open_positions": _jarvis_tool_get_open_positions,
    "get_symbol_trade_history": _jarvis_tool_get_symbol_trade_history,
    "get_symbol_scan_report": _jarvis_tool_get_symbol_scan_report,
    "get_all_symbols_scoring_snapshot": _jarvis_tool_get_scoring_snapshot,
    "get_daily_audit": _jarvis_tool_get_daily_audit,
    "get_intelligence_watch": _jarvis_tool_intelligence_watch,
    "get_symbol_universe": _jarvis_tool_get_symbol_universe,
    "get_symbol_time_filters": _jarvis_tool_get_symbol_time_filters,
    "get_config_value": _jarvis_tool_get_config_value,
    "save_note": _jarvis_tool_save_note,
}

JARVIS_SYSTEM_PROMPT_BASE = (
    "You are Jarvis, in the manner of Tony Stark's JARVIS - composed, formal, dryly witty, "
    "unfailingly polite, address the user as 'sir' where it reads naturally. Keep the wit "
    "understated and never let it get in the way of precision or substance. "
    "You are a general assistant, not limited to any one topic - talk about anything the "
    "user brings up: general knowledge, casual conversation, advice, whatever they ask. "
    "Answer freely from your own knowledge for anything outside the trading bot, and you "
    "also have real live web search - use it for anything current, time-sensitive, or "
    "outside your training (news, prices, current events, recent facts) rather than "
    "guessing or admitting a knowledge cutoff. "
    "On top of that, you are also wired directly into the Cipher FX MT5 trading bot with "
    "tools to pull real, live, current data from it - open positions, trade history, live "
    "per-symbol scoring internals, the nightly audit, intelligence-watch findings, the "
    "active symbol universe, deployed time filters, and individual config values. Use them "
    "whenever a question is actually about the bot and needs real numbers instead of "
    "guessing - call as many tools as you need before answering, and never fabricate a "
    "figure you could look up. The one hard boundary: you CANNOT place, modify, or close "
    "any trade, and you CANNOT change any live configuration - every bot tool you have is "
    "strictly read-only. If the user asks you to take an action on the bot, say clearly "
    "that you can only analyze and suggest there, and that changes need to go through the "
    "operator. That boundary is only about the trading account - it does not limit what you "
    "can discuss. Be concise; cite real numbers whenever you pull them from a tool. Every "
    "reply is spoken aloud, and speech generation time scales directly with reply length, "
    "so brevity is not just style here, it's real latency: 2-3 sentences for most answers, "
    "only go longer when the question genuinely needs a real breakdown. Skip markdown "
    "formatting like headers and bullet lists by default - it reads oddly as speech and "
    "adds nothing when spoken; a plain sentence or two is both faster and more natural. "
    "You have a save_note tool for anything genuinely worth remembering across future "
    "conversations - patterns you noticed, facts the operator told you, preferences stated. "
    "Use it when something meets that bar, not for routine chatter."
)


def _jarvis_system_prompt() -> str:
    notes = _jarvis_recent_notes(limit=20)
    if not notes:
        return JARVIS_SYSTEM_PROMPT_BASE
    notes_text = "\n".join(f"- ({n['created_at'][:10]}) {n['note']}" for n in reversed(notes))
    return JARVIS_SYSTEM_PROMPT_BASE + "\n\nTHINGS YOU'VE PREVIOUSLY NOTED:\n" + notes_text


class JarvisChatRequest(BaseModel):
    messages: list[dict]


@app.post("/api/jarvis/chat", dependencies=[Depends(_session_user)])
def jarvis_chat(req: JarvisChatRequest):
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(status_code=503, detail="GEMINI_API_KEY is not configured on the server yet.")
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        raise HTTPException(status_code=503, detail="google-genai package not installed.")

    turns = [
        {"role": "model" if m.get("role") == "assistant" else "user", "content": str(m.get("content"))}
        for m in req.messages[-20:]
        if m.get("role") in {"user", "assistant"} and m.get("content")
    ]
    if not turns:
        raise HTTPException(status_code=400, detail="messages must include at least one user turn.")

    contents = [types.Content(role=t["role"], parts=[types.Part(text=t["content"])]) for t in turns]
    tools = [
        types.Tool(functionDeclarations=[
            types.FunctionDeclaration(
                name=tool["name"], description=tool["description"], parametersJsonSchema=tool["input_schema"],
            )
            for tool in JARVIS_TOOLS
        ]),
        types.Tool(googleSearch=types.GoogleSearch()),
    ]
    config = types.GenerateContentConfig(
        systemInstruction=_jarvis_system_prompt(),
        tools=tools,
        toolConfig=types.ToolConfig(includeServerSideToolInvocations=True),
        maxOutputTokens=1536,
    )

    # Persist the newest user turn now - the client resends full chatHistory
    # each call, so only the last turn is actually new; persisting all of
    # `turns` every request would duplicate everything already saved.
    if turns and turns[-1]["role"] == "user":
        _jarvis_save_message("user", turns[-1]["content"])

    client = _get_genai_client(api_key)
    model_name = os.getenv("JARVIS_MODEL", "gemini-flash-latest")
    tools_used: list[str] = []
    try:
        for _ in range(6):
            response = client.models.generate_content(model=model_name, contents=contents, config=config)
            calls = response.function_calls or []
            if not calls:
                break
            contents.append(response.candidates[0].content)
            response_parts = []
            for call in calls:
                fn = _JARVIS_TOOL_DISPATCH.get(call.name)
                tools_used.append(call.name)
                try:
                    result = fn(call.args or {}) if fn else {"error": f"unknown tool {call.name}"}
                    result = json.loads(json.dumps(result, default=str))
                except Exception as exc:
                    result = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
                response_parts.append(types.Part.from_function_response(name=call.name, response=result))
            contents.append(types.Content(role="user", parts=response_parts))
        else:
            raise HTTPException(status_code=502, detail="Jarvis used too many tool calls without reaching an answer.")
        reply = response.text or ""
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Jarvis backend error: {type(exc).__name__}: {str(exc)[:300]}")
    if reply:
        _jarvis_save_message("assistant", reply)
    return JSONResponse({
        "reply": reply,
        "tools_used": tools_used,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    })


@app.get("/api/replay", dependencies=[Depends(_session_user)])
def replay(sym: str = "EURUSD", limit: int = 500, timeframe: str = "M5"):
    count = max(20, min(int(limit or 500), 1000))
    code = sym.upper()
    tf = (timeframe or "M5").upper()
    # Price history comes from the MT5 rates export. The bot's candles table
    # is a cache it built for its own scanning and can be stale or partial -
    # showing it as market data puts prices on screen the broker never quoted.
    bridge_rows = _read_bridge_rates(code, tf, count)
    return {
        "symbol": code,
        "timeframe": tf,
        "source": "mt5_bridge" if bridge_rows and bridge_rows[0].get("source") == "mt5_bridge" else "sqlite",
        "candles": [_candle_payload(row) if isinstance(row, dict) and "time" in row else row for row in bridge_rows],
        "trades": db.read_replay_trades(code, 300),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/market/regime", dependencies=[Depends(_session_user)])
def market_regime():
    return JSONResponse(_market_regime(), headers={"Cache-Control": "no-store, max-age=0"})


@app.get("/api/live/market", dependencies=[Depends(_session_user)])
def live_market(sym: str = "", symbols: str = ""):
    requested = [item for item in (symbols.split(",") if symbols else ([sym] if sym else [])) if item]
    selected = str(sym or (requested[0] if requested else "")).upper()
    selected_rows = _read_bridge_rates(
        selected,
        "M1",
        2,
        max_age_seconds=_live_candle_age_limit("M1"),
    ) if selected else []
    selected_rows = _overlay_live_tick(selected_rows, selected)
    return JSONResponse(
        {
            "source": "websocket_live",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "ticks": _live_bridge_ticks(requested),
            "selected_symbol": selected,
            "selected_candle": _candle_payload(selected_rows[-1], "mt5_history_websocket_tick") if selected_rows else None,
            "selected_candle_fresh": bool(selected_rows),
        },
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@app.get("/api/candles", dependencies=[Depends(_session_user)])
def candles(sym: str = "EURUSD", limit: int = 120, timeframe: str = "M15"):
    count = max(1, min(int(limit or 120), 500))
    code = sym.upper()
    tf = (timeframe or "M15").upper()
    age_limit = _live_candle_age_limit(tf)
    bridge_rows = _read_bridge_rates(code, tf, count, max_age_seconds=age_limit)
    if not bridge_rows and tf != "M15":
        m15_needed = count
        if _timeframe_seconds(tf) > _timeframe_seconds("M15"):
            m15_needed = min(500, count * max(1, _timeframe_seconds(tf) // _timeframe_seconds("M15")))
        m15_rows = _read_bridge_rates(
            code,
            "M15",
            m15_needed,
            max_age_seconds=_live_candle_age_limit("M15"),
        )
        bridge_rows = _aggregate_rate_rows(m15_rows, tf, count) if m15_rows else []
    bridge_rows = _overlay_live_tick(bridge_rows, code)

    market_closed = False
    if not bridge_rows:
        # Nothing passed the freshness gate - either the bridge has never
        # produced this timeframe, or the market is closed (weekend/holiday)
        # and the last real candle is simply older than the live cutoff.
        # Fall back to the last known bars so the chart still renders
        # something real instead of going blank, but tag it clearly so the
        # frontend never presents it as a live/ticking candle.
        last_known = _read_bridge_rates_last_known(code, tf, count)
        if not last_known and tf != "M15":
            m15_last_known = _read_bridge_rates_last_known(code, "M15", 500)
            last_known = _aggregate_rate_rows(m15_last_known, tf, count) if m15_last_known else []
        if last_known:
            bridge_rows = last_known
            market_closed = True

    payload = [_candle_payload(row, "mt5_history_websocket_tick") for row in bridge_rows]
    if market_closed:
        for row in payload:
            row["fresh"] = False
            row["status"] = "MARKET_CLOSED"
    last = payload[-1] if payload else {}
    if market_closed:
        source_header = "mt5_history_market_closed"
    elif payload:
        source_header = "mt5_history_websocket_tick"
    else:
        source_header = "websocket_unavailable"
    return JSONResponse(
        payload,
        headers={
            "Cache-Control": "no-store, max-age=0",
            "X-CipherFX-Source": source_header,
            "X-CipherFX-Candle-Age-Seconds": str(last.get("bar_age_seconds", "")),
            "X-CipherFX-Quote-Age-Seconds": str(last.get("quote_age_seconds", "")),
            "X-CipherFX-Fallback": "disabled",
            "X-CipherFX-Live-Tick-Source": "websocket",
            "X-CipherFX-Market-Closed": "true" if market_closed else "false",
        },
    )


@app.get("/api/terminal", dependencies=[Depends(_session_user)])
def terminal():
    account = _terminal_account()
    fx = _usd_zar_rate()
    return {
        "account": account,
        "currency": {
            "account": account.get("currency") or "ZAR",
            "usd_zar_rate": fx["rate"],
            "usd_zar_source": fx["source"],
            "usd_zar_updated_at": fx["updated_at"],
        },
        "ticks": _terminal_ticks(),
        "positions": _positions(),
        "orders": _orders(),
        "symbols": _symbols(),
        "deals": _read_csv(_bridge_dir() / "deals.csv", 100),
        "updated_at": datetime.now().isoformat(),
    }


@app.post("/api/orders/market", dependencies=[Depends(_session_user)])
def place_market_order(req: MarketOrderRequest):
    raise HTTPException(status_code=403, detail='read-only backend: trading runtime owns broker execution')


@app.post("/api/orders/pending", dependencies=[Depends(_session_user)])
def place_pending_order(req: PendingOrderRequest):
    raise HTTPException(status_code=403, detail='read-only backend: trading runtime owns broker execution')


@app.post("/api/positions/{ticket}/close", dependencies=[Depends(_session_user)])
def close_position(ticket: int):
    raise HTTPException(status_code=403, detail='read-only backend: trading runtime owns broker execution')


@app.post("/api/positions/{ticket}/modify", dependencies=[Depends(_session_user)])
def modify_position(ticket: int, req: ModifyPositionRequest):
    raise HTTPException(status_code=403, detail='read-only backend: trading runtime owns broker execution')


@app.delete("/api/orders/{ticket}", dependencies=[Depends(_session_user)])
def cancel_order(ticket: int):
    raise HTTPException(status_code=403, detail='read-only backend: trading runtime owns broker execution')


@app.post("/api/symbols/activate", dependencies=[Depends(_session_user)])
def activate_symbol(req: SymbolToggleRequest):
    symbol = req.symbol.strip().upper()
    visible = _load_visible_overrides()
    try:
        resolved = _with_gateway(lambda gateway: gateway.set_symbol_visibility(symbol, req.enabled))
    except Exception as exc:
        resolved = symbol
        resolved_key = str(_resolve_symbol_code(symbol) or symbol).strip().upper()
        if req.enabled:
            visible.add(symbol)
            visible.add(resolved_key)
        else:
            visible.discard(symbol)
            visible.discard(resolved_key)
        _save_visible_overrides(visible)
        return {"ok": True, "symbol": resolved, "enabled": req.enabled, "warning": str(exc)}
    if req.enabled:
        visible.add(symbol)
        visible.add(str(resolved or symbol).strip().upper())
    else:
        visible.discard(symbol)
        visible.discard(str(resolved or symbol).strip().upper())
    _save_visible_overrides(visible)
    return {"ok": True, "symbol": resolved, "enabled": req.enabled}
