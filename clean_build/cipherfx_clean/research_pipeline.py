"""Deterministic candidate sweep, validation, registry, and graveyard path."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import timedelta
from hashlib import sha256
from itertools import product
import json
from typing import Iterable, Iterator, Literal, Mapping, Sequence

from .historical_memory import HistoricalPath, HistoricalState, feature_names
from .intelligence.validation_full import TimedObservation, ValidationEvidence, validate_candidate
from .snapshot import RESEARCH_TIMEFRAMES
from .store import EvidenceStore


Operation = Literal["GT", "GE", "LT", "LE", "EQ", "ABS_LE"]
_FEATURE_INDEX = {
    name: index for index, name in enumerate(feature_names(RESEARCH_TIMEFRAMES))
}
_SPREAD_INDEX = {
    timeframe: _FEATURE_INDEX[f"{timeframe}:spread_return"]
    for timeframe in RESEARCH_TIMEFRAMES
}


@dataclass(frozen=True)
class FeatureCondition:
    feature_index: int
    operation: Operation
    value: float

    def matches(self, vector: Sequence[float]) -> bool:
        if self.feature_index < 0 or self.feature_index >= len(vector):
            return False
        observed = vector[self.feature_index]
        return {
            "GT": observed > self.value,
            "GE": observed >= self.value,
            "LT": observed < self.value,
            "LE": observed <= self.value,
            "EQ": observed == self.value,
            "ABS_LE": abs(observed) <= self.value,
        }[self.operation]


@dataclass(frozen=True)
class ExperimentSpec:
    experiment_id: str
    version: int
    family: str
    symbol: str
    direction: Literal["BUY", "SELL"]
    timeframe: str
    session: str
    regime: str
    horizon_seconds: int
    conditions: tuple[FeatureCondition, ...]
    cost_return: float
    cost_evidence_status: str = "INCOMPLETE_UNMEASURED"
    cost_source_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResearchPolicy:
    train_size: int
    test_size: int
    holdout_size: int
    embargo_seconds: int
    minimum_oos_samples: int
    minimum_holdout_samples: int
    required_fidelity: str = "HFM_EXECUTABLE_TICKS"


@dataclass(frozen=True)
class ExperimentRecord:
    spec: ExperimentSpec
    status: str
    matched_states: int
    fidelities: tuple[str, ...]
    validation: ValidationEvidence | None
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class ResearchRunReport:
    run_id: str
    planned: int
    tested: int
    historical_pass: int
    no_edge: int
    insufficient: int
    blocked_fidelity: int
    blocked_cost_evidence: int
    no_observations: int
    graveyard_entries: int
    complete: bool
    experiment_ids: tuple[str, ...]


def sweep_specs(
    *,
    families: Sequence[str],
    symbols: Sequence[str],
    directions: Sequence[Literal["BUY", "SELL"]],
    timeframes: Sequence[str],
    sessions: Sequence[str],
    regimes: Sequence[str],
    horizons: Sequence[int],
    condition_sets: Sequence[tuple[FeatureCondition, ...]],
    cost_return: float,
    cost_evidence_status: str = "INCOMPLETE_UNMEASURED",
    cost_source_ids: Sequence[str] = (),
    version: int = 1,
) -> Iterator[ExperimentSpec]:
    if version <= 0 or cost_return < 0:
        raise ValueError("positive version and non-negative cost are required")
    dimensions = (families, symbols, directions, timeframes, sessions, regimes, horizons, condition_sets)
    if any(not values for values in dimensions):
        raise ValueError("every sweep dimension must contain at least one value")
    for family, symbol, direction, timeframe, session, regime, horizon, conditions in product(*dimensions):
        if horizon <= 0:
            raise ValueError("horizons must be positive")
        payload = (
            version, family, symbol, direction, timeframe, session, regime,
            horizon, tuple(conditions), cost_return, cost_evidence_status,
            tuple(cost_source_ids),
        )
        experiment_id = sha256(json.dumps(payload, default=str, separators=(",", ":")).encode()).hexdigest()
        yield ExperimentSpec(
            experiment_id, version, family, symbol, direction, timeframe,
            session, regime, horizon, tuple(conditions), cost_return,
            cost_evidence_status, tuple(cost_source_ids),
        )


def default_condition_sets(timeframe: str) -> tuple[tuple[FeatureCondition, ...], ...]:
    if timeframe not in RESEARCH_TIMEFRAMES:
        raise ValueError(f"unsupported research timeframe: {timeframe}")
    names = feature_names(RESEARCH_TIMEFRAMES)
    index = {name: names.index(f"{timeframe}:{name}") for name in (
        "body_return",
        "lower_wick_fraction",
        "upper_wick_fraction",
        "relative_tick_volume",
        "structure_direction",
        "regime_state",
        "candle_pattern_count_log",
    )}
    return (
        (),
        (FeatureCondition(index["body_return"], "GT", 0.0),),
        (FeatureCondition(index["body_return"], "LT", 0.0),),
        (FeatureCondition(index["lower_wick_fraction"], "GE", .6),),
        (FeatureCondition(index["upper_wick_fraction"], "GE", .6),),
        (FeatureCondition(index["relative_tick_volume"], "GE", 1.25),),
        (FeatureCondition(index["structure_direction"], "EQ", 1.0),),
        (FeatureCondition(index["structure_direction"], "EQ", -1.0),),
        (FeatureCondition(index["regime_state"], "GE", .5),),
        (FeatureCondition(index["regime_state"], "LE", -.5),),
        (FeatureCondition(index["candle_pattern_count_log"], "GT", 0.0),),
    )


def evaluate_experiment(
    spec: ExperimentSpec,
    cases: Iterable[tuple[HistoricalState, HistoricalPath | None]],
    *,
    policy: ResearchPolicy,
    multiple_test_count: int,
) -> ExperimentRecord:
    observations: list[TimedObservation] = []
    fidelities: set[str] = set()
    for state, path in cases:
        if path is None or not _matches(spec, state):
            continue
        outcome = next((item for item in path.outcomes if item.horizon_seconds == spec.horizon_seconds), None)
        if outcome is None:
            continue
        gross = outcome.close_return if spec.direction == "BUY" else -outcome.close_return
        observed_spread = _observed_spread_return(state, spec.timeframe)
        observations.append(
            TimedObservation(
                timestamp=state.observed_at,
                label_end=state.observed_at + timedelta(seconds=spec.horizon_seconds),
                value=gross,
                cost=spec.cost_return + observed_spread,
                instrument=state.symbol,
                session=spec.session if spec.session != "ANY" else _session(state),
                regime=spec.regime if spec.regime != "ANY" else _regime(state, spec.timeframe),
                timeframe=spec.timeframe,
                direction=spec.direction,
                source_id=state.state_id,
            )
        )
        fidelities.add(path.fidelity)
    return _record_from_observations(
        spec,
        observations,
        fidelities,
        policy=policy,
        multiple_test_count=multiple_test_count,
    )


def _record_from_observations(
    spec: ExperimentSpec,
    observations: Sequence[TimedObservation],
    fidelities: Iterable[str],
    *,
    policy: ResearchPolicy,
    multiple_test_count: int,
) -> ExperimentRecord:
    if not observations:
        return ExperimentRecord(spec, "NO_OBSERVATIONS", 0, (), None, ("NO_MATCHED_HISTORICAL_STATES",))
    observed_fidelities = frozenset(fidelities)
    validation = validate_candidate(
        spec.experiment_id,
        observations,
        train_size=policy.train_size,
        test_size=policy.test_size,
        embargo=timedelta(seconds=policy.embargo_seconds),
        multiple_test_count=multiple_test_count,
        holdout_size=policy.holdout_size,
        minimum_oos_samples=policy.minimum_oos_samples,
        minimum_holdout_samples=policy.minimum_holdout_samples,
    )
    reasons = list(validation.reasons)
    status = validation.status
    if status == "PASS" and observed_fidelities != {policy.required_fidelity}:
        status = "BLOCKED_NON_EXECUTABLE_EVIDENCE"
        reasons.append(
            f"REQUIRED_FIDELITY={policy.required_fidelity};OBSERVED={','.join(sorted(observed_fidelities))}"
        )
    elif status == "PASS" and (
        spec.cost_evidence_status != "COMPLETE" or not spec.cost_source_ids
    ):
        status = "BLOCKED_INCOMPLETE_COST_EVIDENCE"
        reasons.append(
            f"COST_EVIDENCE_STATUS={spec.cost_evidence_status};SOURCES={len(spec.cost_source_ids)}"
        )
    return ExperimentRecord(
        spec,
        status,
        len(observations),
        tuple(sorted(observed_fidelities)),
        validation,
        tuple(reasons),
    )


def run_research(
    *,
    store: EvidenceStore,
    specs: Sequence[ExperimentSpec],
    cases: Sequence[tuple[HistoricalState, HistoricalPath | None]],
    policy: ResearchPolicy,
    multiple_test_count: int | None = None,
    run_identity: str | None = None,
) -> ResearchRunReport:
    effective_test_count = multiple_test_count or len(specs)
    if effective_test_count < len(specs):
        raise ValueError("multiple-test count cannot be smaller than the evaluated shard")
    run_id = _digest((run_identity or tuple(item.experiment_id for item in specs), asdict(policy), effective_test_count))
    case_index = _ResearchCaseIndex(cases)
    counts: dict[str, int] = {}
    experiment_ids: list[str] = []
    graveyard_entries = 0
    batch: list[dict[str, object]] = []
    for spec in specs:
        record = case_index.evaluate(
            spec,
            policy=policy,
            multiple_test_count=effective_test_count,
        )
        validation_id = None
        validation_manifest = None
        validation_summary = None
        if record.validation is not None:
            validation_summary = _validation_summary(record.validation)
            if record.status != "INSUFFICIENT_EVIDENCE":
                validation_manifest = _validation_manifest(record.validation)
                validation_id = _digest((run_id, record.spec.experiment_id, validation_manifest))
        manifest = _experiment_manifest(record, validation_id, validation_summary)
        assessment_id = _digest((run_id, record.spec.experiment_id, record.status, validation_id, record.reasons))
        graveyard_id = None
        if record.status == "NO_EDGE":
            graveyard_id = _digest((assessment_id, "NO_EDGE"))
            graveyard_entries += 1
        batch.append({
            "experiment_id": record.spec.experiment_id,
            "spec": asdict(record.spec),
            "assessment_id": assessment_id,
            "run_id": run_id,
            "status": record.status,
            "validation_id": validation_id,
            "validation": validation_manifest,
            "assessment": manifest,
            "graveyard_id": graveyard_id,
            "graveyard_reason": "NO_EDGE" if graveyard_id else None,
            "graveyard": {
                "assessment_id": assessment_id,
                "status": record.status,
                "matched_states": record.matched_states,
                "reasons": record.reasons,
            } if graveyard_id else None,
        })
        if len(batch) >= 5_000:
            store.write_research_batch(batch)
            batch.clear()
        counts[record.status] = counts.get(record.status, 0) + 1
        experiment_ids.append(record.spec.experiment_id)
    if batch:
        store.write_research_batch(batch)
    return ResearchRunReport(
        run_id=run_id,
        planned=len(specs),
        tested=len(experiment_ids),
        historical_pass=counts.get("PASS", 0),
        no_edge=counts.get("NO_EDGE", 0),
        insufficient=counts.get("INSUFFICIENT_EVIDENCE", 0),
        blocked_fidelity=counts.get("BLOCKED_NON_EXECUTABLE_EVIDENCE", 0),
        blocked_cost_evidence=counts.get("BLOCKED_INCOMPLETE_COST_EVIDENCE", 0),
        no_observations=counts.get("NO_OBSERVATIONS", 0),
        graveyard_entries=graveyard_entries,
        complete=len(experiment_ids) == len(specs),
        experiment_ids=tuple(experiment_ids),
    )


@dataclass(frozen=True)
class _IndexedResearchCase:
    state: HistoricalState
    path: HistoricalPath | None
    session: str
    regimes: Mapping[str, str]
    spreads: Mapping[str, float]
    outcomes: Mapping[int, object]


class _ResearchCaseIndex:
    """Cache condition matches so large empirical sweeps remain tractable."""

    def __init__(self, cases: Sequence[tuple[HistoricalState, HistoricalPath | None]]):
        self._by_symbol: dict[str, tuple[_IndexedResearchCase, ...]] = {}
        for symbol in sorted({state.symbol for state, _ in cases}):
            self._by_symbol[symbol] = tuple(
                self._index_case(state, path)
                for state, path in cases
                if state.symbol == symbol
            )
        self._atom_cache: dict[tuple[str, FeatureCondition], frozenset[int]] = {}
        self._set_cache: dict[
            tuple[str, str, str, tuple[FeatureCondition, ...]], tuple[int, ...]
        ] = {}

    @staticmethod
    def _index_case(
        state: HistoricalState,
        path: HistoricalPath | None,
    ) -> _IndexedResearchCase:
        session = "UNKNOWN"
        regimes: dict[str, str] = {}
        for label in state.labels:
            if label.startswith("SESSION:"):
                session = label.removeprefix("SESSION:")
                continue
            if ":REGIME:" in label:
                timeframe, regime = label.split(":REGIME:", 1)
                regimes[timeframe] = regime
        spreads = {
            timeframe: max(0.0, state.vector[index])
            for timeframe, index in _SPREAD_INDEX.items()
            if index < len(state.vector)
        }
        outcomes = {
            item.horizon_seconds: item
            for item in path.outcomes
        } if path is not None else {}
        return _IndexedResearchCase(
            state, path, session, regimes, spreads, outcomes,
        )

    def select(self, spec: ExperimentSpec) -> tuple[_IndexedResearchCase, ...]:
        rows = self._by_symbol.get(spec.symbol, ())
        key = (spec.symbol, spec.session, spec.regime, spec.conditions)
        indices = self._set_cache.get(key)
        if indices is None:
            selected = set(range(len(rows)))
            if spec.session != "ANY":
                selected.intersection_update(
                    index for index, row in enumerate(rows)
                    if row.session == spec.session
                )
            if spec.regime != "ANY":
                selected.intersection_update(
                    index for index, row in enumerate(rows)
                    if row.regimes.get(spec.timeframe, "UNKNOWN") == spec.regime
                )
            for condition in spec.conditions:
                atom_key = (spec.symbol, condition)
                matched = self._atom_cache.get(atom_key)
                if matched is None:
                    matched = frozenset(
                        index for index, row in enumerate(rows)
                        if condition.matches(row.state.vector)
                    )
                    self._atom_cache[atom_key] = matched
                selected.intersection_update(matched)
                if not selected:
                    break
            indices = tuple(sorted(selected))
            self._set_cache[key] = indices
        return tuple(rows[index] for index in indices)

    def evaluate(
        self,
        spec: ExperimentSpec,
        *,
        policy: ResearchPolicy,
        multiple_test_count: int,
    ) -> ExperimentRecord:
        observations: list[TimedObservation] = []
        fidelities: set[str] = set()
        for row in self.select(spec):
            if row.path is None:
                continue
            outcome = row.outcomes.get(spec.horizon_seconds)
            if outcome is None:
                continue
            gross = outcome.close_return if spec.direction == "BUY" else -outcome.close_return
            observations.append(
                TimedObservation(
                    timestamp=row.state.observed_at,
                    label_end=row.state.observed_at + timedelta(seconds=spec.horizon_seconds),
                    value=gross,
                    cost=spec.cost_return + row.spreads.get(spec.timeframe, 0.0),
                    instrument=row.state.symbol,
                    session=spec.session if spec.session != "ANY" else row.session,
                    regime=(
                        spec.regime
                        if spec.regime != "ANY"
                        else row.regimes.get(spec.timeframe, "UNKNOWN")
                    ),
                    timeframe=spec.timeframe,
                    direction=spec.direction,
                    source_id=row.state.state_id,
                )
            )
            fidelities.add(row.path.fidelity)
        return _record_from_observations(
            spec,
            observations,
            fidelities,
            policy=policy,
            multiple_test_count=multiple_test_count,
        )


def _matches(spec: ExperimentSpec, state: HistoricalState) -> bool:
    labels = set(state.labels)
    if state.symbol != spec.symbol:
        return False
    if spec.session != "ANY" and f"SESSION:{spec.session}" not in labels:
        return False
    if spec.regime != "ANY" and f"{spec.timeframe}:REGIME:{spec.regime}" not in labels:
        return False
    return all(condition.matches(state.vector) for condition in spec.conditions)


def _session(state: HistoricalState) -> str:
    return next((item.removeprefix("SESSION:") for item in state.labels if item.startswith("SESSION:")), "UNKNOWN")


def _regime(state: HistoricalState, timeframe: str) -> str:
    prefix = f"{timeframe}:REGIME:"
    return next((item.removeprefix(prefix) for item in state.labels if item.startswith(prefix)), "UNKNOWN")


def _observed_spread_return(state: HistoricalState, timeframe: str) -> float:
    if timeframe not in RESEARCH_TIMEFRAMES:
        return 0.0
    index = _SPREAD_INDEX[timeframe]
    return max(0.0, state.vector[index]) if index < len(state.vector) else 0.0


def _experiment_manifest(
    record: ExperimentRecord,
    validation_id: str | None,
    validation_summary: Mapping[str, object] | None,
) -> dict[str, object]:
    return {
        "experiment_id": record.spec.experiment_id,
        "status": record.status,
        "matched_states": record.matched_states,
        "fidelities": record.fidelities,
        "reasons": record.reasons,
        "validation_id": validation_id,
        "validation_summary": validation_summary,
        "storage_contract": "COMPACT_REPRODUCIBLE_EXPERIMENT_V2",
    }


def _validation_summary(validation: ValidationEvidence) -> dict[str, object]:
    return {
        "status": validation.status,
        "reasons": validation.reasons,
        "fold_count": len(validation.folds),
        "in_sample_count": validation.in_sample.sample_size,
        "out_of_sample_count": validation.out_of_sample.sample_size,
        "holdout_count": validation.holdout.sample_size,
        "multiple_test_count": validation.multiple_test_count,
        "multiplicity_adjusted_lower": validation.multiplicity_adjusted_lower,
    }


def _validation_manifest(validation: ValidationEvidence) -> dict[str, object]:
    return {
        "edge_id": validation.edge_id,
        "folds": tuple(_fold_manifest(fold) for fold in validation.folds),
        "in_sample": asdict(validation.in_sample),
        "out_of_sample": asdict(validation.out_of_sample),
        "holdout": asdict(validation.holdout),
        "slices": {key: asdict(value) for key, value in validation.slices.items()},
        "monte_carlo_lower": validation.monte_carlo_lower,
        "multiple_test_count": validation.multiple_test_count,
        "multiplicity_adjusted_lower": validation.multiplicity_adjusted_lower,
        "status": validation.status,
        "reasons": validation.reasons,
        "storage_contract": "COMPACT_PURGED_WALK_FORWARD_V1",
    }


def _fold_manifest(fold: object) -> dict[str, object]:
    train = tuple(fold.train)
    test = tuple(fold.test)
    return {
        "number": fold.number,
        "train_count": len(train),
        "test_count": len(test),
        "train_start": train[0].timestamp.isoformat() if train else None,
        "train_end": train[-1].timestamp.isoformat() if train else None,
        "test_start": test[0].timestamp.isoformat() if test else None,
        "test_end": test[-1].timestamp.isoformat() if test else None,
        "embargo_start": fold.embargo_start.isoformat() if fold.embargo_start else None,
        "embargo_end": fold.embargo_end.isoformat() if fold.embargo_end else None,
        "train_source_digest": _source_digest(train),
        "test_source_digest": _source_digest(test),
    }


def _source_digest(rows: Sequence[object]) -> str:
    return _digest(tuple((row.source_id, row.timestamp, row.label_end) for row in rows))


def _digest(value: object) -> str:
    return sha256(json.dumps(value, default=str, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
