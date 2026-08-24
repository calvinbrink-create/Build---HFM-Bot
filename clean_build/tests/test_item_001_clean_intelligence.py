from datetime import datetime, timedelta, timezone

from cipherfx_clean.contracts import Candle, RawTick
from cipherfx_clean.intelligence.execution_model import Quote, executable_entry, replay_executable_prices
from cipherfx_clean.intelligence.advanced import relative_activity, regime_probability, spread_context, zone_quality
from cipherfx_clean.intelligence.edge_miner import EdgeMiner, HypothesisProposal, run_hypothesis
from cipherfx_clean.intelligence.feature_store import FeatureRecord, FeatureStore
from cipherfx_clean.intelligence.governance import EdgeScorecard, GovernancePolicy, eligible_for_promotion
from cipherfx_clean.intelligence.market_features import (
    build_session_range,
    detect_fair_value_gaps,
    detect_liquidity_sweeps,
    session_vwap,
    tick_observation,
    volatility_observation,
)
from cipherfx_clean.intelligence.outcomes import future_path_outcome
from cipherfx_clean.intelligence.patterns import detect_geometric_patterns
from cipherfx_clean.intelligence.router import StrategyRouter
from cipherfx_clean.intelligence.planning import ConditionalPlan, PlanReality, PlanningLedger
from cipherfx_clean.intelligence.runtime_contracts import InstructionGateway, StructuredOutput
from cipherfx_clean.intelligence.strategies import chart_patterns, default_knowledge, knowledge_by_family
from cipherfx_clean.intelligence.tournament import ShadowTournament
from cipherfx_clean.intelligence.validation_full import TimedObservation, feature_ablation, validate_candidate
from cipherfx_clean.operations import BrokerContract, OperationalRequest, validate_operational


def candles(values):
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return tuple(Candle("XAUUSD", "M5", base + timedelta(minutes=i * 5), base + timedelta(minutes=(i + 1) * 5), *row) for i, row in enumerate(values))


def test_market_observations_cover_tick_gap_session_vwap_and_sweep():
    rows = candles(((10, 11, 9, 10.5), (10.5, 12, 10.5, 11.5), (11.5, 13, 11.5, 12.5), (12.5, 12.8, 8.5, 10.6)))
    ticks = tuple(RawTick("XAUUSD", rows[0].start + timedelta(seconds=i), 10 + i * .1, 10.05 + i * .1) for i in range(3))
    assert tick_observation(ticks).tick_rate > 0
    assert session_vwap(ticks).sample_size == 3
    assert build_session_range(rows, "ASIA", 0, 7).high == 13
    assert detect_fair_value_gaps(rows)
    assert detect_liquidity_sweeps(rows, lookback=2)
    assert volatility_observation(rows).atr > 0


def test_patterns_knowledge_and_outcomes_are_explicit():
    rows = candles(((10, 12, 9, 11), (11, 13, 10, 12), (12, 13, 11, 11.5), (11.5, 12, 10, 10.5), (10.5, 13, 10, 12.5)))
    assert chart_patterns()
    assert default_knowledge()
    assert set(knowledge_by_family()) >= {"PRICE_ACTION", "SMC", "WYCKOFF", "PATTERN"}
    assert detect_geometric_patterns(rows) is not None
    outcome = future_path_outcome("s1", "BUY", 10, ((5, 10.5, 10.6), (10, 11.5, 11.6)), 1, 1)
    assert outcome.tp_before_sl is True


def test_validation_is_oos_cost_aware_and_tracks_ablation():
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = tuple(TimedObservation(base + timedelta(minutes=i), 1 if i % 2 == 0 else -0.2, "XAUUSD", "LONDON", "TREND_UP") for i in range(12))
    report = validate_candidate("edge", rows, train_size=6, test_size=3, embargo=timedelta(minutes=1), multiple_test_count=12)
    assert report.folds and report.out_of_sample.sample_size > 0
    assert feature_ablation((1.0, 1.0), {"feature": (1.2, 1.2)})["feature"] > 0


def test_gateway_feature_store_planning_and_shadow_tournament():
    sent = []
    output = StructuredOutput("d1", "BUY", .6, "RETEST", (10, 10.1), "below_zone", "prior_high", ("chart-1",), "edge-1", ("ZONE",))
    assert InstructionGateway(sent.append).send(output) == output
    store = FeatureStore()
    record = FeatureRecord("s1", "XAUUSD", datetime.now(timezone.utc), "M5", {"spread": 0.1}, "hash", 1)
    store.put(record)
    assert store.get("s1", "M5", 1) == record
    ledger = PlanningLedger()
    ledger.add(ConditionalPlan("p1", "XAUUSD", "LONDON", record.observed_at, ("break",), "range_reclaim", ("chart-1",)))
    ledger.reconcile(PlanReality("p1", "MATCHED", "s1", "observed"))
    assert ledger.plan("p1").symbol == "XAUUSD"
    tournament = ShadowTournament({"a": lambda _: "BUY", "b": lambda _: "NO_TRADE"})
    assert len(tournament.run((("s1", object(), 1.0),))) == 2


def test_executable_prices_and_governance_are_deterministic():
    quote = Quote(0, 10, 10.2)
    assert executable_entry("BUY", quote) == 10.2
    assert replay_executable_prices("SELL", (quote,), 10)[0].cost.total > 0
    score = EdgeScorecard("edge", 1, 100, .6, .1, 1.5, .4, .5, 2.0, (.02, .8), 30)
    assert eligible_for_promotion(score, GovernancePolicy(50, 0.0, 20, 1.0))


def test_edge_discovery_is_injected_and_routes_are_explicit():
    proposal = HypothesisProposal("h1", "impulse state", ("velocity",), "research", 1)
    miner = EdgeMiner(lambda _: (proposal,))
    assert miner.propose({}) == (proposal,)
    result = run_hypothesis(proposal, (("s1", {"velocity": 2}, 1.0), ("s2", {"velocity": 0}, -1.0)), lambda row: row["velocity"] > 1)
    assert result.values == (1.0,)
    route = StrategyRouter({("TREND_UP", "LONDON"): ("h1",)}).candidates("TREND_UP", "LONDON")
    assert route.candidate_ids == ("h1",)


def test_empirical_profiles_are_observations_not_hidden_filters():
    rows = candles(((10, 11, 9, 10.5), (10.5, 12, 10, 11.5), (11.5, 13, 11, 12.5), (12.5, 14, 12, 13.5)))
    quality = zone_quality(rows, 10, 11, 0)
    assert quality.observation_count == 3
    assert spread_context(.2, (.1, .15, .3)).percentile > 0
    assert relative_activity(2, (1, 1)).ratio == 2
    assert regime_probability(("TREND_UP", "RANGE", "TREND_UP")).probabilities["TREND_UP"] > .5


def test_operational_validation_reports_broker_reasons_only():
    contract = BrokerContract("XAUUSD", .01, 1, 100, .1, 10, .1, 50)
    request = OperationalRequest("XAUUSD", .2, .1, .2, 40, 100, False, True, True)
    assert validate_operational(request, contract).status == "PASS"
    blocked = validate_operational(OperationalRequest("XAUUSD", .2, .3, .2, 40, 100, False, True, True), contract)
    assert blocked.status == "EXECUTION_BLOCKED"
    assert blocked.reasons == ("SPREAD_OUT_OF_RANGE",)
