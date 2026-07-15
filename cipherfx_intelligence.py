"""Shared, shadow-only intelligence and decision evidence for CipherFX.

This module records evidence and validates inputs. It never sends, rejects,
cancels, or expires orders; the existing MT5 gateway remains authoritative.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import uuid

UTC = timezone.utc
SHADOW_MODE = str(os.getenv("CIPHERFX_INTELLIGENCE_SHADOW_MODE", "1")).strip().lower() not in {"0", "false", "no", "off"}

HARD_SAFETY_REASONS = {
    "ACCOUNT_PERMISSION", "MARKET_CLOSED", "SYMBOL_CLOSED", "MT5_DISCONNECTED",
    "BRIDGE_OFFLINE", "DAILY_LOSS_LIMIT", "PORTFOLIO_RISK_LIMIT",
    "SYMBOL_RISK_LIMIT", "PYRAMID_AGGREGATE_RISK_LIMIT", "MARGIN_LIMIT",
    "DUPLICATE_ORDER", "TRADE_COUNT_LIMIT", "MAX_OPEN_POSITION_LIMIT",
    "INVALID_PRICE_GEOMETRY", "INVALID_SL_TP", "SPREAD_EMERGENCY",
    "SLIPPAGE_EMERGENCY", "BROKER_MIN_LOT", "MARGIN_INSUFFICIENT",
    "SIDE_CONSISTENCY_MISMATCH", "BROKER_RECONCILIATION_UNSAFE",
    "KILL_SWITCH", "AUTHENTICATION_FAILURE",
}
QUALITY_REASONS = {
    "QUALITY_REJECTED", "NOT_QUALIFIED", "PENDING_M5_OR_M1", "STALE_ENTRY",
    "EXHAUSTION", "ADX_DANGER", "HTF_ALIGNMENT", "PROFILE_QUALITY",
    "COST_QUALITY", "LOW_VOLATILITY", "REGIME_UNCERTAIN",
    "NEWS_DATA_UNAVAILABLE",
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _safe_json(value) -> str:
    try:
        return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))
    except Exception:
        return json.dumps({"serialization_error": str(value)[:240]})


def _token(text: str) -> str:
    return str(text or "").upper().replace("-", "_").replace(" ", "_")[:160]


@dataclass(frozen=True)
class DecisionRecord:
    decision_id: str
    created_at: str
    decision_state: str
    event: str
    symbol: str = ""
    setup_id: str = ""
    engine: str = ""
    side: str = ""
    reason: str = ""
    scope: str = ""
    hard_block: bool = False
    quality_rejection: bool = False
    shadow_only: bool = True
    payload: dict | None = None


class TradePermissionGovernor:
    """Classifies decisions without owning execution or setup lifecycle."""

    def final_quality_decision(self, *, allowed: bool, reason: str = "", event: str = "") -> dict:
        text = str(reason or "")
        token = _token(text)
        hard = any(name in token or name.replace("_", " ") in text.upper() for name in HARD_SAFETY_REASONS)
        event_token = _token(event)
        if event_token.startswith("PENDING") or "WAITING_1M" in event_token:
            state = "PENDING_M5_OR_M1"
        elif event_token.startswith("LOSS_STREAK_OBSERVE") or event_token.startswith("OBSERVE") or str(reason).lower().startswith("observe"):
            state = "OBSERVE_ONLY"
        elif allowed:
            state = "EXECUTION_ELIGIBLE"
        elif hard:
            state = "HARD_BLOCKED"
        else:
            state = "NOT_QUALIFIED"
        return {
            "decision_state": state,
            "hard_block": state == "HARD_BLOCKED",
            "quality_rejection": state == "NOT_QUALIFIED",
            "reason": text,
            "event": str(event or "decision"),
            "shadow_only": SHADOW_MODE,
        }


class MarketDataIntegrityEngine:
    """Validates completed candles and ticks without classifying closed markets stale."""

    DEFAULTS = {"tick": float(os.getenv("DATA_MAX_TICK_AGE_SECONDS", "5")), "M1": float(os.getenv("DATA_MAX_M1_AGE_SECONDS", "90")), "M5": float(os.getenv("DATA_MAX_M5_AGE_SECONDS", "360")), "H1": float(os.getenv("DATA_MAX_H1_AGE_SECONDS", "7200")), "clock": float(os.getenv("DATA_MAX_CLOCK_DRIFT_SECONDS", "5")), "missing_tolerance": int(os.getenv("DATA_MISSING_BAR_TOLERANCE", "1"))}
    INTERVAL_SECONDS = {"M1": 60, "M5": 300, "M15": 900, "H1": 3600, "H4": 14400}

    def __init__(self, now_fn=None):
        self._now_fn = now_fn or (lambda: datetime.now(UTC).timestamp())

    def _timestamp(self, row):
        for key in ("time", "Time", "timestamp", "Timestamp", "ts", "date"):
            try:
                value = row[key]
            except Exception:
                continue
            try:
                if hasattr(value, "timestamp"):
                    return float(value.timestamp())
                return float(value)
            except Exception:
                continue
        return None

    def validate_frame(self, frame, timeframe: str, *, expected_open: bool = True) -> dict:
        tf = str(timeframe or "").upper()
        result = {"timeframe": tf, "status": "HEALTHY", "issues": [], "bars": 0}
        if frame is None:
            result.update(status="DATA_MISSING", issues=["missing_frame"])
            return result
        try:
            result["bars"] = int(len(frame))
        except Exception:
            result.update(status="DATA_MISSING", issues=["invalid_frame"])
            return result
        if result["bars"] == 0:
            result.update(status="DATA_MISSING", issues=["empty_frame"])
            return result
        try:
            start = max(0, len(frame) - 200)
            rows = [frame.iloc[i] for i in range(start, len(frame))]
            stamps = [self._timestamp(row) for row in rows]
            stamps = [value for value in stamps if value is not None]
            if stamps:
                if any(a >= b for a, b in zip(stamps, stamps[1:])):
                    result["issues"].append("OUT_OF_ORDER")
                if len(set(stamps)) != len(stamps):
                    result["issues"].append("DUPLICATE_BARS")
                latest_stamp = max(stamps)
                age = max(0.0, self._now_fn() - latest_stamp)
                result["latest_age_seconds"] = round(age, 3)
                limit = float(self.DEFAULTS.get(tf, 7200.0))
                result["max_age_seconds"] = limit
                if expected_open and age > limit:
                    result["issues"].append("STALE")
                interval = self.INTERVAL_SECONDS.get(tf, 0)
                ordered = sorted(set(stamps))
                gaps = []
                session_gaps = 0
                for previous, current in zip(ordered, ordered[1:]):
                    delta = current - previous
                    if interval and delta > 21600:
                        session_gaps += 1
                    elif interval and delta > interval * (self.DEFAULTS["missing_tolerance"] + 1):
                        gaps.append(round(delta, 3))
                if gaps:
                    result["missing_bar_gaps_seconds"] = gaps[:10]
                    result["issues"].append("MISSING_BARS")
                result["session_gap_count"] = session_gaps
                future_drift = latest_stamp - self._now_fn()
                result["clock_drift_seconds"] = round(future_drift, 3)
                if future_drift > float(self.DEFAULTS["clock"]):
                    result["issues"].append("CLOCK_FUTURE")
        except Exception as exc:
            result["issues"].append(f"validation_error:{str(exc)[:120]}")
        if result["issues"]:
            issues = result["issues"]
            result["status"] = "DUPLICATE_BARS" if "DUPLICATE_BARS" in issues else "OUT_OF_ORDER" if "OUT_OF_ORDER" in issues else "STALE" if "STALE" in issues else "DEGRADED"
        return result


    def validate_tick(self, tick, *, max_age_seconds: float | None = None) -> dict:
        """Validate broker/bridge tick age, clock, and quote geometry as evidence only."""
        result = {"status": "HEALTHY", "issues": [], "max_age_seconds": max_age_seconds}
        if tick is None:
            return {"status": "DATA_MISSING", "issues": ["missing_tick"], "max_age_seconds": max_age_seconds}
        now = self._now_fn()
        raw_epoch = getattr(tick, "time_utc", getattr(tick, "time", 0))
        try:
            epoch = float(raw_epoch or 0.0)
        except Exception:
            epoch = 0.0
        if epoch <= 0.0:
            result["issues"].append("missing_tick_timestamp")
        else:
            age = now - epoch
            result["tick_epoch"] = epoch
            result["age_seconds"] = round(age, 3)
            if age < -float(self.DEFAULTS["clock"]):
                result["issues"].append("CLOCK_FUTURE")
            elif max_age_seconds is not None and age > float(max_age_seconds):
                result["issues"].append("STALE")
        try:
            bid = float(getattr(tick, "bid", 0.0) or 0.0)
            ask = float(getattr(tick, "ask", 0.0) or 0.0)
            result["bid"] = bid
            result["ask"] = ask
            if bid <= 0.0 or ask <= 0.0 or ask < bid:
                result["issues"].append("INVALID_QUOTE_GEOMETRY")
        except Exception:
            result["issues"].append("INVALID_QUOTE_GEOMETRY")
        if result["issues"]:
            result["status"] = "STALE" if "STALE" in result["issues"] else "DATA_CLOCK_INVALID" if "CLOCK_FUTURE" in result["issues"] else "DEGRADED"
        return result

    def validate_frames(self, frames, tick=None, *, tick_max_age_seconds: float | None = None, required_timeframes=None):
        results = {}
        required = set(required_timeframes or ("H4", "H1", "M15", "M5", "M1"))
        for label in ("H4", "H1", "M15", "M5", "M1"):
            if label not in required and (frames or {}).get(label) is None:
                results[label] = {"timeframe": label, "status": "NOT_REQUIRED", "issues": [], "bars": 0}
            else:
                results[label] = self.validate_frame((frames or {}).get(label), label, expected_open=True)
        if tick is not None:
            results["TICK"] = self.validate_tick(tick, max_age_seconds=tick_max_age_seconds)
        statuses = [row.get("status") for row in results.values() if row.get("status") != "NOT_REQUIRED"]
        status = "HEALTHY" if all(item == "HEALTHY" for item in statuses) else "DEGRADED"
        return {"status": status, "required_timeframes": sorted(required), "frames": results}


class BrokerSpecificationValidator:
    REQUIRED_FIELDS = ("symbol", "point", "volume_min", "volume_max", "volume_step")

    def validate(self, spec) -> dict:
        values = {field: getattr(spec, field, None) if spec is not None else None for field in self.REQUIRED_FIELDS}
        issues = []
        if not values.get("symbol"):
            issues.append("missing_symbol")
        for field in ("point", "volume_min", "volume_max", "volume_step"):
            try:
                if float(values.get(field) or 0.0) <= 0:
                    issues.append(f"invalid_{field}")
            except Exception:
                issues.append(f"invalid_{field}")
        return {"status": "HEALTHY" if not issues else "INVALID_SPEC", "issues": issues, "fields": {k: str(v) for k, v in values.items()}}


class CipherFXIntelligenceCoordinator:
    """Persistent shadow evidence coordinator for the existing bot."""

    SCHEMA_VERSION = "intelligence_shadow_v1"
    TABLES = (
        "market_data_health", "broker_symbol_specs", "premarket_plans",
        "premarket_plan_versions", "premarket_plan_events", "market_regimes",
        "strategy_permissions", "liquidity_levels", "session_profiles",
        "gap_events", "opening_ranges", "news_events", "execution_cost_models",
        "setup_scores", "pending_setups", "portfolio_exposure_snapshots",
        "trade_decisions", "trade_executions", "trade_outcomes",
        "trade_excursions", "trade_attribution", "missed_trades",
        "false_entries", "shadow_trades", "walk_forward_runs",
        "walk_forward_results", "live_drift_snapshots", "trade_replay_events",
        "reconciliation_events", "system_health_events", "kill_switch_events",
        "configuration_versions", "deployment_versions", "notifications",
        "trade_permission_decisions", "score_adjustments", "trade_flow_snapshots",
        "database_integrity_events",
    )

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.governor = TradePermissionGovernor()
        self.data = MarketDataIntegrityEngine()
        self.broker = BrokerSpecificationValidator()
        self._init_schema()

    def _connect(self):
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.execute("PRAGMA busy_timeout=10000")
        # Keep intelligence writes from allowing an unbounded WAL. This is
        # persistence hygiene only; it does not alter trading decisions.
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA wal_autocheckpoint=1000")
        conn.execute("PRAGMA journal_size_limit=67108864")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self):
        conn = self._connect()
        try:
            for table in self.TABLES:
                conn.execute(
                    f"CREATE TABLE IF NOT EXISTS {table} ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT, created_at TEXT NOT NULL, "
                    "symbol TEXT, setup_id TEXT, status TEXT, reason TEXT, payload_json TEXT)"
                )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_trade_permission_created ON trade_permission_decisions(created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_trade_permission_symbol ON trade_permission_decisions(symbol, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_market_health_symbol ON market_data_health(symbol, created_at)")
            conn.commit()
        finally:
            conn.close()

    def configuration_checksum(self) -> str:
        rows = []
        prefixes = ("MT5_", "CIPHERFX_", "DATA_", "PLAN_", "SHADOW_", "NEW_STRATEGIES_", "DRIFT_", "AUTO_")
        for key, value in sorted(os.environ.items()):
            if key.startswith(prefixes):
                rows.append(f"{key}={value}")
        return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()

    def record_decision(self, event: dict | None):
        event = dict(event or {})
        classified = self.governor.final_quality_decision(
            allowed=str(event.get("decision") or "").lower() in {"allow", "allowed", "pass", "eligible"},
            reason=str(event.get("reason") or ""),
            event=str(event.get("event") or "decision"),
        )
        state = str(event.get("decision_state") or classified["decision_state"])
        payload = {**event, **classified, "schema_version": self.SCHEMA_VERSION}
        event_id = str(event.get("event_id") or uuid.uuid4())
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO trade_permission_decisions(event_id,created_at,symbol,setup_id,status,reason,payload_json) VALUES (?,?,?,?,?,?,?)",
                (event_id, str(event.get("ts") or utc_now()), str(event.get("symbol") or ""), str(event.get("setup_id") or ""), state, str(event.get("reason") or classified.get("reason") or ""), _safe_json(payload)),
            )
            conn.commit()
        except Exception:
            pass
        finally:
            conn.close()
        return DecisionRecord(
            decision_id=event_id,
            created_at=str(event.get("ts") or utc_now()),
            decision_state=state,
            event=str(event.get("event") or "decision"),
            symbol=str(event.get("symbol") or ""),
            setup_id=str(event.get("setup_id") or ""),
            engine=str(event.get("engine") or ""),
            side=str(event.get("direction") or event.get("side") or ""),
            reason=str(event.get("reason") or ""),
            scope=str(event.get("scope") or ""),
            hard_block=bool(classified.get("hard_block")),
            quality_rejection=bool(classified.get("quality_rejection")),
            payload=payload,
        )

    def record_health(self, symbol: str, status: str, payload: dict | None = None):
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO market_data_health(created_at,symbol,status,payload_json) VALUES (?,?,?,?)",
                (utc_now(), str(symbol or ""), str(status or ""), _safe_json(payload or {})),
            )
            conn.commit()
        finally:
            conn.close()

    def persist(self, table: str, *, symbol: str = "", setup_id: str = "", status: str = "", reason: str = "", payload: dict | None = None, event_id: str = ""):
        table_name = str(table or "").strip()
        if table_name not in self.TABLES:
            raise ValueError(f"unknown intelligence table: {table_name}")
        conn = self._connect()
        try:
            conn.execute(
                f"INSERT INTO {table_name}(event_id,created_at,symbol,setup_id,status,reason,payload_json) VALUES (?,?,?,?,?,?,?)",
                (str(event_id or uuid.uuid4()), utc_now(), str(symbol or ""), str(setup_id or ""), str(status or ""), str(reason or ""), _safe_json(payload or {})),
            )
            conn.commit()
        finally:
            conn.close()

    def query(self, sql: str, params: tuple = ()):
        conn = self._connect()
        try:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def status(self) -> dict:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "mode": "SHADOW_ONLY",
            "configuration_checksum": self.configuration_checksum(),
            "hard_safety_reason_count": len(HARD_SAFETY_REASONS),
            "quality_reason_count": len(QUALITY_REASONS),
        }


__all__ = [
    "DecisionRecord", "TradePermissionGovernor", "MarketDataIntegrityEngine",
    "BrokerSpecificationValidator", "CipherFXIntelligenceCoordinator",
    "HARD_SAFETY_REASONS", "QUALITY_REASONS", "SHADOW_MODE",
]
