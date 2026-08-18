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
from portfolio_intelligence import PortfolioIntelligenceEngine

LOG = logging.getLogger("cipherfx.platform")


class ModularTradingRuntime:
    """One active route: live tick -> H4 -> M15 -> M5 -> proposal -> execution."""

    ASSET_GROUPS = {"forex": "forex", "indices": "index", "metals": "metal"}
    LIFECYCLE_TRANSITIONS = {
        "IDLE": {"SCANNING", "RECOVERY"},
        "SCANNING": {"MARKET_DATA_VERIFIED", "RECOVERY"},
        "MARKET_DATA_VERIFIED": {"ENGINE_ANALYSIS", "RECOVERY"},
        "ENGINE_ANALYSIS": {"PROPOSAL_CREATED", "DATABASE_UPDATED", "RECOVERY"},
        "PROPOSAL_CREATED": {"RISK_VALIDATED", "DATABASE_UPDATED", "RECOVERY"},
        "RISK_VALIDATED": {"BROKER_VALIDATED", "DATABASE_UPDATED", "RECOVERY"},
        "BROKER_VALIDATED": {"ORDER_EXECUTED", "DATABASE_UPDATED", "RECOVERY"},
        "ORDER_EXECUTED": {"DATABASE_UPDATED", "RECOVERY"},
        "DATABASE_UPDATED": {"COOLDOWN", "RECOVERY"},
        "COOLDOWN": {"NEXT_SCAN", "RECOVERY"},
        "NEXT_SCAN": {"IDLE", "SCANNING", "RECOVERY"},
        "RECOVERY": {"IDLE", "NEXT_SCAN"},
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
        self.portfolio_intelligence = PortfolioIntelligenceEngine()
        self.heartbeat_path = Path(
            os.getenv(
                "MT5_HEARTBEAT_FILE",
                str(settings.paths.app_dir / "state" / "mt5_heartbeat.json"),
            )
        )
        self._stop = False
        self._last_market_feed_persist_at = 0.0
        self._last_db_checkpoint_at = 0.0
        self._last_portfolio_event_at = 0.0
        self._last_portfolio_event_signature = ""
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
            "route": "H4_M15_M5_SETUP",
            "market_data_source": "websocket",
        }
        temporary = Path(str(self.heartbeat_path) + ".tmp")
        temporary.write_text(json.dumps(payload, sort_keys=True))
        temporary.replace(self.heartbeat_path)

    def stop(self, *_args) -> None:
        self._stop = True
        self.gateway.request_shutdown()

    def _wait_for_initial_feed(self) -> bool:
        timeout = max(5.0, min(120.0, float(os.getenv("MT5_STARTUP_HEALTH_GRACE_SECONDS", "90"))))
        deadline = time.monotonic() + timeout
        last_status = {}
        while not self._stop and time.monotonic() < deadline:
            last_status = self.market_data.live_tick_status()
            if int(last_status.get("fresh_symbols", 0) or 0) > 0:
                self.database.event("MARKET_FEED_READY", {"fresh_symbols": last_status.get("fresh_symbols", 0)})
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
    def _snapshot_id(snapshot) -> str:
        frame_times = "|".join(
            f"{name}:{frame.last.timestamp.isoformat() if frame.last else ''}"
            for name, frame in sorted(snapshot.frames.items())
        )
        material = "|".join(
            (
                snapshot.symbol,
                snapshot.asset_class,
                snapshot.tick.timestamp.isoformat(),
                frame_times,
            )
        )
        return hashlib.sha256(material.encode()).hexdigest()[:32]

    @staticmethod
    def _decision_payload(report: dict, proposal=None) -> dict:
        setup = dict(report.get("setup") or {})
        frames = dict(setup.get("frames") or {})
        h4 = dict(frames.get("H4") or {})
        m15 = dict(frames.get("M15") or {})
        m5 = dict(frames.get("M5") or {})
        chart = dict(setup.get("chart_setup") or {})
        setup_direction = str(
            setup.get("setup_direction")
            or frames.get("setup_direction")
            or h4.get("direction")
            or chart.get("side")
            or "NO_TRADE"
        )
        side = str(setup.get("side") or chart.get("side") or "NO_TRADE")
        return {
            "engine": report.get("engine", "SETUP_ENGINE"),
            "decision": report.get("decision", "NO_SETUP"),
            "symbol": report.get("symbol", ""),
            "asset_class": report.get("asset_class", ""),
            "side": side,
            "setup_type": setup.get("setup_type", ""),
            "entry_model": setup.get("entry_model", ""),
            "h4_direction": setup_direction,
            "h4_direction_source": h4.get("direction_source", ""),
            "m15_aoi": bool(m15.get("aoi") or m15.get("available")),
            "m15_confirmation": bool(m15.get("confirmation") or m15.get("direction") in {"BUY", "SELL"}),
            "m15_retracement": bool(m15.get("retracement", False)),
            "m5_trigger": bool(chart.get("valid") or m5.get("valid")),
            "m5_trigger_type": chart.get("type") or m5.get("type") or "NONE",
            "trigger_source": chart.get("trigger_source") or m5.get("trigger_source") or "",
            "setup_valid": bool(setup.get("valid")),
            "rejection_reason": setup.get("rejection_reason", ""),
            "decision_role": setup.get("decision_role", "CHART_SETUP_WITH_MEMORY"),
            "memory_recognized": bool((setup.get("memory") or {}).get("recognized")),
            "memory": setup.get("memory", {}),
            "reasons": setup.get("reasons", ()),
            "proposal_id": proposal.proposal_id if proposal else "",
        }

    def _run_live_trigger(self, canonical: str) -> bool:
        """Evaluate one changed WebSocket symbol without waiting for full scan."""
        group = self.config.group_for_symbol(canonical)
        asset = self.ASSET_GROUPS.get(group)
        if not asset:
            return False
        try:
            snapshot = self.market_data.cached_snapshot(canonical, asset)
            proposal = self.learning.propose(snapshot)
            setup = dict((proposal.context if proposal else {}).get("setup") or {})
            chart = dict(setup.get("chart_setup") or {})
            if proposal is None or chart.get("trigger_source") != "LIVE_TICK":
                return False
            if self.database.proposal_seen(proposal.proposal_id):
                return False
            self.database.save_proposal(proposal)
            self.database.event(
                "LIVE_TICK_PROPOSAL_CREATED",
                {
                    "proposal_id": proposal.proposal_id,
                    "side": proposal.side,
                    "entry": proposal.entry_price,
                    "trigger_price": chart.get("trigger_price", proposal.entry_price),
                    "trigger_age_seconds": chart.get("trigger_age_seconds"),
                    "trigger_source": chart.get("trigger_source"),
                },
                symbol=canonical,
                proposal_id=proposal.proposal_id,
            )
            result = self.execution.submit(proposal)
            self.database.event(
                "LIVE_TICK_EXECUTION",
                {
                    "status": result.status,
                    "reason": result.reason,
                    "volume": result.volume,
                    "order_ticket": result.order_ticket,
                    "deal_ticket": result.deal_ticket,
                    "position_ticket": result.position_ticket,
                },
                symbol=canonical,
                proposal_id=proposal.proposal_id,
            )
            return True
        except MarketDataError:
            return False
        except Exception as exc:
            LOG.exception("live tick trigger failed for %s", canonical)
            self.database.event(
                "LIVE_TICK_TRIGGER_ERROR",
                {"error": f"{type(exc).__name__}:{str(exc)[:180]}"},
                symbol=canonical,
            )
            return False

    def _run_live_triggers(self) -> int:
        triggered = 0
        for symbol in self.market_data.drain_changed_symbols():
            if self._run_live_trigger(symbol):
                triggered += 1
        return triggered

    def _expire_feedback(self) -> int:
        expired = self.database.expired_proposals()
        for row in expired:
            self.learning.record_expired(
                row["asset_class"],
                row["proposal_id"],
                row["symbol"],
                {"source": "proposal_expiry"},
            )
            self.database.update_proposal_state(row["proposal_id"], "EXPIRED")
            self.database.event(
                "PROPOSAL_EXPIRED",
                {"reason": "proposal deadline elapsed"},
                symbol=row["symbol"],
                proposal_id=row["proposal_id"],
            )
        return len(expired)

    def _position_engine(self, _symbol: str) -> str:
        return "SETUP_ENGINE"

    def _publish_portfolio_intelligence(self) -> None:
        """Publish portfolio facts only; this method has no trade authority."""
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
                for position in (self.gateway.positions() or [])
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
                for order in (self.gateway.orders() or [])
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
                    "health": snapshot.get("portfolio_health"),
                    "open_positions": snapshot.get("exposure", {}).get("open_positions", 0),
                    "pending_proposals": snapshot.get("proposals", {}).get("pending", 0),
                },
                sort_keys=True,
            )
            now = time.monotonic()
            if signature != self._last_portfolio_event_signature or now - self._last_portfolio_event_at >= 30.0:
                self.database.event(
                    "PORTFOLIO_INTELLIGENCE",
                    {
                        "health": snapshot.get("portfolio_health"),
                        "risk": snapshot.get("risk", {}),
                        "open_positions": snapshot.get("exposure", {}).get("open_positions", 0),
                        "pending_proposals": snapshot.get("proposals", {}).get("pending", 0),
                    },
                )
                self._last_portfolio_event_signature = signature
                self._last_portfolio_event_at = now
        except Exception as exc:
            LOG.exception("portfolio intelligence publication failed")
            self.database.status(
                "portfolio_intelligence",
                {
                    "status": "UNAVAILABLE",
                    "reason": str(exc),
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "authority": {"informational_only": True, "trade_decision_authority": False},
                },
            )

    def run_once(self, *, perform_scan: bool = True) -> dict[str, int]:
        summary = {
            "symbols": 0,
            "snapshots": 0,
            "proposals": 0,
            "filled": 0,
            "blocked": 0,
            "rejected": 0,
            "closed": 0,
            "expired": 0,
            "live_triggers": 0,
        }
        decisions: list[dict] = []

        if not perform_scan:
            summary["live_triggers"] = self._run_live_triggers()

        if perform_scan:
            self._record_lifecycle("SCANNING", "configured scan cycle")
            for canonical in self.config.all_symbols():
                group = self.config.group_for_symbol(canonical)
                asset = self.ASSET_GROUPS.get(group)
                if not asset:
                    self.database.event(
                        "SYMBOL_UNSUPPORTED_GROUP",
                        {"group": group, "reason": "not in active setup universe"},
                        symbol=canonical,
                    )
                    continue
                summary["symbols"] += 1
                try:
                    snapshot = self.market_data.snapshot(canonical, asset)
                    summary["snapshots"] += 1
                    self.database.save_snapshot(self._snapshot_id(snapshot), snapshot)
                    proposal = self.learning.propose(snapshot)
                    decision = self._decision_payload(self.learning.last_report, proposal)
                    decisions.append(decision)
                    self.database.event(
                        "SETUP_DECISION",
                        decision,
                        symbol=canonical,
                        proposal_id=proposal.proposal_id if proposal else "",
                    )
                    if proposal is None:
                        continue

                    # The proposal identity is stable for the active M5 setup.
                    # Do not publish or submit the same setup again on every
                    # fast scan; execution idempotency remains the final guard.
                    if self.database.proposal_seen(proposal.proposal_id):
                        continue

                    summary["proposals"] += 1
                    self.database.save_proposal(proposal)
                    self.database.event(
                        "TRADE_PROPOSAL_CREATED",
                        {
                            "proposal_id": proposal.proposal_id,
                            "side": proposal.side,
                            "entry": proposal.entry_price,
                            "stop_loss": proposal.stop_loss,
                            "take_profit": proposal.take_profit,
                            "setup_type": proposal.context.get("setup", {}).get("setup_type", ""),
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
                    self.database.event(
                        "EXECUTION_DECISION",
                        {
                            "status": result.status,
                            "reason": result.reason,
                            "volume": result.volume,
                            "order_ticket": result.order_ticket,
                            "deal_ticket": result.deal_ticket,
                            "position_ticket": result.position_ticket,
                        },
                        symbol=canonical,
                        proposal_id=proposal.proposal_id,
                    )
                except MarketDataError as exc:
                    decisions.append(
                        {
                            "symbol": canonical,
                            "decision": "NO_SETUP",
                            "reason": "MARKET_DATA_UNAVAILABLE",
                            "detail": str(exc),
                        }
                    )
                    self.database.event(
                        "MARKET_DATA_REJECTED",
                        {"reason": str(exc), "decision": "NO_SETUP"},
                        symbol=canonical,
                    )
                except Exception as exc:
                    LOG.exception("symbol cycle failed for %s", canonical)
                    decisions.append(
                        {
                            "symbol": canonical,
                            "decision": "NO_SETUP",
                            "reason": "RUNTIME_ERROR",
                            "detail": f"{type(exc).__name__}:{str(exc)[:180]}",
                        }
                    )
                    self.database.event(
                        "RUNTIME_ERROR",
                        {"error": str(exc), "decision": "NO_SETUP"},
                        symbol=canonical,
                    )

            self._record_lifecycle("MARKET_DATA_VERIFIED", "scan cycle complete", snapshots=summary["snapshots"])
            self._record_lifecycle("ENGINE_ANALYSIS", "asset-specific learning engines evaluated all snapshots")
            if summary["proposals"]:
                self._record_lifecycle("PROPOSAL_CREATED", "immutable proposals created", proposals=summary["proposals"])
                self._record_lifecycle("RISK_VALIDATED", "operational checks completed by execution")
                self._record_lifecycle("BROKER_VALIDATED", "broker response recorded")
                if summary["filled"]:
                    self._record_lifecycle("ORDER_EXECUTED", "proposal accepted by MT5", filled=summary["filled"])

        try:
            self.management.monitor()
        except Exception as exc:
            LOG.exception("position management cycle failed")
            self.database.event("POSITION_MANAGEMENT_ERROR", {"error": str(exc)})
        try:
            summary["closed"] = self.feedback.reconcile_closed_trades(self.gateway)
        except Exception as exc:
            LOG.exception("closed-trade reconciliation failed")
            self.database.event("LEARNING_RECONCILIATION_ERROR", {"error": str(exc)})
        summary["expired"] = self._expire_feedback()
        self._publish_portfolio_intelligence()

        if perform_scan:
            self.database.status(
                "scan",
                {
                    "route": "H4_POI_5M_SWEEP_MSS_FVG_OB",
                    "frames": ["H4", "M5"],
                    "decision_authority": "LearningEngine",
                    "score_logic": "NOT_USED",
                    "decisions": decisions,
                    "summary": summary,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            self._record_lifecycle("DATABASE_UPDATED", "scan, execution, management and learning persisted")
            self._record_lifecycle("COOLDOWN", "cycle complete")
            self._record_lifecycle("NEXT_SCAN", "awaiting next live scan")

        self.database.status(
            "runtime",
            {
                "last_cycle": datetime.now(timezone.utc).isoformat(),
                "scan_performed": bool(perform_scan),
                "scan_interval_seconds": int(self.config.scan_seconds),
                "route": "H4_POI_5M_SWEEP_MSS_FVG_OB",
                "market_data_source": "websocket",
                **summary,
            },
        )

        now = time.monotonic()
        if now - self._last_market_feed_persist_at >= 0.5:
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
            self._last_market_feed_persist_at = now
        if now - self._last_db_checkpoint_at >= 300.0:
            checkpoint = self.database.checkpoint()
            self.database.status("database_health", checkpoint)
            self._last_db_checkpoint_at = now
        self._write_heartbeat("RUNNING")
        return summary

    def run(self) -> int:
        if not self.websocket.start():
            raise RuntimeError("live WebSocket tick listener failed to start")
        try:
            self.gateway.connect()
            self._wait_for_initial_feed()
            self.database.status(
                "service",
                {
                    "state": "CONNECTED",
                    "mode": self.gateway.mode,
                    "market_data_source": "websocket",
                    "route": "H4_POI_5M_SWEEP_MSS_FVG_OB",
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
    parser = argparse.ArgumentParser(description="CipherFX active setup bot")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    settings = load_settings()
    if args.dry_run:
        settings = settings.with_dry_run(True)
    return ModularTradingRuntime(settings).run()
