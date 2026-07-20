from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .contracts import Candle, Frame, MarketSnapshot, Tick
from .database import DatabaseLayer
from .engines.router import LearningEngine

SAST = timezone(timedelta(hours=2), name="SAST")
PLANNING_TIMEFRAMES = ("MN1", "W1", "D1", "H4", "H1", "M30", "M15", "M5", "M3", "M1")


class PremarketPlanningEngine:
    """Builds informational next-session plans from persisted market snapshots.

    A plan is not a TradeProposal, is not saved in trade_proposals, and is never
    sent to ExecutionEngine. The live scan must independently revalidate the
    current WebSocket snapshot before any order can exist.
    """

    def __init__(
        self,
        database: DatabaseLayer,
        learning: LearningEngine,
        config,
        interval_seconds: float = 300.0,
    ):
        self.database = database
        self.learning = learning
        self.config = config
        self.interval_seconds = max(60.0, float(interval_seconds or 300.0))

    @staticmethod
    def next_market_date(value: datetime | date | None = None) -> date:
        current = value or datetime.now(SAST)
        if isinstance(current, datetime):
            local_date = current.astimezone(SAST).date() if current.tzinfo else current.date()
        else:
            local_date = current
        candidate = local_date + timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate += timedelta(days=1)
        return candidate

    @staticmethod
    def _as_datetime(value: Any) -> datetime:
        raw = str(value or "").strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _target_window(symbol: str, asset_class: str) -> str:
        code = str(symbol or "").upper()
        if asset_class in {"forex", "metal"}:
            return "Sunday 23:00 to Saturday 02:01 SAST"
        if code in {"NAS100", "US30", "SPX500"}:
            return "Monday-Friday 15:30 to 22:00 SAST"
        if code in {"GER40", "UK100", "FRA40", "EU50"}:
            return "Monday-Friday 10:00 to 19:30 SAST"
        if code == "JP225":
            return "Monday-Friday 02:00 to 10:00 SAST"
        return "configured market window"

    @staticmethod
    def _snapshot_from_row(row: dict[str, Any]) -> MarketSnapshot:
        payload = json.loads(row.get("payload_json") or "{}")
        frame_payload = payload.get("frames") or {}
        frames: dict[str, Frame] = {}
        for timeframe in PLANNING_TIMEFRAMES:
            candles = []
            for item in frame_payload.get(timeframe) or []:
                candles.append(
                    Candle(
                        timestamp=PremarketPlanningEngine._as_datetime(item["timestamp"]),
                        open=float(item["open"]),
                        high=float(item["high"]),
                        low=float(item["low"]),
                        close=float(item["close"]),
                        volume=float(item.get("volume") or 0.0),
                    )
                )
            frames[timeframe] = Frame(timeframe, tuple(candles))
        tick_payload = payload.get("tick") or {}
        tick_timestamp = PremarketPlanningEngine._as_datetime(
            tick_payload.get("timestamp") or row.get("tick_timestamp")
        )
        received_at = PremarketPlanningEngine._as_datetime(
            tick_payload.get("received_at") or tick_payload.get("timestamp") or row.get("captured_at")
        )
        tick = Tick(
            symbol=str(row.get("symbol") or "").upper(),
            bid=float(tick_payload.get("bid") or 0.0),
            ask=float(tick_payload.get("ask") or 0.0),
            timestamp=tick_timestamp,
            received_at=received_at,
        )
        return MarketSnapshot(
            symbol=str(row.get("symbol") or "").upper(),
            asset_class=str(row.get("asset_class") or "").lower(),
            frames=frames,
            tick=tick,
            captured_at=PremarketPlanningEngine._as_datetime(row.get("captured_at")),
            freshness=payload.get("freshness") or {},
        )

    def _asset_class(self, symbol: str) -> str:
        group = str(self.config.group_for_symbol(symbol) or "").lower()
        return {"forex": "forex", "metals": "metal", "indices": "index"}.get(group, "")

    def refresh(self, now: datetime | None = None) -> dict[str, Any]:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        current = current.astimezone(timezone.utc)
        local = current.astimezone(SAST)
        target = self.next_market_date(local)
        target_label = target.isoformat()
        symbols = list(self.config.all_symbols())
        rows = {
            str(row.get("symbol") or "").upper(): row
            for row in self.database.latest_snapshots(symbols)
        }
        summary = {
            "status": "READY",
            "source": "persisted_market_snapshots",
            "authority": "LearningEngine",
            "execution": "informational_only",
            "target_market_date": target_label,
            "target_market_day": target.strftime("%A"),
            "planned_at": current.isoformat(),
            "refresh_interval_seconds": self.interval_seconds,
            "symbols": len(symbols),
            "planned": 0,
            "watching": 0,
            "waiting_for_snapshot": 0,
            "plans": [],
        }

        for symbol in symbols:
            canonical = str(symbol or "").strip().upper()
            asset_class = self._asset_class(canonical)
            row = rows.get(canonical)
            event_id = hashlib.sha256(
                f"premarket|{target_label}|{canonical}".encode()
            ).hexdigest()[:32]
            setup_id = f"plan:{target_label}:{canonical}"
            base = {
                "plan_id": event_id,
                "setup_id": setup_id,
                "symbol": canonical,
                "asset_class": asset_class,
                "target_market_date": target_label,
                "target_market_day": target.strftime("%A"),
                "target_window_sast": self._target_window(canonical, asset_class),
                "planned_at": current.isoformat(),
                "timeframes": list(PLANNING_TIMEFRAMES),
                "execution_status": "WAITING_FOR_LIVE_REVALIDATION",
                "execution_authority": "ExecutionEngine",
                "is_trade_proposal": False,
            }
            status = "WAITING_FOR_SNAPSHOT"
            reason = "NO_CACHED_SNAPSHOT"
            report: dict[str, Any] = {}
            snapshot_age_hours = None

            if row:
                try:
                    snapshot = self._snapshot_from_row(row)
                    snapshot_age_hours = round(
                        max(0.0, (current - snapshot.captured_at).total_seconds()) / 3600.0,
                        3,
                    )
                    proposal = self.learning.propose(snapshot)
                    report = dict(self.learning.last_report or {})
                    base.update(
                        {
                            "source_snapshot_at": snapshot.captured_at.isoformat(),
                            "source_snapshot_age_hours": snapshot_age_hours,
                            "latest_candle_ids": {
                                timeframe: (
                                    frame.last.timestamp.isoformat()
                                    if frame.last is not None
                                    else ""
                                )
                                for timeframe, frame in snapshot.frames.items()
                            },
                            "score_breakdown": {
                                "timeframes": report.get("scores") or {},
                                "totals": report.get("totals") or {},
                                "feature_scores": report.get("feature_scores") or {},
                                "threshold": report.get("threshold"),
                                "reasons": report.get("reasons") or [],
                            },
                        }
                    )
                    if proposal is not None:
                        status = "PLANNED"
                        reason = "LEARNING_CANDIDATE_FROM_CACHED_SNAPSHOT"
                        base.update(
                            {
                                "direction": proposal.side,
                                "entry": proposal.entry_price,
                                "stop": proposal.stop_loss,
                                "target": proposal.take_profit,
                                "confidence": proposal.confidence,
                                "probability": proposal.probability,
                                "score": proposal.score,
                                "reasoning": list(proposal.reasoning),
                                "candidate_proposal_id": proposal.proposal_id,
                            }
                        )
                        summary["planned"] += 1
                    else:
                        status = "WATCHING"
                        reason = "NO_CURRENT_QUALIFICATION"
                        summary["watching"] += 1
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    status = "WAITING_FOR_SNAPSHOT"
                    reason = f"SNAPSHOT_UNREADABLE:{type(exc).__name__}"
                    summary["waiting_for_snapshot"] += 1
            else:
                summary["waiting_for_snapshot"] += 1

            base["source_snapshot_age_hours"] = snapshot_age_hours
            self.database.save_premarket_plan(
                event_id=event_id,
                symbol=canonical,
                setup_id=setup_id,
                status=status,
                reason=reason,
                payload=base,
            )
            summary["plans"].append(
                {
                    "symbol": canonical,
                    "asset_class": asset_class,
                    "status": status,
                    "reason": reason,
                    "score": base.get("score"),
                    "direction": base.get("direction", ""),
                    "target_market_date": target_label,
                    "source_snapshot_age_hours": snapshot_age_hours,
                }
            )
        return summary
