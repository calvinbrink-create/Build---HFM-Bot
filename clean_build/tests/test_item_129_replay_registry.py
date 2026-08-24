from datetime import datetime, timedelta, timezone

from cipherfx_clean.contracts import MarketState
from cipherfx_clean.intelligence.governance import EdgeVersion, version_transition
from cipherfx_clean.intelligence.monitor import EdgeObservation, monitor
from cipherfx_clean.intelligence.tournament import ShadowTournament
from cipherfx_clean.replay import replay


def test_replay_tournament_monitor_and_immutable_versions():
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    states = tuple(
        MarketState("UK100", at + timedelta(minutes=index), {"value": index}, f"s{index}")
        for index in range(6)
    )
    decide = lambda state: state.state_id
    assert replay(tuple(reversed(states)), decide).decisions == replay(states, decide).decisions
    tournament = ShadowTournament({"a": lambda state: "BUY", "b": lambda state: "NO_TRADE"})
    results = tournament.run(tuple((state.state_id, state, 0.1) for state in states))
    assert len(results) == 2
    report = monitor(tuple(EdgeObservation("e", .5, 0.0, .1, True) for _ in range(6)))
    assert report.sample_size == 6
    v1 = EdgeVersion("e", 1, "CANDIDATE", at, None, ("a",))
    v2 = version_transition(v1, "HISTORICAL_VALIDATED", evidence_ids=("b",))
    assert v2.version == 2
    assert v2.parent_version == 1
