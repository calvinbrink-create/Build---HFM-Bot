"""Descriptive regime-to-hypothesis routing with no hidden trade gate."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .strategies import default_knowledge


@dataclass(frozen=True)
class StrategyRoute:
    regime: str
    session: str
    candidate_ids: tuple[str, ...]
    reason: str
    strategy_families: tuple[str, ...] = ()
    informational_only: bool = True


class StrategyRouter:
    def __init__(self, mapping: Mapping[tuple[str, str], tuple[str, ...]] | None = None):
        self._mapping = dict(mapping or {})

    def candidates(self, regime: str, session: str) -> StrategyRoute:
        candidates = ()
        route_kind = "NO_REGISTERED_CANDIDATE"
        for key, label in (
            ((regime, session), "EXPLICIT_ROUTE"),
            ((regime, "*"), "REGIME_ROUTE"),
            (("*", session), "SESSION_ROUTE"),
            (("*", "*"), "DEFAULT_ROUTE"),
        ):
            if key in self._mapping:
                candidates = tuple(self._mapping[key])
                route_kind = label
                break
        known = {item.strategy_id: item for item in default_knowledge()}
        families = tuple(
            dict.fromkeys(
                known[item_id].family
                for item_id in candidates
                if item_id in known
            )
        )
        return StrategyRoute(
            regime,
            session,
            candidates,
            route_kind,
            families,
            True,
        )


def default_strategy_mapping() -> dict[tuple[str, str], tuple[str, ...]]:
    """Return an explicit observational mapping for the registered hypotheses.

    The wildcard session entries describe which research families are relevant
    to a current regime. They do not select, approve, reject, delay, or size a
    trade; proposal and execution authorities remain separate modules.
    """

    return {
        ("TREND_UP", "*"): (
            "impulse_continuation",
            "price_action_rejection",
            "smc_sweep_reclaim",
        ),
        ("TREND_DOWN", "*"): (
            "impulse_continuation",
            "price_action_rejection",
            "smc_sweep_reclaim",
        ),
        ("RANGE", "*"): (
            "value_reversion",
            "price_action_rejection",
        ),
        ("QUIET", "*"): (
            "value_reversion",
            "price_action_rejection",
        ),
        ("BREAKOUT", "*"): (
            "session_breakout",
            "compression_expansion",
            "opening_range_outcome",
        ),
        ("EXPANSION", "*"): (
            "session_breakout",
            "compression_expansion",
            "impulse_continuation",
        ),
        ("VOLATILE", "*"): (
            "price_action_rejection",
            "session_breakout",
            "smc_sweep_reclaim",
        ),
        ("COMPRESSION", "*"): (
            "compression_expansion",
            "session_breakout",
        ),
        ("NEWS", "*"): (
            "price_action_rejection",
            "session_breakout",
            "smc_sweep_reclaim",
        ),
        ("UNKNOWN", "*"): ("chart_geometry_outcome",),
        ("*", "*"): ("chart_geometry_outcome",),
    }


def build_strategy_router() -> StrategyRouter:
    return StrategyRouter(default_strategy_mapping())
