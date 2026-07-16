from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from config.settings import ServiceSettings, load_settings
from mt5_xm_gateway import MT5Gateway
from tick_websocket import TickWebSocketServer

from .database import DatabaseLayer
from .engines import LearningEngine
from .execution import ExecutionEngine
from .feedback import LearningFeedbackEngine
from .management import TradeManagementEngine
from .market_data import MarketDataEngine, MarketDataError

LOG = logging.getLogger("cipherfx.platform")


class ModularTradingRuntime:
    """Authoritative orchestration of market snapshots, proposals, and operations."""

    ASSET_GROUPS = {"forex": "forex", "indices": "index", "metals": "metal"}
    LIFECYCLE_STATES = (
        "IDLE", "SCANNING", "MARKET_DATA_VERIFIED", "ENGINE_ANALYSIS",
        "PROPOSAL_CREATED", "RISK_VALIDATED", "BROKER_VALIDATED",
        "ORDER_EXECUTED", "POSITION_MANAGED", "POSITION_CLOSED",
        "DATABASE_UPDATED", "COOLDOWN", "NEXT_SCAN", "RECOVERY",
    )
    LIFECYCLE_TRANSITIONS = {
        "IDLE": {"SCANNING", "RECOVERY"},
        "SCANNING": {"MARKET_DATA_VERIFIED", "COOLDOWN", "RECOVERY"},
        "MARKET_DATA_VERIFIED": {"ENGINE_ANALYSIS", "RECOVERY"},
        "ENGINE_ANALYSIS": {"PROPOSAL_CREATED", "RISK_VALIDATED", "POSITION_MANAGED", "COOLDOWN", "RECOVERY"},
        "PROPOSAL_CREATED": {"RISK_VALIDATED", "COOLDOWN", "RECOVERY"},
        "RISK_VALIDATED": {"BROKER_VALIDATED", "COOLDOWN", "RECOVERY"},
        "BROKER_VALIDATED": {"ORDER_EXECUTED", "POSITION_MANAGED", "DATABASE_UPDATED", "COOLDOWN", "RECOVERY"},
        "ORDER_EXECUTED": {"POSITION_MANAGED", "DATABASE_UPDATED", "RECOVERY"},
        "POSITION_MANAGED": {"POSITION_CLOSED", "DATABASE_UPDATED", "COOLDOWN", "RECOVERY"},
        "POSITION_CLOSED": {"DATABASE_UPDATED", "COOLDOWN"},
        "DATABASE_UPDATED": {"COOLDOWN", "NEXT_SCAN"},
        "COOLDOWN": {"NEXT_SCAN", "RECOVERY"},
        "NEXT_SCAN": {"SCANNING", "IDLE", "RECOVERY"},
        "RECOVERY": {"NEXT_SCAN", "IDLE"},
    }

    def __init__(self, settings: ServiceSettings):
        self.settings = settings
        self.config = settings.runtime
        self.database = DatabaseLayer(settings.paths.database_file)
        self.gateway = MT5Gateway(self.config)
        self.market_data = MarketDataEngine(self.gateway, self.config.history_bars)
        self.websocket = TickWebSocketServer(
            os.getenv("MT5_WS_TICK_HOST", "127.0.0.1"),
            int(os.getenv("MT5_WS_TICK_PORT", "8765")),
            self.market_data.ingest_tick,
            enabled=True,
        )
        self.learning = LearningEngine(self.database)
        self.execution = ExecutionEngine(self.gateway, self.config, self.database)
        self.management = TradeManagementEngine(self.gateway, self.database)
        self.feedback = LearningFeedbackEngine(self.database, self.learning.record_outcome)
        self.heartbeat_path = Path(
            os.getenv(
                "MT5_HEARTBEAT_FILE",
                str(settings.paths.app_dir / "state" / "mt5_heartbeat.json"),
            )
        )
        self._stop = False
        self._last_market_feed_persist_at = 0.0
        self._last_db_checkpoint_at = 0.0
        self._lifecycle_state = "IDLE"

    def _record_lifecycle(self, state: str, reason: str = "", **details) -> bool:
        state = str(state).upper()
        previous = self._lifecycle_state
        payload = {
            "previous_state": previous,
            "current_state": state,
            "reason": reason,
            "event_time": datetime.now(timezone.utc).isoformat(),
            **details,
        }
        if state not in self.LIFECYCLE_TRANSITIONS.get(previous, set()):
            self.database.event("LIFECYCLE_INVALID_TRANSITION", payload)
            return False
        self._lifecycle_state = state
        self.database.status("lifecycle", payload)
        self.database.event("LIFECYCLE_STATE", payload)
        return True

    def _write_heartbeat(self, state: str = "RUNNING") -> None:
        self.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "state": state,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "pid": os.getpid(),
            "route": "cipherfx_platform",
        }
        temporary = Path(str(self.heartbeat_path) + ".tmp")
        temporary.write_text(json.dumps(payload, sort_keys=True))
        temporary.replace(self.heartbeat_path)

    def stop(self, *_args) -> None:
        self._stop = True
        self.gateway.request_shutdown()

    @staticmethod
    def _payload(report: dict, proposal=None) -> dict:
        payload = dict(report)
        payload["proposal_id"] = proposal.proposal_id if proposal else ""
        payload["proposal_side"] = proposal.side if proposal else ""
        payload["confidence"] = proposal.confidence if proposal else 0.0
        payload["probability"] = proposal.probability if proposal else 0.0
        payload["proposal_reasoning"] = proposal.reasoning if proposal else ()
        return payload

    @staticmethod
    def _snapshot_id(snapshot) -> str:
        frame_times = "|".join(
            f"{name}:{frame.last.timestamp.isoformat() if frame.last else ''}"
            for name, frame in sorted(snapshot.frames.items())
        )
        material = "|".join(
            [
                snapshot.symbol,
                snapshot.asset_class,
                snapshot.tick.timestamp.isoformat(),
                frame_times,
            ]
        )
        return hashlib.sha256(material.encode()).hexdigest()[:32]

    def _expire_feedback(self) -> int:
        expired = self.database.expired_proposals()
        for row in expired:
            self.feedback.record_expired_proposal(
                row["proposal_id"],
                row["symbol"],
                row["side"],
                row["asset_class"],
            )
        return len(expired)

    def run_once(self, *, perform_scan: bool = True) -> dict[str, int]:
        summary = {
            "symbols": 0,
            "proposals": 0,
            "filled": 0,
            "blocked": 0,
            "rejected": 0,
            "closed": 0,
            "expired": 0,
        }
        if perform_scan:
            self._record_lifecycle("SCANNING", "configured scan interval elapsed")
            for canonical in self.config.all_symbols():
                group = self.config.group_for_symbol(canonical)
                asset = self.ASSET_GROUPS.get(group)
                if not asset:
                    self.database.event("SYMBOL_UNSUPPORTED_GROUP", {"group": group}, symbol=canonical)
                    continue
                summary["symbols"] += 1
                try:
                    snapshot = self.market_data.snapshot(canonical, asset)
                    self.database.save_snapshot(self._snapshot_id(snapshot), snapshot)
                    proposal = self.learning.propose(snapshot)
                    payload = self._payload(self.learning.last_report, proposal)
                    self.database.event("ENGINE_EVALUATED", payload, symbol=canonical)
                    self.database.status(f"scan:{canonical}", payload)
                    if proposal is None:
                        self.database.event(
                            "NO_TRADE_PROPOSAL",
                            {"reason": "ENGINE_THRESHOLD_OR_DIRECTION", "report": payload},
                            symbol=canonical,
                        )
                        continue
                    summary["proposals"] += 1
                    self.database.save_proposal(proposal)
                    self.database.event(
                        "TRADE_PROPOSAL_CREATED",
                        {
                            "proposal_id": proposal.proposal_id,
                            "engine": proposal.context.get("engine", ""),
                            "score": proposal.score,
                            "confidence": proposal.confidence,
                            "probability": proposal.probability,
                            "side": proposal.side,
                            "entry": proposal.entry_price,
                            "stop_loss": proposal.stop_loss,
                            "take_profit": proposal.take_profit,
                        },
                        symbol=canonical,
                        proposal_id=proposal.proposal_id,
                    )
                    result = self.execution.submit(proposal)
                    if result.status in {"FILLED", "DRY_RUN"}:
                        summary["filled"] += 1
                    elif result.status == "EXECUTION_BLOCKED":
                        summary["blocked"] += 1
                    else:
                        summary["rejected"] += 1
                except MarketDataError as exc:
                    self.database.event("MARKET_DATA_REJECTED", {"reason": str(exc)}, symbol=canonical)
                except Exception as exc:
                    LOG.exception("symbol cycle failed for %s", canonical)
                    self.database.event("RUNTIME_ERROR", {"error": str(exc)}, symbol=canonical)
        if perform_scan:
            self._record_lifecycle("MARKET_DATA_VERIFIED", "scan cycle completed", symbols=summary["symbols"])
            self._record_lifecycle("ENGINE_ANALYSIS", "engine evaluation completed")
            if summary["proposals"]:
                self._record_lifecycle("PROPOSAL_CREATED", "proposal stage completed", proposals=summary["proposals"])
                self._record_lifecycle("RISK_VALIDATED", "execution preflight stage entered")
                self._record_lifecycle("BROKER_VALIDATED", "broker submission stage entered")

        self.management.monitor()
        summary["closed"] = self.feedback.reconcile_closed_trades(self.gateway)
        summary["expired"] = self._expire_feedback()
        if perform_scan and summary["filled"]:
            self._record_lifecycle("ORDER_EXECUTED", "one or more orders filled", filled=summary["filled"])
        if perform_scan:
            self._record_lifecycle("POSITION_MANAGED", "position manager cycle completed")
            if summary["closed"]:
                self._record_lifecycle("POSITION_CLOSED", "closed positions reconciled", closed=summary["closed"])
            self._record_lifecycle("DATABASE_UPDATED", "cycle state persisted")
            self._record_lifecycle("COOLDOWN", "cycle complete")
            self._record_lifecycle("NEXT_SCAN", "awaiting configured scan interval")

        self.database.status(
            "runtime",
            {
                "last_cycle": datetime.now(timezone.utc).isoformat(),
                "scan_performed": bool(perform_scan),
                "scan_interval_seconds": int(self.config.scan_seconds),
                **summary,
            },
        )
        now_monotonic = time.monotonic()
        if now_monotonic - self._last_market_feed_persist_at >= 0.5:
            self.database.status(
                "market_feed",
                {
                    **self.market_data.live_tick_status(),
                    "ticks": self.market_data.live_tick_rows(),
                    "listener_running": self.websocket.running,
                    "connected_clients": self.websocket.connected_clients,
                    "received_events": self.websocket.received_events,
                    "last_transport_event_at": self.websocket.last_event_at,
                },
            )
            self._last_market_feed_persist_at = now_monotonic
        if perform_scan and time.monotonic() - self._last_db_checkpoint_at >= 300.0:
            checkpoint = self.database.checkpoint()
            self.database.status("database_health", checkpoint)
            self._last_db_checkpoint_at = time.monotonic()
        self._write_heartbeat("RUNNING")
        return summary

    def run(self) -> int:
        if not self.websocket.start():
            raise RuntimeError("live WebSocket tick listener failed to start")
        try:
            self.gateway.connect()
            self._write_heartbeat("RUNNING")
            self.database.status(
                "service",
                {
                    "state": "CONNECTED",
                    "mode": self.gateway.mode,
                    "market_data_source": "websocket",
                    "websocket_host": self.websocket.host,
                    "websocket_port": self.websocket.port,
                    "max_daily_trades": int(self.config.max_daily_trades),
                    "max_open_trades": int(self.config.max_open_trades),
                    "daily_loss_cap_usd": float(self.config.daily_loss_limit_usd),
                    "max_trades_per_symbol": int(self.config.max_trades_per_symbol),
                    "execution_mode": self.config.execution_mode,
                },
            )
            signal.signal(signal.SIGTERM, self.stop)
            signal.signal(signal.SIGINT, self.stop)
            next_scan_at = time.monotonic()
            while not self._stop:
                now = time.monotonic()
                perform_scan = now >= next_scan_at
                self.run_once(perform_scan=perform_scan)
                if perform_scan:
                    next_scan_at = time.monotonic() + max(1.0, float(self.config.scan_seconds))
                time.sleep(self.config.poll_seconds)
        finally:
            self.websocket.stop()
            self.gateway.shutdown()
            self._write_heartbeat("STOPPED")
            self.database.status("service", {"state": "STOPPED", "market_data_source": "websocket"})
        return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CipherFX modular platform")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    settings = load_settings()
    if args.dry_run:
        settings = settings.with_dry_run(True)
    return ModularTradingRuntime(settings).run()
