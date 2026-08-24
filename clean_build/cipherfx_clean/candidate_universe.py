"""Leakage-safe empirical hypothesis universe derived from discovery data.

Conditions are research candidates, never live gates. Thresholds are learned
only from the declared discovery period and all generated candidates are
returned; the function does not select winners or inspect future outcomes.

Every feature receives its full univariate quantile/category candidates. Pair
coverage is one balanced split per distinct raw-feature pair, selected without
looking at outcomes. This preserves broad interaction discovery without the
quadratic duplication of every threshold permutation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from itertools import combinations
import json
from typing import Iterable

from .historical_memory import (
    FEATURES_PER_TIMEFRAME,
    GLOBAL_FEATURE_COUNT,
    HistoricalState,
    feature_kinds,
    feature_names,
)
from .research_pipeline import FeatureCondition
from .snapshot import RESEARCH_TIMEFRAMES


@dataclass(frozen=True)
class CandidateUniverse:
    universe_id: str
    symbol: str
    timeframe: str
    discovery_start: datetime
    discovery_end: datetime
    discovery_samples: int
    feature_schema_version: int
    condition_sets: tuple[tuple[FeatureCondition, ...], ...]
    source_digest: str
    feature_names: tuple[str, ...]
    interaction_contract: str


def build_candidate_universe(
    states: Iterable[HistoricalState],
    *,
    symbol: str,
    timeframe: str,
    discovery_end: datetime,
    quantiles: tuple[float, ...] = (.25, .50, .75),
    interaction_order: int = 2,
    minimum_samples: int = 100,
) -> CandidateUniverse:
    if timeframe not in RESEARCH_TIMEFRAMES:
        raise ValueError(f"unsupported research timeframe: {timeframe}")
    if minimum_samples <= 0 or interaction_order not in (1, 2):
        raise ValueError("minimum samples must be positive and interaction order must be 1 or 2")
    if not quantiles or any(not 0 < value < 1 for value in quantiles):
        raise ValueError("quantiles must be inside (0, 1)")
    rows = tuple(sorted(
        (row for row in states if row.symbol == symbol and row.observed_at < discovery_end),
        key=lambda row: (row.observed_at, row.state_id),
    ))
    if len(rows) < minimum_samples:
        raise ValueError("insufficient discovery-period states")
    versions = {row.feature_schema_version for row in rows}
    widths = {len(row.vector) for row in rows}
    expected_width = len(RESEARCH_TIMEFRAMES) * FEATURES_PER_TIMEFRAME + GLOBAL_FEATURE_COUNT
    if len(versions) != 1 or widths != {expected_width}:
        raise ValueError("candidate discovery requires one current compatible feature schema")
    offset = RESEARCH_TIMEFRAMES.index(timeframe) * FEATURES_PER_TIMEFRAME
    global_offset = len(RESEARCH_TIMEFRAMES) * FEATURES_PER_TIMEFRAME
    names = feature_names(RESEARCH_TIMEFRAMES)
    kinds = feature_kinds(RESEARCH_TIMEFRAMES)
    selected_indices = tuple(range(offset, offset + FEATURES_PER_TIMEFRAME)) + tuple(
        range(global_offset, global_offset + GLOBAL_FEATURE_COUNT)
    )
    atoms: list[FeatureCondition] = []
    for index in selected_indices:
        values = sorted(row.vector[index] for row in rows)
        unique_values = sorted(set(values))
        if len(unique_values) <= 1:
            continue
        if kinds[index] == "CATEGORICAL":
            if len(unique_values) > 16:
                raise ValueError(f"categorical feature has unexpected cardinality: {index}")
            for value in unique_values:
                atoms.append(FeatureCondition(index, "EQ", value))
        else:
            for quantile in quantiles:
                threshold = _quantile(values, quantile)
                operation = "LE" if quantile <= .5 else "GE"
                atoms.append(FeatureCondition(index, operation, threshold))
    unique_atoms = tuple(sorted(
        set(atoms),
        key=lambda row: (row.feature_index, row.operation, row.value),
    ))
    sets: list[tuple[FeatureCondition, ...]] = [()]
    sets.extend((atom,) for atom in unique_atoms)
    if interaction_order == 2:
        canonical = _balanced_atoms(unique_atoms, rows)
        sets.extend(combinations(canonical, 2))
    condition_sets = tuple(sets)
    source_payload = tuple((row.state_id, row.observed_at.isoformat()) for row in rows)
    source_digest = _digest(source_payload)
    payload = (
        symbol, timeframe, rows[0].observed_at, discovery_end,
        tuple(quantiles), interaction_order, source_digest, condition_sets,
    )
    return CandidateUniverse(
        _digest(payload),
        symbol,
        timeframe,
        rows[0].observed_at,
        discovery_end,
        len(rows),
        next(iter(versions)),
        condition_sets,
        source_digest,
        tuple(names[index] for index in selected_indices),
        "ALL_FEATURE_SINGLES_PLUS_ONE_BALANCED_SPLIT_PER_DISTINCT_FEATURE_PAIR_V1",
    )


def _balanced_atoms(
    atoms: tuple[FeatureCondition, ...],
    rows: tuple[HistoricalState, ...],
) -> tuple[FeatureCondition, ...]:
    by_feature: dict[int, list[FeatureCondition]] = {}
    for atom in atoms:
        by_feature.setdefault(atom.feature_index, []).append(atom)
    selected = []
    for feature_index, candidates in sorted(by_feature.items()):
        selected.append(min(
            candidates,
            key=lambda atom: (
                abs(sum(atom.matches(row.vector) for row in rows) / len(rows) - 0.5),
                atom.operation,
                atom.value,
            ),
        ))
    return tuple(selected)


def _quantile(values: list[float], fraction: float) -> float:
    if len(values) == 1:
        return values[0]
    position = (len(values) - 1) * fraction
    lower = int(position)
    upper = min(len(values) - 1, lower + 1)
    weight = position - lower
    return values[lower] * (1 - weight) + values[upper] * weight


def _digest(value: object) -> str:
    return sha256(json.dumps(value, default=str, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
