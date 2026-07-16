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

from .database import DatabaseLayer
from .engines import MarketIntelligenceEngine
from .execution import ExecutionEngine
from .feedback import LearningFeedbackEngine
from .management import TradeManagementEngine
from .market_data import MarketDataEngine, MarketDataError

LOG = logging.getLogger("cipherfx.platform")


class ModularTradingRuntime:
    """Authoritative orchestration of market snapshots, proposals, and operations."""

    ASSET_GROUPS = {"forex": "forex", "indices": "index", "metals": "metal"}

    def __init__(self, settings: ServiceSettings):
        self.settings = settings
        self.config = settings.runtime
        self.database = DatabaseLayer(settings.paths.database_file)
        self.gateway = MT5Gateway(self.config)
        self.market_data = MarketDataEngine(self.gateway, self.config.history_bars)
        self.intelligence = MarketIntelligenceEngine(self.database)
        self.execution = ExecutionEngine(self.gateway, self.config, self.database)
        self.management = TradeManagementEngine(self.gateway, self.database)
        self.feedback = LearningFeedbackEngine(self.database, self.intelligence.record_outcome)
        self.heartbeat_path = Path(
            os.getenv(
                "MT5_HEARTBEAT_FILE",
                str(settings.paths.app_dir / "state" / "mt5_heartbeat.json"),
            )
        )
        self._stop = False

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

    def run_once(self) -> dict[str, int]:
        summary = {
            "symbols": 0,
            "proposals": 0,
            "filled": 0,
            "blocked": 0,
            "rejected": 0,
            "closed": 0,
            "expired": 0,
        }
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
                proposal = self.intelligence.propose(snapshot)
                payload = self._payload(self.intelligence.last_report, proposal)
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

        self.management.snapshot()
        summary["closed"] = self.feedback.reconcile_closed_trades(self.gateway)
        summary["expired"] = self._expire_feedback()
        self.database.status(
            "runtime",
            {
                "last_cycle": datetime.now(timezone.utc).isoformat(),
                **summary,
            },
        )
        self._write_heartbeat("RUNNING")
        return summary

    def run(self) -> int:
        self.gateway.connect()
        self._write_heartbeat("RUNNING")
        self.database.status("service", {"state": "CONNECTED", "mode": self.gateway.mode})
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        while not self._stop:
            self.run_once()
            time.sleep(self.config.poll_seconds)
        self.gateway.shutdown()
        self._write_heartbeat("STOPPED")
        self.database.status("service", {"state": "STOPPED"})
        return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CipherFX modular platform")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    settings = load_settings()
    if args.dry_run:
        settings = settings.with_dry_run(True)
    return ModularTradingRuntime(settings).run()
