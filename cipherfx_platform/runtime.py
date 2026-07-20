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
from .planning import PremarketPlanningEngine
from portfolio_intelligence import PortfolioIntelligenceEngine

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
        self.planning = PremarketPlanningEngine(
            self.database,
            self.learning,
            self.config,
            interval_seconds=float(os.getenv("MT5_PREMARKET_PLAN_INTERVAL_SECONDS", "300")),
        )
        self.portfolio_intelligence = PortfolioIntelligenceEngine()
        self._last_portfolio_event_at = 0.0
        self._last_portfolio_event_signature = ""
        self.heartbeat_path = Path(
            os.getenv(
                "MT5_HEARTBEAT_FILE",
                str(settings.paths.app_dir / "state" / "mt5_heartbeat.json"),
            )
        )
        self._stop = False
        self._last_market_feed_persist_at = 0.0
        self._last_db_checkpoint_at = 0.0
        self._last_planning_at = 0.0
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

    def _wait_for_initial_feed(self) -> bool:
        """Avoid consuming the first scan before the live WebSocket has a tick."""
        timeout = max(
            5.0,
            min(120.0, float(os.getenv("MT5_STARTUP_HEALTH_GRACE_SECONDS", "90"))),
        )
        deadline = time.monotonic() + timeout
        last_status = {}
        while not self._stop and time.monotonic() < deadline:
            last_status = self.market_data.live_tick_status()
            if int(last_status.get("fresh_symbols", 0) or 0) > 0:
                self.database.event(
                    "MARKET_FEED_READY",
                    {"fresh_symbols": last_status.get("fresh_symbols", 0)},
                )
                return True
            time.sleep(0.2)
        self.database.event(
            "MARKET_FEED_STARTUP_TIMEOUT",
            {
                "timeout_seconds": timeout,
                "fresh_symbols": last_status.get("fresh_symbols", 0),
                "stale_symbols": last_status.get("stale_symbols", []),
            },
        )
        return False

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

    def _position_engine(self, symbol: str) -> str:
        """Attribute an open position from symbol metadata only."""
        canonical = self.config.canonical_symbol(symbol)
        group = self.config.group_for_symbol(canonical)
        if group in {"forex", "indices", "metals"}:
            return {"forex": "FOREX", "indices": "INDICES", "metals": "METALS"}[group]
        try:
            info = self.gateway.symbol_info(symbol)
            text = " ".join(
                str(value or "") for value in (
                    getattr(info, "path", ""),
                    getattr(info, "description", ""),
                    symbol,
                )
            ).upper()
        except Exception:
            text = str(symbol or "").upper()
        if any(token in text for token in ("METAL", "XAU", "XAG", "GOLD", "SILVER")):
            return "METALS"
        if any(token in text for token in ("FOREX", "FX", "CURRENCY")):
            return "FOREX"
        if any(token in text for token in ("INDEX", "INDICES", "CASH", "GER40", "UK100", "US30", "US100", "US500")):
            return "INDICES"
        return "UNATTRIBUTED"

    def _publish_portfolio_intelligence(self) -> None:
        """Publish portfolio facts without participating in any trade decision."""
        try:
            account = self.gateway.account_info()
            account_payload = dict(vars(account)) if hasattr(account, "__dict__") else {}
            positions = [
                {
                    "ticket": position.ticket,
                    "symbol": position.symbol,
                    "side": position.direction,
                    "volume": position.volume,
                    "price_open": position.price_open,
                    "price_current": position.price_current,
                    "sl": position.sl,
                    "tp": position.tp,
                    "profit": position.profit,
                    "engine": self._position_engine(position.symbol),
                    "risk_amount": float(
                        (self.database.position_baseline(position.ticket) or {}).get("initial_risk") or 0.0
                    ),
                }
                for position in self.gateway.positions()
            ]
            orders = [
                {
                    "ticket": order.ticket,
                    "symbol": order.symbol,
                    "side": order.side,
                    "volume": order.volume,
                    "price_open": order.price_open,
                    "sl": order.sl,
                    "tp": order.tp,
                    "state": order.state,
                }
                for order in self.gateway.orders()
            ]
            feed = self.market_data.live_tick_status()
            operational = {
                "mt5_connected": bool(getattr(account, "terminal_connected", False)),
                "trade_allowed": bool(getattr(account, "trade_allowed", False)),
                "account_trade_allowed": bool(getattr(account, "account_trade_allowed", False)),
                "bridge_mode": self.gateway.mode,
                "market_data": feed,
                "stale_tick_count": len(feed.get("stale_symbols", [])),
                "websocket": {
                    "running": self.websocket.running,
                    "connected_clients": self.websocket.connected_clients,
                    "received_events": self.websocket.received_events,
                },
            }
            snapshot = self.portfolio_intelligence.snapshot(
                account=account_payload,
                positions=positions,
                orders=orders,
                proposal_states=self.database.portfolio_proposal_states(),
                closed_summaries=self.database.portfolio_closed_summaries(),
                operational=operational,
            )
            self.database.status("portfolio_intelligence", snapshot)
            signature = json.dumps(
                {
                    "health": snapshot["portfolio_health"],
                    "open_positions": snapshot["exposure"]["open_positions"],
                    "pending_proposals": snapshot["proposals"]["pending"],
                },
                sort_keys=True,
            )
            now = time.monotonic()
            if (
                signature != self._last_portfolio_event_signature
                or now - self._last_portfolio_event_at >= 30.0
            ):
                self.database.event(
                    "PORTFOLIO_INTELLIGENCE",
                    {
                        "health": snapshot["portfolio_health"],
                        "risk": snapshot.get("risk", {}),
                        "open_positions": snapshot["exposure"]["open_positions"],
                        "pending_proposals": snapshot["proposals"]["pending"],
                    },
                )
                self._last_portfolio_event_signature = signature
                self._last_portfolio_event_at = now
        except Exception as exc:
            LOG.exception("portfolio intelligence publication failed")
            self.database.status(
                "portfolio_intelligence",
                {
                    "contract_version": "portfolio-intelligence-v1",
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "status": "UNAVAILABLE",
                    "reason": str(exc),
                    "authority": {
                        "informational_only": True,
                        "trade_decision_authority": False,
                    },
                },
            )

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
        self._publish_portfolio_intelligence()
        now_monotonic = time.monotonic()
        if now_monotonic - self._last_planning_at >= self.planning.interval_seconds:
            try:
                planning = self.planning.refresh()
                self.database.status("planning", planning)
                self.database.event(
                    "PREMARKET_PLANS_REFRESHED",
                    {
                        "target_market_date": planning.get("target_market_date"),
                        "symbols": planning.get("symbols", 0),
                        "planned": planning.get("planned", 0),
                        "watching": planning.get("watching", 0),
                        "waiting_for_snapshot": planning.get("waiting_for_snapshot", 0),
                    },
                )
            except Exception as exc:
                LOG.exception("premarket planning refresh failed")
                self.database.status(
                    "planning",
                    {
                        "status": "ERROR",
                        "source": "persisted_market_snapshots",
                        "reason": str(exc),
                        "planned_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
            finally:
                self._last_planning_at = now_monotonic
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
        if time.monotonic() - self._last_db_checkpoint_at >= 300.0:
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
            self._wait_for_initial_feed()
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
