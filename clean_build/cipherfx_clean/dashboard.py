"""Read-only projection of clean-build evidence for a dashboard consumer."""

from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import sqlite3

from .contracts import TradeDecision, TradeOutcome
from .intelligence.model import IntelligenceReport


def intelligence_view(report: IntelligenceReport) -> dict[str, object]:
    return {
        "symbol": report.symbol,
        "observed_at": report.observed_at,
        "timeframes": list(report.chart.timeframes),
        "evidence": list(report.evidence),
        "regimes": {key: asdict(value) for key, value in report.regimes.items()},
        "zones": {key: [asdict(item) for item in value] for key, value in report.zones.items()},
        "liquidity": {key: [asdict(item) for item in value] for key, value in report.liquidity.items()},
        "fair_value_gaps": {key: [asdict(item) for item in value] for key, value in report.fair_value_gaps.items()},
        "displacement": {key: asdict(item) for key, item in report.displacement.items()},
        "volatility": {key: asdict(item) for key, item in report.volatility.items()},
        "session_context": asdict(report.session_context) if report.session_context is not None else None,
        "session_profiles": {
            key: asdict(item)
            for key, item in report.session_profiles.items()
        },
        "asia_ranges": [asdict(item) for item in report.asia_ranges],
        "london_sweep_statistics": (
            asdict(report.london_sweep_statistics)
            if report.london_sweep_statistics is not None
            else None
        ),
        "london_sweep_outcomes": [asdict(item) for item in report.london_sweep_outcomes],
        "ny_transition_statistics": (
            asdict(report.ny_transition_statistics)
            if report.ny_transition_statistics is not None
            else None
        ),
        "ny_transition_outcomes": [asdict(item) for item in report.ny_transition_outcomes],
        "opening_range_history": [asdict(item) for item in report.opening_range_history],
        "opening_range_breakout_statistics": {
            key: asdict(item)
            for key, item in report.opening_range_breakout_statistics.items()
        },
        "opening_range_breakout_outcomes": [
            asdict(item) for item in report.opening_range_breakout_outcomes
        ],
        "false_opening_range_breakout_statistics": {
            key: asdict(item)
            for key, item in report.false_opening_range_breakout_statistics.items()
        },
        "false_opening_range_breakout_outcomes": [
            asdict(item) for item in report.false_opening_range_breakout_outcomes
        ],
        "session_vwap_history": [asdict(item) for item in report.session_vwap_history],
        "current_session_vwap": (
            asdict(report.current_session_vwap)
            if report.current_session_vwap is not None
            else None
        ),
        "vwap": asdict(report.vwap) if report.vwap is not None else None,
        "vwap_deviation": asdict(report.vwap_deviation) if report.vwap_deviation is not None else None,
        "vwap_deviation_context": (
            asdict(report.vwap_deviation_context)
            if report.vwap_deviation_context is not None
            else None
        ),
        "vwap_reclaim_rejection_statistics": {
            key: asdict(item)
            for key, item in report.vwap_reclaim_rejection_statistics.items()
        },
        "vwap_reclaim_rejection_outcomes": [
            asdict(item) for item in report.vwap_reclaim_rejection_outcomes
        ],
        "tick_volume_context": {
            key: asdict(item) for key, item in report.tick_volume_context.items()
        },
        "relative_volume_context": {
            key: asdict(item) for key, item in report.relative_volume_context.items()
        },
        "trend_engine_context": {
            key: asdict(item) for key, item in report.trend_engine_context.items()
        },
        "trend_persistence_context": {
            key: asdict(item) for key, item in report.trend_persistence_context.items()
        },
        "mean_reversion_context": {
            key: asdict(item) for key, item in report.mean_reversion_context.items()
        },
        "breakout_engine_context": {
            key: asdict(item) for key, item in report.breakout_engine_context.items()
        },
        "breakout_quality_context": {
            key: asdict(item) for key, item in report.breakout_quality_context.items()
        },
        "failed_breakout_context": {
            key: asdict(item) for key, item in report.failed_breakout_context.items()
        },
        "reversal_engine_context": {
            key: asdict(item) for key, item in report.reversal_engine_context.items()
        },
        "continuation_engine_context": {
            key: asdict(item) for key, item in report.continuation_engine_context.items()
        },
        "cross_asset_context": {
            key: asdict(item) for key, item in report.cross_asset_context.items()
        },
        "correlation_context": {
            key: asdict(item) for key, item in report.correlation_context.items()
        },
        "correlation_breakdown_context": {
            key: asdict(item) for key, item in report.correlation_breakdown_context.items()
        },
        "market_regime_context": {
            key: asdict(item) for key, item in report.market_regime_context.items()
        },
        "regime_probability_context": {
            key: asdict(item) for key, item in report.regime_probability_context.items()
        },
        "strategy_routing_context": (
            asdict(report.strategy_routing_context)
            if report.strategy_routing_context is not None
            else None
        ),
        "pattern_outcome_context": {
            key: asdict(item) for key, item in report.pattern_outcome_context.items()
        },
        "failed_pattern_context": {
            key: asdict(item) for key, item in report.failed_pattern_context.items()
        },
        "realized_volatility": {
            key: asdict(item)
            for key, item in report.realized_volatility.items()
        },
        "atr_intelligence": {
            key: asdict(item)
            for key, item in report.atr_intelligence.items()
        },
        "volatility_regime_probability": {
            key: asdict(item)
            for key, item in report.volatility_regime_probability.items()
        },
        "volatility_of_volatility": {
            key: asdict(item)
            for key, item in report.volatility_of_volatility.items()
        },
        "compression": {
            key: asdict(item)
            for key, item in report.compression.items()
        },
        "expansion": {
            key: asdict(item)
            for key, item in report.expansion.items()
        },
    }


def outcome_view(outcome: TradeOutcome) -> dict[str, object]:
    return asdict(outcome)


def edge_view(scorecard: object, validation_state: str, live_monitor: object | None = None) -> dict[str, object]:
    """Read-only edge scorecard projection; it cannot affect execution."""
    values = {
        key: getattr(scorecard, key)
        for key in ("edge_id", "version", "sample_size", "win_rate", "expectancy", "profit_factor", "drawdown", "sharpe", "cost_total", "uncertainty")
        if hasattr(scorecard, key)
    }
    values["validation_state"] = validation_state
    values["live_monitor"] = live_monitor
    return values


def trade_evidence_view(*, decision: TradeDecision, chart_ids: tuple[str, ...], reasoning: tuple[str, ...], result: TradeOutcome | None) -> dict[str, object]:
    return {
        "decision_id": decision.decision_id,
        "symbol": decision.symbol,
        "action": decision.action,
        "entry": decision.entry,
        "stop": decision.stop,
        "target": decision.target,
        "edge_id": decision.edge_id,
        "evidence": list(decision.evidence),
        "reasoning": list(reasoning),
        "chart_ids": list(chart_ids),
        "result": outcome_view(result) if result else None,
    }


class EvidenceDashboard:
    """Query-only dashboard source over the canonical clean evidence database."""

    def __init__(self, database: str | Path):
        uri = f"file:{Path(database).resolve()}?mode=ro"
        self._conn = sqlite3.connect(uri, uri=True)
        self._conn.execute("PRAGMA query_only = ON")
        self._conn.row_factory = sqlite3.Row

    def close(self) -> None:
        self._conn.close()

    def overview(self) -> dict[str, object]:
        counts = {
            table: self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "datasets", "dataset_quality_reports", "market_states", "historical_states",
                "experiment_registry", "strategy_graveyard", "validation_reports", "edge_registry",
                "trade_decisions", "broker_events", "trade_outcomes", "release_artifacts",
            )
        }
        latest_quality = tuple(
            dict(row)
            for row in self._conn.execute(
                """
                SELECT d.symbol,q.status,q.observed_at,q.dataset_id
                  FROM dataset_quality_reports q
                  JOIN datasets d ON d.dataset_id=q.dataset_id
                 WHERE q.observed_at=(SELECT MAX(q2.observed_at) FROM dataset_quality_reports q2 WHERE q2.dataset_id=q.dataset_id)
                 ORDER BY d.symbol
                """
            )
        )
        memory = tuple(
            dict(row)
            for row in self._conn.execute(
                "SELECT symbol,COUNT(*) AS states,MIN(observed_at) AS first_state,MAX(observed_at) AS latest_state FROM historical_states GROUP BY symbol ORDER BY symbol"
            )
        )
        return {"counts": counts, "latest_dataset_quality": latest_quality, "memory_coverage": memory}

    def research(self) -> dict[str, object]:
        experiments = tuple(
            {**dict(row), "payload": json.loads(row["payload"])}
            for row in self._conn.execute(
                "SELECT experiment_id,status,payload FROM experiment_registry ORDER BY experiment_id"
            )
        )
        edges = tuple(
            {**dict(row), "payload": json.loads(row["payload"])}
            for row in self._conn.execute("SELECT edge_key,status,payload FROM edge_registry ORDER BY edge_key")
        )
        return {
            "experiments": experiments,
            "edges": edges,
            "graveyard_count": self._conn.execute("SELECT COUNT(*) FROM strategy_graveyard").fetchone()[0],
            "validation_count": self._conn.execute("SELECT COUNT(*) FROM validation_reports").fetchone()[0],
        }

    def trade(self, decision_id: str) -> dict[str, object] | None:
        decision = self._conn.execute(
            "SELECT decision_id,symbol,action,created_at,payload FROM trade_decisions WHERE decision_id=?",
            (decision_id,),
        ).fetchone()
        if decision is None:
            return None
        outcome = self._conn.execute(
            "SELECT status,payload FROM trade_outcomes WHERE decision_id=?", (decision_id,)
        ).fetchone()
        broker = tuple(
            {**dict(row), "payload": json.loads(row["payload"])}
            for row in self._conn.execute(
                "SELECT event_type,event_at,payload FROM broker_events WHERE decision_id=? ORDER BY event_at",
                (decision_id,),
            )
        )
        lineage = tuple(
            {**dict(row), "payload": json.loads(row["payload"])}
            for row in self._conn.execute(
                "SELECT event_id,state_id,outcome_id,payload FROM lineage_events WHERE decision_id=? ORDER BY event_id",
                (decision_id,),
            )
        )
        return {
            "decision": {**dict(decision), "payload": json.loads(decision["payload"])},
            "outcome": {**dict(outcome), "payload": json.loads(outcome["payload"])} if outcome else None,
            "broker_events": broker,
            "lineage": lineage,
        }
