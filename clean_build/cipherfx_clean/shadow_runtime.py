"""Restart-safe shadow-forward runner with no broker execution capability."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json

from .contracts import RawTick, TradeDecision
from .shadow import ShadowOutcome, ShadowPosition, advance_shadow, create_shadow_position
from .store import EvidenceStore


@dataclass(frozen=True)
class ShadowAdvance:
    shadow_id: str
    status: str
    duplicate_tick: bool
    outcome: ShadowOutcome | None


class PersistentShadowRunner:
    """Persist every state transition and recover from the latest event."""

    def __init__(self, store: EvidenceStore):
        self._store = store

    def submit(
        self,
        decision: TradeDecision,
        *,
        state_id: str,
        expires_at,
        extra_cost_return: float = 0.0,
    ) -> ShadowPosition:
        position = create_shadow_position(
            decision,
            state_id=state_id,
            expires_at=expires_at,
            extra_cost_return=extra_cost_return,
        )
        self._store.write_shadow_position(position)
        return position

    def advance_tick(self, tick: RawTick) -> tuple[ShadowAdvance, ...]:
        results = []
        tick_id = _digest((tick.symbol, tick.timestamp.isoformat(), tick.bid, tick.ask))
        for position in self._store.load_active_shadow_positions(symbol=tick.symbol):
            mark_id = _digest((position.shadow_id, tick_id))
            existing = self._store.shadow_mark(mark_id)
            if existing is not None:
                results.append(ShadowAdvance(
                    position.shadow_id,
                    existing["position"]["status"],
                    True,
                    self._store.load_shadow_outcome(position.shadow_id),
                ))
                continue
            latest = self._store.latest_shadow_mark(position.shadow_id)
            if latest is not None and tick.timestamp < latest[0]:
                raise ValueError("shadow ticks must be chronological")
            updated, outcome = advance_shadow(position, tick)
            self._store.write_shadow_mark(mark_id, position.shadow_id, tick.timestamp, {
                "position": updated,
                "tick": tick,
                "source_tick_id": tick_id,
            })
            if outcome is not None:
                self._store.write_shadow_outcome(outcome)
            results.append(ShadowAdvance(updated.shadow_id, updated.status, False, outcome))
        return tuple(results)


def shadow_scorecard(outcomes: tuple[ShadowOutcome, ...]) -> dict[str, dict[str, float | int | None]]:
    grouped: dict[str, list[ShadowOutcome]] = {}
    for outcome in outcomes:
        grouped.setdefault(outcome.edge_id, []).append(outcome)
    result = {}
    for edge_id, rows in sorted(grouped.items()):
        measured = tuple(row for row in rows if row.r_multiple is not None)
        wins = tuple(row for row in measured if row.r_multiple > 0)
        result[edge_id] = {
            "total_outcomes": len(rows),
            "executed_outcomes": len(measured),
            "unfilled_expired": sum(row.entry_at is None for row in rows),
            "win_rate": len(wins) / len(measured) if measured else None,
            "expectancy_r": sum(row.r_multiple for row in measured) / len(measured) if measured else None,
            "average_mfe_r": sum(row.mfe_r for row in measured if row.mfe_r is not None) / sum(row.mfe_r is not None for row in measured) if any(row.mfe_r is not None for row in measured) else None,
            "average_mae_r": sum(row.mae_r for row in measured if row.mae_r is not None) / sum(row.mae_r is not None for row in measured) if any(row.mae_r is not None for row in measured) else None,
        }
    return result


def _digest(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()
