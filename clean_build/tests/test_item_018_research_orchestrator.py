from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from cipherfx_clean.historical_memory import HistoricalState, feature_kinds, feature_names
from cipherfx_clean.research_coverage import HistoricalCoveragePolicy, assess_history_coverage
from cipherfx_clean.research_orchestrator import (
    PipelineConfiguration,
    build_candidate_universes,
    code_manifest,
    run_pipeline,
)
from cipherfx_clean.research_pipeline import ResearchPolicy
from cipherfx_clean.snapshot import RESEARCH_TIMEFRAMES


def test_code_manifest_is_content_addressed_and_excludes_runtime_evidence(tmp_path):
    root = tmp_path / "clean_build"
    root.mkdir()
    (root / "module.py").write_text("value = 1\n")
    (root / "README.md").write_text("scope\n")
    evidence = root / "evidence"
    evidence.mkdir()
    (evidence / "mutable.json").write_text("{}\n")

    first = code_manifest(root)
    (evidence / "mutable.json").write_text('{"changed":true}\n')
    second = code_manifest(root)

    assert first == second
    assert {row["path"] for row in first["files"]} == {"README.md", "module.py"}
    (root / "module.py").write_text("value = 2\n")
    assert code_manifest(root)["manifest_digest"] != first["manifest_digest"]


def test_orchestrator_uses_full_empirical_universes_not_eleven_fixed_conditions():
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    names = feature_names(RESEARCH_TIMEFRAMES)
    kinds = feature_kinds(RESEARCH_TIMEFRAMES)
    states = tuple(
        HistoricalState(
            f"s-{index}", "dataset", "XAUUSD", start + timedelta(hours=index),
            tuple(
                float(index % 3 - 1) if kinds[feature] == "CATEGORICAL"
                else float(index % 7)
                for feature in range(len(names))
            ),
            ("SESSION:LONDON", "M5:REGIME:TREND_UP"),
            (f"source-{index}",),
        )
        for index in range(140)
    )
    universes, manifest = build_candidate_universes(
        states, ("XAUUSD",), ("M5",), interaction_order=2,
    )

    assert len(universes) == 1
    assert len(universes[0].condition_sets) > 11
    assert manifest["total_condition_sets"] == len(universes[0].condition_sets)
    assert any(len(item) == 2 for item in universes[0].condition_sets)


def _dataset(m1_bars: int, h1_bars: int):
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    def rows(count: int, minutes: int):
        return tuple(
            SimpleNamespace(start=start + timedelta(minutes=index * minutes),
                            end=start + timedelta(minutes=(index + 1) * minutes))
            for index in range(count)
        )
    return SimpleNamespace(bars={"M1": rows(m1_bars, 1), "H1": rows(h1_bars, 60)})


class _Adapter:
    def __init__(self, dataset):
        self.dataset_value = dataset

    def dataset(self, symbol, **_kwargs):
        return self.dataset_value


def _coverage_policy():
    return HistoricalCoveragePolicy(
        required_timeframes=("M1", "H1"),
        minimum_bars={"M1": 100, "H1": 100},
        required_state_windows=300,
        lookback_bars=100,
        stride_m1_bars=60,
        required_fidelity="BAR_OHLC_NO_HISTORICAL_BID_ASK_TICKS",
    )


def test_history_coverage_blocks_before_the_source_can_supply_validator_windows():
    report = assess_history_coverage(
        _Adapter(_dataset(10_080, 120)),
        ("XAUUSD",),
        observed_at=datetime(2024, 2, 1, tzinfo=timezone.utc),
        policy=_coverage_policy(),
    )

    assert report.status == "BLOCKED_INSUFFICIENT_HISTORICAL_COVERAGE"
    symbol = report.symbols[0]
    assert symbol.maximum_state_windows == 167
    assert symbol.required_state_windows == 300
    assert any(reason.endswith("STATE_WINDOWS:167<300") for reason in symbol.reasons)
    assert next(frame for frame in symbol.frames if frame.timeframe == "M1").required_bars == 18_041


def test_history_coverage_accepts_exact_capacity_for_chronological_windows():
    report = assess_history_coverage(
        _Adapter(_dataset(18_041, 120)),
        ("XAUUSD",),
        observed_at=datetime(2024, 2, 1, tzinfo=timezone.utc),
        policy=_coverage_policy(),
    )

    assert report.status == "READY"
    assert report.symbols[0].maximum_state_windows == 300


def test_history_coverage_blocks_tick_required_research_when_only_bar_ohlc_is_available():
    policy = HistoricalCoveragePolicy(
        required_timeframes=("M1", "H1"),
        minimum_bars={"M1": 100, "H1": 100},
        required_state_windows=300,
        lookback_bars=100,
        stride_m1_bars=60,
        required_fidelity="HFM_EXECUTABLE_TICKS",
    )
    report = assess_history_coverage(
        _Adapter(_dataset(18_041, 120)),
        ("XAUUSD",),
        observed_at=datetime(2024, 2, 1, tzinfo=timezone.utc),
        policy=policy,
    )

    assert report.status == "BLOCKED_INSUFFICIENT_HISTORICAL_FIDELITY"
    assert report.available_historical_fidelity == "BAR_OHLC_NO_HISTORICAL_BID_ASK_TICKS"
    assert report.reasons == (
        "HISTORICAL_FIDELITY:BAR_OHLC_NO_HISTORICAL_BID_ASK_TICKS!=HFM_EXECUTABLE_TICKS",
    )


def test_pipeline_returns_before_population_when_history_preflight_is_blocked(tmp_path, monkeypatch):
    import cipherfx_clean.research_orchestrator as orchestrator

    repository = tmp_path / "repository"
    source = tmp_path / "source"
    repository.mkdir()
    source.mkdir()
    blocked = assess_history_coverage(
        _Adapter(_dataset(10_080, 120)),
        ("XAUUSD",),
        observed_at=datetime(2024, 2, 1, tzinfo=timezone.utc),
        policy=_coverage_policy(),
    )
    monkeypatch.setattr(orchestrator, "assess_history_coverage", lambda *_args, **_kwargs: blocked)
    monkeypatch.setattr(orchestrator, "populate", lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not populate")))
    configuration = PipelineConfiguration(
        str(source), str(repository), str(repository / "research.sqlite3"),
        str(repository / "evidence"), ("XAUUSD",), ("M1", "H1"), (300,),
        datetime(2024, 2, 1, tzinfo=timezone.utc).isoformat(), 60, 100, 0.0,
        "INCOMPLETE_UNMEASURED", (), ResearchPolicy(200, 50, 50, 900, 100, 50),
    )

    result = run_pipeline(configuration)

    assert result.status == "BLOCKED"
    assert result.history_coverage == "evidence/research_history_coverage.json"
    assert result.population == "NOT_RUN"
    assert not (repository / "research.sqlite3").exists()
