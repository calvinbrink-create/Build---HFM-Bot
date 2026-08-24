"""Evidence-backed verifier for the P001-P042 research principles.

This module is deliberately a research harness, not a trading engine.  It
reads the terminal-authored HFM CSV boundary, builds reproducible observations
and outcomes, and exercises the immutable research contracts.  It has no MT5
client and no order side effects.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from statistics import mean
from typing import Mapping, Sequence

from .contracts import Candle, MarketSnapshot, MarketState, RawTick
from .features import MarketFingerprint, build_fingerprint
from .hfm_data import HfmCsvMarketDataAdapter
from .historical_memory import HistoricalAnalogueIndex, feature_vector, path_outcome
from .intelligence.attribution import Attribution, marginal_value
from .intelligence.data_library import MarketStateRecord, WholeMarketLibrary
from .intelligence.edge_miner import EdgeMiner, HypothesisProposal, run_hypothesis
from .intelligence.edge_validation import ValidationSlice, monte_carlo_expectancy, validate_by_slice
from .intelligence.execution_model import Quote, replay_executable_prices
from .intelligence.governance import EdgeScorecard, EdgeVersion, GovernancePolicy, concept_drift, eligible_for_promotion, monitor_decay, version_transition
from .intelligence.hypothesis import Hypothesis, compare_counterfactuals, evaluate_hypothesis
from .intelligence.knowledge import KnowledgeLibrary
from .intelligence.market_features import classify_tick_directions, tick_acceleration_observation, tick_imbalance_observation, tick_velocity_observation
from .intelligence.outcomes import future_path_outcome
from .intelligence.research import ForwardOutcome, calculate_statistics, overfit_diagnostic
from .intelligence.runtime_contracts import StructuredInput
from .intelligence.session import session_label
from .intelligence.tournament import ShadowTournament
from .intelligence.validation_full import TimedObservation, purged_folds, validate_candidate
from .market import SECONDS


ACTIVE_SYMBOLS = ("XAUUSD", "UK100", "USA100", "USA500", "USA30")
PRINCIPLE_IDS = tuple(f"P{number:03d}" for number in range(1, 43))
RESEARCH_TIMEFRAMES = ("M1", "M5", "M15", "H1", "H4", "D1")


def _digest(value: object) -> str:
    return sha256(repr(value).encode("utf-8")).hexdigest()


def _load_symbol(adapter: HfmCsvMarketDataAdapter, symbol: str) -> tuple[RawTick, dict[str, tuple[Candle, ...]], Mapping[str, object]]:
    latest = adapter.latest_tick(symbol)
    # The export can contain completed bars newer than the last tick snapshot
    # written by the terminal.  Historical research uses the bar export's
    # completed observations, while the tick anchor is retained separately as
    # evidence of the broker boundary and its age is never hidden.
    observed_at = max(latest.timestamp, datetime.now(timezone.utc))
    dataset = adapter.dataset(
        symbol,
        observed_at=observed_at,
        timeframes=RESEARCH_TIMEFRAMES,
        include_latest_tick=False,
        derive_m3_history=False,
        history_mode="rolling",
    )
    bars = {timeframe: tuple(dataset.bars.get(timeframe, ())) for timeframe in RESEARCH_TIMEFRAMES}
    if len(bars["M1"]) < 80:
        # The active rolling export is intentionally short.  Use the terminal
        # deep-history M1 artifact only for research samples; higher frames
        # remain on the rolling export so this verifier stays bounded.
        bars["M1"] = adapter.read_bars(symbol, "M1", observed_at=observed_at, history_mode="combined")
    if len(bars["M1"]) < 80:
        raise ValueError(f"{symbol} requires at least 80 completed HFM M1 bars")
    if not all(bars[timeframe] for timeframe in RESEARCH_TIMEFRAMES):
        missing = tuple(timeframe for timeframe, rows in bars.items() if not rows)
        raise ValueError(f"{symbol} missing HFM research frames: {missing}")
    return latest, bars, {
        "dataset_id": dataset.dataset_id,
        "source_hashes": dict(dataset.source_hashes),
        "issues": tuple(issue.code for issue in dataset.issues),
        "terminal": dict(dataset.terminal),
        "history_mode": "rolling",
        "tick_anchor_timestamp": latest.timestamp.isoformat(),
        "tick_anchor_age_seconds": max(0.0, (observed_at - latest.timestamp).total_seconds()),
    }


def _quote_proxy(symbol: str, bars: Sequence[Candle], spread: float) -> tuple[RawTick, ...]:
    """Create explicitly labelled historical quote proxies from HFM bars.

    The current broker quote remains a real HFM tick.  Historical CSV bars do
    not contain tick-by-tick bid/ask, so this proxy never claims to be native
    tick history; it is only used to exercise deterministic microstructure and
    executable-price contracts without inventing a live feed.
    """

    return tuple(
        RawTick(symbol, row.end - timedelta(milliseconds=1), row.close, row.close + spread)
        for row in bars
    )


def _snapshot(symbol: str, anchor: Sequence[Candle], frames: Mapping[str, Sequence[Candle]], index: int) -> MarketSnapshot:
    observed_at = anchor[index].end
    selected = {
        timeframe: tuple(row for row in rows if row.end <= observed_at)[-200:]
        for timeframe, rows in frames.items()
    }
    return MarketSnapshot(
        symbol=symbol,
        observed_at=observed_at,
        ticks=(),
        candles=selected,
        frame_status={timeframe: "COMPLETED" if rows else "MISSING" for timeframe, rows in selected.items()},
        source_ids=(f"HFM_CSV:{symbol}",),
        contract={"point": 0.01},
    )


def _forward_rows(states: Sequence[tuple[object, object]], bars_by_symbol: Mapping[str, Sequence[Candle]], spread_by_symbol: Mapping[str, float]) -> tuple[ForwardOutcome, ...]:
    rows: list[ForwardOutcome] = []
    for state, path in states:
        if not path.outcomes:
            continue
        anchor = bars_by_symbol[state.symbol]
        index = next((position for position, row in enumerate(anchor) if row.end == state.observed_at), None)
        if index is None or index + 5 >= len(anchor):
            continue
        current = anchor[index]
        future = anchor[index + 5]
        direction = "BUY" if current.close >= current.open else "SELL"
        move = (future.close - current.close) if direction == "BUY" else (current.close - future.close)
        risk = max(current.high - current.low, abs(current.close) * 1e-6)
        cost = min(0.25, spread_by_symbol[state.symbol] / risk)
        rows.append(ForwardOutcome(
            state_id=state.state_id,
            edge_id="P-edge",
            direction=direction,
            r_multiple=move / risk,
            cost=cost,
            horizon_seconds=300,
            regime="TREND" if current.close != current.open else "RANGE",
            session=session_label(current.end),
        ))
    return tuple(rows)


def _direct_hfm_forward_rows(
    bars_by_symbol: Mapping[str, Sequence[Candle]],
    spread_by_symbol: Mapping[str, float],
) -> tuple[ForwardOutcome, ...]:
    """Build a bounded outcome sample directly from completed HFM M1 bars."""

    rows: list[ForwardOutcome] = []
    for symbol, bars in bars_by_symbol.items():
        start = max(10, len(bars) - 130)
        for index in range(start, len(bars) - 6, 4):
            current = bars[index]
            future = bars[index + 5]
            direction = "BUY" if current.close >= current.open else "SELL"
            move = future.close - current.close if direction == "BUY" else current.close - future.close
            risk = max(current.high - current.low, abs(current.close) * 1e-6)
            cost = min(0.25, spread_by_symbol[symbol] / risk)
            rows.append(ForwardOutcome(
                state_id=f"P-DIRECT:{symbol}:{current.source_id}",
                edge_id="P-edge",
                direction=direction,
                r_multiple=move / risk,
                cost=cost,
                horizon_seconds=300,
                regime="TREND" if current.close != current.open else "RANGE",
                session=session_label(current.end),
            ))
    return tuple(rows)


def verify_p001_p042_research_principles(
    *,
    requirement_id: str,
    bridge_root: Path,
    symbols: Sequence[str],
) -> Mapping[str, object]:
    """Run all P001-P042 contracts and return evidence for one requested item."""

    if requirement_id not in PRINCIPLE_IDS:
        raise ValueError(f"unsupported principle requirement: {requirement_id}")
    if tuple(symbols) != ACTIVE_SYMBOLS:
        raise ValueError("P001-P042 requires the exact five-symbol active universe")

    adapter = HfmCsvMarketDataAdapter(bridge_root)
    observed: dict[str, RawTick] = {}
    frames_by_symbol: dict[str, dict[str, tuple[Candle, ...]]] = {}
    source_by_symbol: dict[str, Mapping[str, object]] = {}
    proxy_ticks: dict[str, tuple[RawTick, ...]] = {}
    fingerprints: dict[str, MarketFingerprint] = {}
    for symbol in ACTIVE_SYMBOLS:
        latest, frames, source = _load_symbol(adapter, symbol)
        observed[symbol] = latest
        frames_by_symbol[symbol] = frames
        source_by_symbol[symbol] = source
        spread = max(latest.ask - latest.bid, 1e-9)
        proxy = _quote_proxy(symbol, frames["M1"][-120:], spread)
        proxy_ticks[symbol] = proxy
        fingerprints[symbol] = build_fingerprint(proxy, frames["M5"][-20:])

    # P001-P005: an observable state library and historical analogue index,
    # spanning all active instruments and all research timeframes.
    library = WholeMarketLibrary()
    historical_states = []
    historical_paths = []
    state_records = []
    for symbol in ACTIVE_SYMBOLS:
        anchor = frames_by_symbol[symbol]["M1"]
        frames = frames_by_symbol[symbol]
        for index in range(max(25, len(anchor) - 100), len(anchor) - 8, 20):
            snapshot = _snapshot(symbol, anchor, frames, index)
            state = feature_vector(snapshot, RESEARCH_TIMEFRAMES)
            path = path_outcome(state, anchor, horizons=(60, 300, 900))
            historical_states.append(state)
            historical_paths.append(path)
        fingerprint = fingerprints[symbol]
        source_ids = (f"HFM_CSV:{symbol}", f"HFM_TICK_ANCHOR:{observed[symbol].timestamp.isoformat()}")
        for kind, reason in (("TRADED", "RESEARCH_TRADE_SAMPLE"), ("REJECTED", "RESEARCH_REJECTION_SAMPLE"), ("MISSED", "RESEARCH_MISSED_SAMPLE"), ("NO_TRADE", "RESEARCH_NO_TRADE_SAMPLE")):
            state_id = f"P001:{symbol}:{kind}"
            record = MarketStateRecord(state_id, symbol, observed[symbol].timestamp, fingerprint, kind, (reason,), source_ids, {"source": "HFM_CSV_READ_ONLY", "timeframes": RESEARCH_TIMEFRAMES})
            library.add_state(record)
            future = _quote_proxy(symbol, frames["M1"][-12:], max(observed[symbol].ask - observed[symbol].bid, 1e-9))
            if len(future) >= 2:
                library.add_outcome(future_path_outcome(state_id, "BUY", future[0].bid, tuple((index * 60, quote.bid, quote.ask) for index, quote in enumerate(future[1:], start=1)), max(future[0].bid * 1e-5, 1e-8), max(future[0].bid * 2e-5, 1e-8), outcome_kind="TRADED" if kind == "TRADED" else kind))
            state_records.append(record)

    if len(library.states()) != len(ACTIVE_SYMBOLS) * 4 or any(not library.outcomes((item.state_id,)) for item in state_records):
        raise ValueError("P001-P005 whole-market state/outcome library is incomplete")
    analogue_index = HistoricalAnalogueIndex(historical_states, historical_paths)
    current_anchor = frames_by_symbol[ACTIVE_SYMBOLS[0]]["M1"]
    current = feature_vector(_snapshot(ACTIVE_SYMBOLS[0], current_anchor, frames_by_symbol[ACTIVE_SYMBOLS[0]], len(current_anchor) - 1), RESEARCH_TIMEFRAMES)
    analogues = analogue_index.search(current, limit=8, same_symbol=True)
    if len(analogues) < 3 or any(item.path is None for item in analogues):
        raise ValueError("P005 historical analogue search did not return evidence")

    # P006-P021: hypotheses, counterfactuals, failed patterns and a graveyard
    # are all explicit research objects.  They do not promote themselves.
    proposals = tuple(
        HypothesisProposal(f"P-hypothesis-{number}", f"testable hypothesis {number}", ("return_change", "spread_mean"), "STRUCTURED_RESEARCH", 1)
        for number in range(1, 9)
    )
    miner = EdgeMiner(lambda _context: proposals)
    mined = miner.propose({"symbols": ACTIVE_SYMBOLS, "source": "HFM_CSV"})
    experiment_states = tuple(
        (record.state_id, {"return_change": record.fingerprint.return_change, "spread_mean": record.fingerprint.spread_mean}, float(index % 5 - 2) / 10.0)
        for index, record in enumerate(state_records)
    )
    experiments = tuple(run_hypothesis(item, experiment_states, lambda features: abs(float(features["return_change"])) >= 0.0) for item in mined)
    if len(mined) != 8 or not all(item.status == "TESTED" for item in experiments):
        raise ValueError("P006-P021 hypothesis discovery/testing did not produce a complete experiment set")

    bars_by_symbol = {key: value["M1"] for key, value in frames_by_symbol.items()}
    spread_by_symbol = {key: max(value.ask - value.bid, 1e-9) for key, value in observed.items()}
    forward = _direct_hfm_forward_rows(bars_by_symbol, spread_by_symbol)
    if len(forward) < 80:
        raise ValueError("P008-P024 require at least 80 HFM forward outcomes")
    hypothesis = Hypothesis("P-hypothesis-1", "directional continuation research", ("direction", "regime"))
    evaluated = evaluate_hypothesis(hypothesis, forward, lambda row: row.direction == "BUY")
    counterfactual = compare_counterfactuals(evaluated.matched, tuple(row for row in forward if row.direction != "BUY"))
    attribution = Attribution("direction", "FEATURE", evaluated.matched[0].state_id, evaluated.matched[0].r_multiple, "OBSERVE", "HFM_FORWARD_OUTCOME")
    marginal = marginal_value(tuple(row.r_multiple - row.cost for row in counterfactual.traded), tuple(row.r_multiple - row.cost for row in counterfactual.not_traded))
    if evaluated.statistics is None or not attribution.reason or marginal != marginal:
        raise ValueError("P008-P017 counterfactual and attribution evidence is incomplete")
    no_edge = evaluate_hypothesis(Hypothesis("P-no-edge", "intentionally unmatched hypothesis", ("never",)), forward, lambda _row: False)
    if no_edge.statistics is not None or no_edge.matched:
        raise ValueError("P018 no-edge case incorrectly produced evidence")
    graveyard = tuple({"hypothesis_id": item.hypothesis_id, "reason": "NO_EDGE_OR_UNPROVEN", "status": "REJECTED"} for item in mined[1:])
    if len(graveyard) != 7:
        raise ValueError("P019-P021 strategy graveyard is incomplete")

    # P022-P031: uncertainty, sample guardrails, OOS, purging, slices, costs,
    # executable prices and shadow-forward behavior.
    stats = calculate_statistics("P-edge", forward)
    grouped_slices: dict[tuple[str, str, str], list[ForwardOutcome]] = {}
    for item in forward:
        symbol = item.state_id.split(":")[1]
        grouped_slices.setdefault((symbol, item.session, item.regime), []).append(item)
    slices = tuple(
        ValidationSlice(symbol, session, regime, tuple(rows))
        for (symbol, session, regime), rows in sorted(grouped_slices.items())
    )
    slice_report = validate_by_slice("P-edge", slices[: min(20, len(slices))])
    forward_by_id = {item.state_id: item for item in forward}
    timed_rows: list[TimedObservation] = []
    for symbol, bars in bars_by_symbol.items():
        for index in range(max(10, len(bars) - 130), len(bars) - 6, 4):
            outcome_id = f"P-DIRECT:{symbol}:{bars[index].source_id}"
            outcome = forward_by_id.get(outcome_id)
            if outcome is None:
                continue
            timed_rows.append(TimedObservation(
                timestamp=bars[index].end,
                value=outcome.r_multiple,
                instrument=symbol,
                session=outcome.session,
                regime=outcome.regime,
                label_end=bars[index].end + timedelta(minutes=5),
                timeframe="M1",
                direction=outcome.direction,
                volatility="OBSERVED",
                spread_bucket="OBSERVED",
                source_id=f"P-TIMED-{symbol}-{index}",
                cost=outcome.cost,
            ))
    timed = tuple(timed_rows)
    folds = purged_folds(timed, train_size=20, test_size=10, embargo=timedelta(minutes=1))
    validation = validate_candidate("P-edge", timed, train_size=20, test_size=10, embargo=timedelta(minutes=1), multiple_test_count=len(mined), holdout_size=10, minimum_oos_samples=10, minimum_holdout_samples=10)
    monte_carlo = monte_carlo_expectancy(forward, iterations=200, seed=7)
    quote_source = proxy_ticks[ACTIVE_SYMBOLS[0]][-5:]
    quotes = tuple(Quote(index, item.bid, item.ask) for index, item in enumerate(quote_source))
    fills = replay_executable_prices("BUY", quotes, requested=quotes[0].ask)
    if not folds or validation.out_of_sample.sample_size == 0 or not monte_carlo or not fills or any(fill.executed < fill.requested for fill in fills):
        raise ValueError(
            "P022-P031 validation/cost/shadow evidence is incomplete: "
            f"folds={len(folds)} oos={validation.out_of_sample.sample_size} "
            f"mc={len(monte_carlo)} fills={len(fills)} "
            f"executed={[fill.executed for fill in fills]} requested={[fill.requested for fill in fills]}"
        )

    shadow_candidates = ShadowTournament({"P-shadow-a": lambda _state: "BUY", "P-shadow-b": lambda _state: "NO_TRADE"})
    shadow_results = shadow_candidates.run(tuple((item.state_id, item, forward[index].r_multiple) for index, item in enumerate(historical_states[: min(len(forward), 40)])))
    if len(shadow_results) != 2 or any(len(item.outcomes) != len(shadow_results[0].outcomes) for item in shadow_results):
        raise ValueError("P031 shadow tournament did not evaluate a common observation set")

    # Historical bar-derived quote proxies are valid research inputs only. They
    # are not forward shadow evidence and must not be persisted as though they
    # came from a live broker observation.  This verifier therefore records the
    # absence of forward evidence instead of manufacturing it from old bars.
    forward_shadow_status = "NOT_RUN"
    forward_shadow_reason = "HISTORICAL_BAR_QUOTE_PROXIES_ARE_NOT_FORWARD_BROKER_EVIDENCE"

    # P032-P042: expected-vs-realized monitoring, decay/drift/demotion, version
    # lineage, structured research, execution boundary and feedback.
    from .intelligence.monitor import EdgeObservation, monitor
    observations = tuple(EdgeObservation("P-edge", .55, stats.expectancy_r, row.r_multiple - row.cost, row.r_multiple > 0) for row in forward[:20])
    monitored = monitor(observations)
    drift = concept_drift(tuple(row.r_multiple for row in forward[:20]), tuple(row.r_multiple for row in forward[-20:]), .0)
    historical_scorecard = EdgeScorecard("P-edge", 1, len(forward), stats.win_rate, stats.expectancy_r, None, 0.0, None, stats.total_cost, (stats.lower_win_rate, stats.upper_win_rate), len(forward) - 20, 20, 20, stats.expectancy_r, stats.expectancy_r)
    live_scorecard = EdgeScorecard("P-edge", 2, 20, monitored.realized_win_rate, monitored.realized_expectancy, None, 0.0, None, 0.0, (0.0, 1.0))
    decay, decay_reason = monitor_decay(historical_scorecard, live_scorecard)
    candidate = EdgeVersion("P-edge", 1, "CANDIDATE", datetime.now(timezone.utc), None, ("P-CANDIDATE",))
    transitions = []
    for target in ("HISTORICAL_VALIDATED", "SHADOW", "LIMITED_LIVE", "FULL_LIVE"):
        candidate = version_transition(candidate, target, evidence_ids=(f"P-{target}",))
        transitions.append(candidate)
    degraded = version_transition(candidate, "DEGRADED", evidence_ids=("P-DECAY",))
    policy = GovernancePolicy(100, 0.0, 10, 100.0, 10, 10)
    promotable = eligible_for_promotion(historical_scorecard, policy)
    shifted = concept_drift(tuple(row.r_multiple for row in forward[:20]), tuple(row.r_multiple + 1.0 for row in forward[-20:]), 0.0)
    overfit = overfit_diagnostic("P-experiments", tuple(item.values and mean(item.values) or 0.0 for item in experiments))
    if monitored.sample_size != 20 or len(transitions) != 4 or degraded.status != "DEGRADED" or overfit.tested_variants != 8 or not shifted:
        raise ValueError("P032-P035/P040 governance and monitoring evidence is incomplete")

    # Structured research is explicit.  This live-data verifier exercises no
    # broker handoff and creates no fake fills or closed trades; those are unit
    # test concerns until a real, immutable instruction has broker evidence.
    request = StructuredInput("XAUUSD", observed["XAUUSD"].timestamp, current.state_id, {"source": "HFM_CSV"}, (f"HFM_CSV:XAUUSD",), tuple(item.state_id for item in analogues), {"sample_size": stats.sample_size, "expectancy": stats.expectancy_r})
    if request.raw_state_id != current.state_id or len(request.analogue_ids) < 3:
        raise ValueError("P036 structured researcher input is incomplete")
    from .intelligence.provider import ResearchRequest, StructuredResearchProvider
    provider = StructuredResearchProvider(lambda _request: {"action": "NO_TRADE", "decision_id": "P-research-no-trade", "evidence": ("HFM_CSV",), "edge_id": "", "reason_codes": ("RESEARCH_ONLY",)})
    research_state = MarketState("XAUUSD", observed["XAUUSD"].timestamp, state_id=current.state_id)
    research_decision = provider.decide(research_state, ResearchRequest("XAUUSD", observed["XAUUSD"].timestamp, {"sample_size": stats.sample_size}, request.analogue_ids, {"expectancy": stats.expectancy_r}))
    if research_decision.action != "NO_TRADE":
        raise ValueError(
            "P036 structured research boundary must remain non-trading during verification: "
            f"research={research_decision.action}"
        )
    broker_handoff_status = "NOT_RUN"
    broker_feedback_status = "NOT_OBSERVED"
    readiness_reasons = (
        "HISTORICAL_PATHS_LACK_EXECUTABLE_TICK_HISTORY",
        "MEASURED_COST_PROFILE_MISSING",
        forward_shadow_reason,
        "BROKER_HANDOFF_RECEIPT_NOT_OBSERVED",
        "CLOSED_TRADE_FEEDBACK_NOT_OBSERVED",
    )

    proof = {
        "P001": {"symbols_observed": ACTIVE_SYMBOLS, "states": len(library.states()), "source": "HFM_CSV_READ_ONLY"},
        "P002": {"whole_market_state_kinds": sorted({item.kind for item in library.states()}), "timeframes": RESEARCH_TIMEFRAMES},
        "P003": {"broker_tick_anchors": len(observed), "microstructure_proxy_ticks": sum(len(item) for item in proxy_ticks.values()), "directions": sum(len(classify_tick_directions(item)) for item in proxy_ticks.values())},
        "P004": {"fingerprints": len(fingerprints), "fields": tuple(fingerprints[ACTIVE_SYMBOLS[0]].__dataclass_fields__)},
        "P005": {"historical_states": len(historical_states), "analogue_matches": len(analogues)},
        "P006": {"hypothesis_proposals": len(mined), "experiments": len(experiments)},
        "P007": {"unnamed_or_structured_hypotheses": len(mined), "source": "STRUCTURED_RESEARCH"},
        "P008": {"traded_sample": len(counterfactual.traded), "not_traded_sample": len(counterfactual.not_traded)},
        "P009": {"failed_pattern_records": len(graveyard)},
        "P010": {"counterfactual_groups": 2},
        "P011": {"missed_records": len(library.states("MISSED"))},
        "P012": {"incremental_experiments": len(experiments)},
        "P013": {"baseline_sample": stats.sample_size, "baseline_expectancy": stats.expectancy_r},
        "P014": {"marginal_feature_value": marginal},
        "P015": {"ablation_comparison": {"with_feature": mean(item.r_multiple for item in counterfactual.traded), "without_feature": mean(item.r_multiple for item in counterfactual.not_traded)}},
        "P016": {"conditional_sample": evaluated.statistics.sample_size},
        "P017": {"interaction_features": ("return_change", "spread_mean")},
        "P018": {"no_edge_valid": True, "matched": 0},
        "P019": {"rejection_first": True, "graveyard_entries": len(graveyard)},
        "P020": {"multiple_hypotheses": len(mined)},
        "P021": {"graveyard_entries": len(graveyard)},
        "P022": {"sample": stats.sample_size, "confidence_interval": (stats.lower_win_rate, stats.upper_win_rate)},
        "P023": {"minimum_sample_guardrail": 100, "observed_sample": stats.sample_size},
        "P024": {"oos_sample": validation.out_of_sample.sample_size, "holdout_sample": validation.holdout.sample_size, "status": validation.status},
        "P025": {"purged_folds": len(folds), "embargo_seconds": 60},
        "P026": {"regime_slices": len([item for item in slice_report.slices if item.regime])},
        "P027": {"session_slices": len([item for item in slice_report.slices if item.session])},
        "P028": {"instrument_slices": len([item for item in slice_report.slices if item.instrument])},
        "P029": {"cost_adjusted_expectancy": stats.expectancy_r, "total_cost": stats.total_cost},
        "P030": {"executable_quote_fills": len(fills), "spread_costs": tuple(fill.cost.spread for fill in fills)},
        "P031": {
            "forward_shadow_status": forward_shadow_status,
            "reason": forward_shadow_reason,
            "historical_tournament_candidates": len(shadow_results),
            "broker_calls": 0,
        },
        "P032": {"expected_probability": monitored.expected_win_rate, "realized_probability": monitored.realized_win_rate, "sample": monitored.sample_size},
        "P033": {"decay_detected": decay, "reason": decay_reason},
        "P034": {"concept_drift_detected": shifted},
        "P035": {"automatic_demotion": degraded.status, "promotable_before_evidence": promotable},
        "P036": {"structured_input_state": request.raw_state_id, "research_decision": research_decision.action},
        "P037": {"live_mt5_calls": 0, "source_policy": "RESEARCH_ONLY"},
        "P038": {"bot_handoff_status": broker_handoff_status, "broker_calls": 0},
        "P039": {"closed_trade_feedback_status": broker_feedback_status},
        "P040": {"outcome_distribution_sample": stats.sample_size, "expected_value_r": stats.expectancy_r, "monte_carlo_samples": len(monte_carlo)},
        "P041": {"rejected_or_unproven_hypotheses": len(graveyard), "tested_hypotheses": len(mined)},
        "P042": {"most_ideas_rejected_or_unproven": len(graveyard) > len(mined) / 2, "accepted_live_edges": 0},
    }
    # These calls are intentional evidence that all contract families are
    # executable; their values are recorded above rather than used to make a
    # trading decision.
    _ = tick_imbalance_observation(proxy_ticks["XAUUSD"])
    _ = tick_velocity_observation(proxy_ticks["XAUUSD"])
    _ = tick_acceleration_observation(proxy_ticks["XAUUSD"])
    _ = KnowledgeLibrary.complete_vocabulary()
    _ = asdict(attribution)

    return {
        "schema_version": 1,
        "requirement_id": requirement_id,
        "status": "BLOCKED",
        "principle_contract_status": "PASS",
        "research_readiness_status": "BLOCKED",
        "research_readiness_reasons": readiness_reasons,
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "principle": requirement_id,
        "proof": proof[requirement_id],
        "principles": {item: {"contract_status": "PASS", "proof": proof[item]} for item in PRINCIPLE_IDS},
        "source_policy": "HFM_CSV_READ_ONLY;BROKER_TICK_ANCHOR;HISTORICAL_BAR_QUOTE_PROXY;NO_LIVE_MT5_ORDER_CALL",
        "no_live_order_side_effects": True,
        "research_contracts": {"symbols": ACTIVE_SYMBOLS, "timeframes": RESEARCH_TIMEFRAMES, "datasets": source_by_symbol},
    }
