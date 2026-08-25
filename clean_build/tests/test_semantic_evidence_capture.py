from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
from zipfile import ZipFile

import pytest

from cipherfx_clean.compliance import EvidenceKind, RequirementStatus, certify_evidence_manifest
from cipherfx_clean.evidence_capture import (
    EvidenceSubject,
    EvidenceBundle,
    capture_c006_live_ticks,
    capture_c008_market_snapshot,
    capture_c009_market_state,
    capture_c011_live_chart,
    capture_c012_chart_pack,
    capture_c013_chart_annotations,
    capture_c014_chart_history,
    capture_live_intelligence_requirement,
    capture_live_geometry_requirement,
    capture_live_tick_intelligence_requirement,
    capture_live_analytics_requirement,
    capture_live_broker_requirement,
    capture_knowledge_library_requirement,
    capture_pattern_outcome_requirement,
    capture_historical_state_requirement,
    capture_live_outcome_metric_requirement,
    capture_live_research_requirement,
    capture_live_attribution_validation_group,
    capture_live_governance_requirement,
    capture_live_context_validation_group,
    capture_live_replay_monitoring_registry_group,
    capture_live_scorecard_governance_contract_group,
    capture_live_execution_feedback_group,
    capture_live_similarity_audit_dashboard_group,
    capture_verified_requirement,
    write_evidence_bundle,
)
from cipherfx_clean.contracts import RawTick
from cipherfx_clean.store import EvidenceStore


ROOT = Path(__file__).resolve().parents[2]
SPECS = ROOT / "build_specs" / "2026-08-23"


def test_requirement_bundle_is_accepted_only_with_requirement_specific_claims(tmp_path):
    subjects = []
    for kind in (EvidenceKind.CODE, EvidenceKind.TEST, EvidenceKind.RUNTIME, EvidenceKind.DATA):
        path = tmp_path / f"{kind.value.lower()}.txt"
        path.write_text(kind.value, encoding="utf-8")
        subjects.append(EvidenceSubject(kind, path))

    bundle = write_evidence_bundle(
        workspace_root=tmp_path,
        requirement_id="C006",
        subjects=tuple(subjects),
        output_directory=tmp_path / "evidence",
        observed_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
    )
    ledger = certify_evidence_manifest(
        spec_directory=SPECS,
        manifest_path=bundle.manifest,
        workspace_root=tmp_path,
    )

    assert ledger.result("C006").status is RequirementStatus.PASS
    assert ledger.complete is False


def test_broker_requirement_bundle_requires_exact_broker_evidence(tmp_path):
    subjects = []
    for kind in (EvidenceKind.CODE, EvidenceKind.TEST, EvidenceKind.RUNTIME, EvidenceKind.BROKER):
        path = tmp_path / f"{kind.value.lower()}.txt"
        path.write_text(kind.value, encoding="utf-8")
        subjects.append(EvidenceSubject(kind, path))

    bundle = write_evidence_bundle(
        workspace_root=tmp_path,
        requirement_id="C032",
        subjects=tuple(subjects),
        output_directory=tmp_path / "evidence",
        observed_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
    )
    ledger = certify_evidence_manifest(
        spec_directory=SPECS,
        manifest_path=bundle.manifest,
        workspace_root=tmp_path,
    )

    assert ledger.result("C032").status is RequirementStatus.PASS


def test_requirement_bundle_rejects_subject_outside_workspace(tmp_path):
    external = tmp_path.parent / "external.txt"
    external.write_text("outside", encoding="utf-8")

    with pytest.raises(ValueError, match="must remain inside workspace"):
        write_evidence_bundle(
            workspace_root=tmp_path,
            requirement_id="C006",
            subjects=(EvidenceSubject(EvidenceKind.CODE, external),),
            output_directory=tmp_path / "evidence",
        )


def test_requirement_bundle_rejects_duplicate_evidence_kinds(tmp_path):
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate evidence kind"):
        write_evidence_bundle(
            workspace_root=tmp_path,
            requirement_id="C006",
            subjects=(
                EvidenceSubject(EvidenceKind.CODE, first),
                EvidenceSubject(EvidenceKind.CODE, second),
            ),
            output_directory=tmp_path / "evidence",
        )


def test_verified_capture_records_test_and_runtime_receipts(tmp_path, monkeypatch):
    code = tmp_path / "code.py"
    test = tmp_path / "test_example.py"
    data = tmp_path / "ticks.sqlite3"
    code.write_text("pass\n", encoding="utf-8")
    test.write_text("pass\n", encoding="utf-8")
    data.write_text("tick evidence\n", encoding="utf-8")

    monkeypatch.setattr(
        "cipherfx_clean.evidence_capture._run_pytest",
        lambda *args: subprocess.CompletedProcess(("python", "-m", "pytest"), 0, "1 passed", ""),
    )
    bundle = capture_verified_requirement(
        workspace_root=tmp_path,
        requirement_id="C006",
        verification={"requirement_id": "C006", "status": "PASS"},
        code_subject=code,
        test_file=test,
        output_directory=tmp_path / "evidence",
        python_executable=Path("/python"),
        data_subjects=(data,),
    )
    ledger = certify_evidence_manifest(
        spec_directory=SPECS,
        manifest_path=bundle.manifest,
        workspace_root=tmp_path,
    )

    assert ledger.result("C006").status is RequirementStatus.PASS
    assert len(list((tmp_path / "evidence").glob("c006_*_test_receipt.json"))) == 1


def test_live_tick_capture_freezes_an_immutable_sqlite_subject(tmp_path, monkeypatch):
    source = tmp_path / "live_ticks.sqlite3"
    code_subject = tmp_path / "clean_build" / "cipherfx_clean" / "tick_ingest.py"
    test_subject = tmp_path / "clean_build" / "tests" / "test_item_006_websocket_tick_ingest.py"
    code_subject.parent.mkdir(parents=True)
    test_subject.parent.mkdir(parents=True)
    code_subject.write_text("tick_ingest = True\n", encoding="utf-8")
    test_subject.write_text("def test_tick_ingest(): pass\n", encoding="utf-8")
    store = EvidenceStore(source)
    store.write_tick(RawTick("XAUUSD", datetime(2026, 8, 24, tzinfo=timezone.utc), 100.0, 100.2))
    store.close()
    monkeypatch.setattr(
        "cipherfx_clean.evidence_capture._run_pytest",
        lambda *args: subprocess.CompletedProcess(("python", "-m", "pytest"), 0, "1 passed", ""),
    )

    bundle = capture_c006_live_ticks(
        workspace_root=tmp_path,
        tick_database=source,
        output_directory=tmp_path / "evidence",
        python_executable=Path("/python"),
    )
    snapshots = tuple((tmp_path / "evidence").glob("c006_ticks_*.sqlite3"))
    assert len(snapshots) == 1

    store = EvidenceStore(source)
    store.write_tick(RawTick("XAUUSD", datetime(2026, 8, 24, 0, 0, 1, tzinfo=timezone.utc), 100.1, 100.3))
    store.close()
    ledger = certify_evidence_manifest(
        spec_directory=SPECS,
        manifest_path=bundle.manifest,
        workspace_root=tmp_path,
    )
    assert ledger.result("C006").status is RequirementStatus.PASS


@pytest.mark.parametrize(
    ("capture", "verification_name", "requirement_id", "test_filter"),
    (
        (capture_c008_market_snapshot, "verify_c008_hfm_candle_builder", "C008", "c008"),
        (capture_c009_market_state, "verify_c009_hfm_market_states", "C009", "c009"),
    ),
)
def test_live_snapshot_capture_returns_the_requirement_specific_bundle(
    tmp_path, monkeypatch, capture, verification_name, requirement_id, test_filter,
):
    import cipherfx_clean.evidence_capture as capture_module

    tick_database = tmp_path / "ticks.sqlite3"
    tick_database.write_text("placeholder", encoding="utf-8")
    expected = EvidenceBundle(tmp_path / "envelope.json", tmp_path / "manifest.json", ("proof",))
    captured = {}
    monkeypatch.setattr(
        capture_module,
        verification_name,
        lambda **_kwargs: {"requirement_id": requirement_id, "status": "PASS"},
    )
    monkeypatch.setattr(
        capture_module,
        "capture_verified_requirement",
        lambda **kwargs: captured.update(kwargs) or expected,
    )

    result = capture(
        workspace_root=tmp_path,
        bridge_root=tmp_path,
        tick_database=tick_database,
        output_directory=tmp_path / "evidence",
        python_executable=Path("/python"),
        symbols=("XAUUSD",),
    )

    assert result is expected
    assert captured["requirement_id"] == requirement_id
    assert captured["test_arguments"] == ("-k", test_filter)


@pytest.mark.parametrize(
    ("capture", "verification_name", "requirement_id", "test_filter", "needs_timeframe"),
    (
        (capture_c011_live_chart, "verify_c011_hfm_live_chart", "C011", "c011", True),
        (capture_c012_chart_pack, "verify_c012_hfm_chart_pack", "C012", "c012", False),
        (capture_c013_chart_annotations, "verify_c013_hfm_annotations", "C013", "c013", True),
    ),
)
def test_live_chart_capture_returns_requirement_specific_bundle(
    tmp_path, monkeypatch, capture, verification_name, requirement_id, test_filter, needs_timeframe,
):
    import cipherfx_clean.evidence_capture as capture_module

    expected = EvidenceBundle(tmp_path / "envelope.json", tmp_path / "manifest.json", ("proof",))
    captured = {}
    chart_path = tmp_path / "live.svg"
    chart_path.write_text("<svg></svg>", encoding="utf-8")

    def verification(**_kwargs):
        result = {"requirement_id": requirement_id, "status": "PASS"}
        if requirement_id == "C011":
            result["chart"] = {"chart_path": str(chart_path)}
        return result

    monkeypatch.setattr(
        capture_module,
        verification_name,
        verification,
    )
    monkeypatch.setattr(
        capture_module,
        "capture_verified_requirement",
        lambda **kwargs: captured.update(kwargs) or expected,
    )
    arguments = {
        "workspace_root": tmp_path,
        "bridge_root": tmp_path,
        "output_directory": tmp_path / "evidence",
        "chart_directory": tmp_path / "charts",
        "python_executable": Path("/python"),
        "symbol": "XAUUSD",
    }
    if needs_timeframe:
        arguments["timeframe"] = "M5"

    result = capture(**arguments)

    assert result is expected
    assert captured["requirement_id"] == requirement_id
    assert captured["test_arguments"] == ("-k", test_filter)
    if requirement_id == "C011":
        assert captured["data_subjects"] == (chart_path,)


def test_live_chart_history_capture_binds_database_and_rendered_chart(tmp_path, monkeypatch):
    import cipherfx_clean.evidence_capture as capture_module

    database = tmp_path / "chart_history.sqlite3"
    database.write_text("database", encoding="utf-8")
    chart = tmp_path / "chart.svg"
    chart.write_text("<svg></svg>", encoding="utf-8")
    expected = EvidenceBundle(tmp_path / "envelope.json", tmp_path / "manifest.json", ("proof",))
    captured = {}
    monkeypatch.setattr(
        capture_module,
        "verify_c014_hfm_chart_history",
        lambda **_kwargs: {"requirement_id": "C014", "status": "PASS", "chart_path": str(chart)},
    )
    monkeypatch.setattr(
        capture_module,
        "capture_verified_requirement",
        lambda **kwargs: captured.update(kwargs) or expected,
    )

    result = capture_c014_chart_history(
        workspace_root=tmp_path,
        bridge_root=tmp_path,
        database=database,
        output_directory=tmp_path / "evidence",
        chart_directory=tmp_path / "charts",
        python_executable=Path("/python"),
        symbol="XAUUSD",
        timeframe="M5",
    )

    assert result is expected
    assert captured["requirement_id"] == "C014"
    data_subject = captured["data_subjects"][0]
    assert data_subject.name.startswith("c014_data_")
    with ZipFile(data_subject) as archive:
        assert archive.namelist() == ["00_chart_history.sqlite3", "01_chart.svg"]
    assert captured["test_file"].name == "test_item_031_chart_history.py"


@pytest.mark.parametrize(
    ("requirement_id", "verification_name", "code_name", "test_name", "test_arguments"),
    (
        ("C015", "verify_c015_hfm_price_structure", "structure.py", "test_item_030_requirement_runtime.py", ("-k", "c015")),
        ("C016", "verify_c016_hfm_swing_hierarchy", "structure.py", "test_item_030_requirement_runtime.py", ("-k", "c016")),
        ("C017", "verify_c017_hfm_supply_zones", "zones.py", "test_item_032_supply_zones.py", ("-k", "c017")),
        ("C018", "verify_c018_hfm_demand_zones", "zones.py", "test_item_033_demand_zones.py", ("-k", "c018")),
        ("C019", "verify_c019_hfm_zone_quality", "advanced.py", "test_item_034_zone_quality.py", ("-k", "c019")),
        ("C020", "verify_c020_hfm_liquidity", "liquidity.py", "test_item_035_liquidity_engine.py", ("-k", "liquidity_engine")),
    ),
)
def test_live_intelligence_capture_binds_specific_verifier_code_and_test(
    tmp_path,
    monkeypatch,
    requirement_id,
    verification_name,
    code_name,
    test_name,
    test_arguments,
):
    import cipherfx_clean.evidence_capture as capture_module

    expected = EvidenceBundle(tmp_path / "envelope.json", tmp_path / "manifest.json", ("proof",))
    captured = {}
    monkeypatch.setattr(
        capture_module,
        verification_name,
        lambda **kwargs: {
            "requirement_id": requirement_id,
            "status": "PASS",
            "symbols": {symbol: {} for symbol in kwargs["symbols"]},
        },
    )
    monkeypatch.setattr(
        capture_module,
        "capture_verified_requirement",
        lambda **kwargs: captured.update(kwargs) or expected,
    )

    result = capture_live_intelligence_requirement(
        workspace_root=tmp_path,
        requirement_id=requirement_id,
        bridge_root=tmp_path,
        output_directory=tmp_path / "evidence",
        python_executable=Path("/python"),
        symbols=("XAUUSD", "USA100"),
    )

    assert result is expected
    assert captured["requirement_id"] == requirement_id
    assert captured["code_subject"].name == code_name
    assert captured["test_file"].name == test_name
    assert captured["test_arguments"] == test_arguments


@pytest.mark.parametrize(
    ("requirement_id", "verification_name", "code_name", "test_name", "test_arguments", "requires_data"),
    (
        ("C021", "verify_c021_hfm_liquidity_sweeps", "market_features.py", "test_item_036_liquidity_sweep_detector.py", (), True),
        ("C022", "verify_c022_hfm_liquidity_failure_library", "liquidity_failures.py", "test_item_037_liquidity_failure_library.py", (), False),
        ("C023", "verify_c023_hfm_fair_value_gaps", "market_features.py", "test_item_038_fvg_engine.py", ("-k", "fvg"), False),
        ("C024", "verify_c024_hfm_fvg_lifecycle", "market_features.py", "test_item_039_fvg_lifecycle.py", ("-k", "fvg"), False),
        ("C025", "verify_c025_hfm_displacement", "market_features.py", "test_item_040_displacement_engine.py", ("-k", "displacement"), False),
    ),
)
def test_live_geometry_capture_binds_specific_verifier_code_test_and_data(
    tmp_path,
    monkeypatch,
    requirement_id,
    verification_name,
    code_name,
    test_name,
    test_arguments,
    requires_data,
):
    import cipherfx_clean.evidence_capture as capture_module

    input_snapshot = tmp_path / "inputs"
    input_snapshot.mkdir()
    (input_snapshot / "rates_XAUUSD_M5.csv").write_text("time,open\n", encoding="utf-8")
    expected = EvidenceBundle(tmp_path / "envelope.json", tmp_path / "manifest.json", ("proof",))
    captured = {}
    monkeypatch.setattr(capture_module, "_snapshot_hfm_inputs", lambda **_kwargs: input_snapshot)
    monkeypatch.setattr(
        capture_module,
        verification_name,
        lambda **kwargs: {"requirement_id": requirement_id, "status": "PASS", "arguments": kwargs},
    )
    monkeypatch.setattr(
        capture_module,
        "capture_verified_requirement",
        lambda **kwargs: captured.update(kwargs) or expected,
    )

    result = capture_live_geometry_requirement(
        workspace_root=tmp_path,
        requirement_id=requirement_id,
        bridge_root=tmp_path,
        output_directory=tmp_path / "evidence",
        python_executable=Path("/python"),
        symbols=("XAUUSD", "USA100"),
        database=tmp_path / "c022.sqlite3",
    )

    assert result is expected
    assert captured["code_subject"].name == code_name
    assert captured["test_file"].name == test_name
    assert captured["test_arguments"] == test_arguments
    assert bool(captured["data_subjects"]) is requires_data
    if requires_data:
        with ZipFile(captured["data_subjects"][0]) as archive:
            assert archive.namelist() == ["00_rates_XAUUSD_M5.csv"]


@pytest.mark.parametrize(
    ("requirement_id", "verification_name", "code_name", "test_name", "test_arguments"),
    (
        ("C026", "verify_c026_tick_directions", "market_features.py", "test_item_041_tick_direction_engine.py", ("-k", "tick_direction")),
        ("C027", "verify_c027_tick_imbalance", "market_features.py", "test_item_042_tick_imbalance_engine.py", ("-k", "tick_imbalance")),
        ("C028", "verify_c028_tick_velocity", "market_features.py", "test_item_043_tick_velocity_engine.py", ("-k", "tick_velocity")),
        ("C029", "verify_c029_tick_acceleration", "market_features.py", "test_item_044_tick_acceleration_engine.py", ("-k", "tick_acceleration")),
        ("C030", "verify_c030_microstructure", "microstructure.py", "test_item_045_microstructure_engine.py", ("-k", "microstructure")),
    ),
)
def test_live_tick_intelligence_capture_uses_an_immutable_database_snapshot(
    tmp_path,
    monkeypatch,
    requirement_id,
    verification_name,
    code_name,
    test_name,
    test_arguments,
):
    import cipherfx_clean.evidence_capture as capture_module

    source = tmp_path / "live_ticks.sqlite3"
    source.write_text("live", encoding="utf-8")
    expected = EvidenceBundle(tmp_path / "envelope.json", tmp_path / "manifest.json", ("proof",))
    captured = {}
    monkeypatch.setattr(
        capture_module,
        "_snapshot_sqlite_database",
        lambda _source, destination: destination.write_text("snapshot", encoding="utf-8"),
    )
    monkeypatch.setattr(
        capture_module,
        verification_name,
        lambda snapshot: {"requirement_id": requirement_id, "status": "PASS", "database": str(snapshot)},
    )
    monkeypatch.setattr(
        capture_module,
        "capture_verified_requirement",
        lambda **kwargs: captured.update(kwargs) or expected,
    )

    result = capture_live_tick_intelligence_requirement(
        workspace_root=tmp_path,
        requirement_id=requirement_id,
        tick_database=source,
        output_directory=tmp_path / "evidence",
        python_executable=Path("/python"),
    )

    assert result is expected
    assert captured["code_subject"].name == code_name
    assert captured["test_file"].name == test_name
    assert captured["test_arguments"] == test_arguments
    snapshot = Path(captured["verification"]["database"])
    assert snapshot.parent == tmp_path / "evidence"
    assert snapshot.read_text(encoding="utf-8") == "snapshot"


@pytest.mark.parametrize(
    ("requirement_id", "verification_name", "code_name", "test_name", "test_arguments", "requires_data"),
    (
        ("C031", "verify_c031_spread_intelligence", "spread_intelligence.py", "test_item_046_spread_intelligence.py", ("-k", "spread"), True),
        ("C034", "verify_c034_momentum_intelligence", "market_features.py", "test_item_049_momentum_intelligence.py", ("-k", "momentum"), False),
        ("C035", "verify_c035_realized_volatility", "market_features.py", "test_item_050_realized_volatility.py", ("-k", "realized_volatility"), False),
        ("C037", "verify_c037_volatility_regime", "volatility_regime.py", "test_item_052_volatility_regime.py", ("-k", "volatility_regime"), False),
        ("C038", "verify_c038_volatility_of_volatility", "volatility_dynamics.py", "test_item_053_volatility_of_volatility.py", ("-k", "volatility"), False),
        ("C039", "verify_c039_compression_detector", "volatility_dynamics.py", "test_item_054_compression_detector.py", ("-k", "compression"), True),
        ("C040", "verify_c040_expansion_detector", "volatility_dynamics.py", "test_item_055_expansion_detector.py", ("-k", "expansion"), False),
        ("C042", "verify_c042_session_profile_library", "session_profiles.py", "test_item_057_session_profiles.py", ("-k", "session"), True),
        ("C043", "verify_c043_asia_range_engine", "session_profiles.py", "test_item_058_asia_ranges.py", ("-k", "asia"), False),
        ("C044", "verify_c044_london_sweep_model", "london_sweeps.py", "test_item_059_london_sweeps.py", ("-k", "london"), False),
        ("C045", "verify_c045_ny_continuation_model", "ny_transitions.py", "test_item_060_ny_transitions.py", (), False),
        ("C046", "verify_c046_ny_reversal_model", "ny_transitions.py", "test_item_060_ny_transitions.py", (), False),
        ("C047", "verify_c047_opening_range_engine", "opening_ranges.py", "test_item_061_opening_ranges.py", ("-k", "opening_range"), False),
        ("C048", "verify_c048_opening_range_breakout_model", "opening_range_breakouts.py", "test_item_062_opening_range_breakouts.py", ("-k", "breakout"), False),
        ("C049", "verify_c049_false_opening_range_breakout_model", "false_opening_range_breakouts.py", "test_item_063_false_opening_range_breakouts.py", ("-k", "breakout"), False),
        ("C050", "verify_c050_session_vwap_engine", "session_vwap.py", "test_item_064_session_vwap.py", ("-k", "vwap"), False),
        ("C052", "verify_c052_vwap_reclaim_rejection_model", "vwap_reclaim_rejection.py", "test_item_066_vwap_reclaim_rejection.py", ("-k", "reclaim"), True),
        ("C055", "verify_c055_trend_engine", "trend_engine.py", "test_item_069_trend_engine.py", ("-k", "trend"), False),
        ("C056", "verify_c056_trend_persistence_probability", "trend_persistence.py", "test_item_070_trend_persistence.py", ("-k", "persistence"), True),
        ("C057", "verify_c057_mean_reversion_engine", "mean_reversion_engine.py", "test_item_071_mean_reversion_engine.py", ("-k", "mean_reversion"), False),
        ("C058", "verify_c058_breakout_engine", "breakout_engine.py", "test_item_072_breakout_engine.py", ("-k", "breakout"), False),
        ("C059", "verify_c059_breakout_quality_model", "breakout_quality_engine.py", "test_item_073_breakout_quality_engine.py", ("-k", "breakout"), False),
        ("C060", "verify_c060_failed_breakout_library", "failed_breakout.py", "test_item_074_failed_breakout_library.py", ("-k", "failed_breakout"), False),
        ("C061", "verify_c061_reversal_engine", "reversal_engine.py", "test_item_075_reversal_engine.py", ("-k", "reversal"), False),
        ("C062", "verify_c062_continuation_engine", "continuation_engine.py", "test_item_076_continuation_engine.py", ("-k", "continuation"), False),
        ("C063", "verify_c063_cross_asset_data_library", "cross_asset.py", "test_item_077_cross_asset.py", ("-k", "cross_asset"), False),
        ("C064", "verify_c064_correlation_engine", "cross_asset.py", "test_item_077_cross_asset.py", ("-k", "correlation"), False),
        ("C065", "verify_c065_correlation_breakdown_detector", "cross_asset.py", "test_item_077_cross_asset.py", ("-k", "breakdown"), False),
        ("C066", "verify_c066_market_regime_engine", "market_regime_engine.py", "test_item_078_market_regime.py", ("-k", "market_regime"), False),
        ("C067", "verify_c067_regime_probability_engine", "regime_probability_engine.py", "test_item_079_regime_probability.py", ("-k", "regime_probability"), False),
        ("C068", "verify_c068_strategy_router", "router.py", "test_item_080_strategy_router.py", ("-k", "strategy_router"), False),
    ),
)
def test_live_analytics_capture_binds_specific_verifier_code_test_and_required_data(
    tmp_path,
    monkeypatch,
    requirement_id,
    verification_name,
    code_name,
    test_name,
    test_arguments,
    requires_data,
):
    import cipherfx_clean.evidence_capture as capture_module

    input_snapshot = tmp_path / "inputs"
    input_snapshot.mkdir()
    (input_snapshot / "symbols.csv").write_text("symbol\n", encoding="utf-8")
    expected = EvidenceBundle(tmp_path / "envelope.json", tmp_path / "manifest.json", ("proof",))
    captured = {}
    monkeypatch.setattr(capture_module, "_snapshot_hfm_inputs", lambda **_kwargs: input_snapshot)
    monkeypatch.setattr(
        capture_module,
        verification_name,
        lambda **kwargs: {"requirement_id": requirement_id, "status": "PASS", "bridge_root": str(kwargs["bridge_root"])},
    )
    monkeypatch.setattr(
        capture_module,
        "capture_verified_requirement",
        lambda **kwargs: captured.update(kwargs) or expected,
    )

    result = capture_live_analytics_requirement(
        workspace_root=tmp_path,
        requirement_id=requirement_id,
        bridge_root=tmp_path,
        output_directory=tmp_path / "evidence",
        python_executable=Path("/python"),
        symbols=("XAUUSD", "USA100"),
    )

    assert result is expected
    assert captured["code_subject"].name == code_name
    assert captured["test_file"].name == test_name
    assert captured["test_arguments"] == test_arguments
    assert bool(captured["data_subjects"]) is requires_data


@pytest.mark.parametrize(
    ("requirement_id", "code_name"),
    (
        ("C069", "knowledge.py"),
        ("C070", "knowledge.py"),
        ("C071", "knowledge.py"),
        ("C072", "knowledge.py"),
        ("C073", "knowledge.py"),
        ("C074", "knowledge.py"),
        ("C075", "knowledge.py"),
        ("C076", "strategies.py"),
    ),
)
def test_knowledge_capture_binds_runtime_verifier_to_the_correct_library(
    tmp_path, monkeypatch, requirement_id, code_name
):
    import cipherfx_clean.evidence_capture as capture_module

    expected = EvidenceBundle(tmp_path / "envelope.json", tmp_path / "manifest.json", ("proof",))
    captured = {}
    monkeypatch.setattr(
        capture_module,
        "verify_c069_c076_knowledge_library",
        lambda received: {"requirement_id": received, "status": "PASS"},
    )
    monkeypatch.setattr(
        capture_module,
        "capture_verified_requirement",
        lambda **kwargs: captured.update(kwargs) or expected,
    )

    result = capture_knowledge_library_requirement(
        workspace_root=tmp_path,
        requirement_id=requirement_id,
        output_directory=tmp_path / "evidence",
        python_executable=Path("/python"),
    )

    assert result is expected
    assert captured["verification"]["requirement_id"] == requirement_id
    assert captured["code_subject"].name == code_name
    assert captured["test_file"].name == "test_item_081_knowledge_libraries.py"
    assert not captured.get("data_subjects", ())


@pytest.mark.parametrize(
    ("requirement_id", "capture_name", "verifier_name", "verification_id", "code_name", "test_name"),
    (
        ("C077", "capture_pattern_outcome_requirement", "verify_c077_c078_pattern_outcomes", "C077-C078", "pattern_outcomes.py", "test_item_082_pattern_outcomes.py"),
        ("C078", "capture_pattern_outcome_requirement", "verify_c077_c078_pattern_outcomes", "C077-C078", "pattern_outcomes.py", "test_item_082_pattern_outcomes.py"),
        ("C079", "capture_historical_state_requirement", "verify_c079_c084_historical_state_layer", "C079", "fingerprint_engine.py", "test_item_083_fingerprint_history.py"),
        ("C080", "capture_historical_state_requirement", "verify_c079_c084_historical_state_layer", "C080", "feature_store.py", "test_item_005_historical_memory.py"),
        ("C081", "capture_historical_state_requirement", "verify_c079_c084_historical_state_layer", "C081", "historical_memory.py", "test_item_005_historical_memory.py"),
        ("C082", "capture_historical_state_requirement", "verify_c079_c084_historical_state_layer", "C082", "historical_memory.py", "test_item_005_historical_memory.py"),
        ("C083", "capture_historical_state_requirement", "verify_c079_c084_historical_state_layer", "C083", "historical_memory.py", "test_item_005_historical_memory.py"),
        ("C084", "capture_historical_state_requirement", "verify_c079_c084_historical_state_layer", "C084", "historical_memory.py", "test_item_084_outcome_metrics.py"),
    ),
)
def test_history_capture_freezes_broker_frames_and_binds_requirement_evidence(
    tmp_path,
    monkeypatch,
    requirement_id,
    capture_name,
    verifier_name,
    verification_id,
    code_name,
    test_name,
):
    import cipherfx_clean.evidence_capture as capture_module

    snapshot = tmp_path / "inputs"
    snapshot.mkdir()
    (snapshot / "symbols.csv").write_text("symbol\n", encoding="utf-8")
    data_bundle = tmp_path / "data.zip"
    data_bundle.write_text("frozen", encoding="utf-8")
    expected = EvidenceBundle(tmp_path / "envelope.json", tmp_path / "manifest.json", ("proof",))
    captured = {}
    monkeypatch.setattr(capture_module, "_snapshot_hfm_inputs", lambda **_kwargs: snapshot)
    monkeypatch.setattr(capture_module, "_bundle_data_subject", lambda *_args: data_bundle)
    if verifier_name == "verify_c077_c078_pattern_outcomes":
        monkeypatch.setattr(
            capture_module,
            verifier_name,
            lambda **_kwargs: {"requirement_id": verification_id, "status": "PASS"},
        )
    else:
        monkeypatch.setattr(
            capture_module,
            verifier_name,
            lambda **kwargs: {"requirement_id": kwargs["requirement_id"], "status": "PASS"},
        )
    monkeypatch.setattr(
        capture_module,
        "capture_verified_requirement",
        lambda **kwargs: captured.update(kwargs) or expected,
    )

    result = getattr(capture_module, capture_name)(
        workspace_root=tmp_path,
        requirement_id=requirement_id,
        bridge_root=tmp_path,
        output_directory=tmp_path / "evidence",
        python_executable=Path("/python"),
        symbols=("XAUUSD", "UK100", "USA100", "USA500", "USA30"),
    )

    assert result is expected
    assert captured["verification"]["requirement_id"] == requirement_id
    assert captured["code_subject"].name == code_name
    assert captured["test_file"].name == test_name
    assert captured["data_subjects"] == (data_bundle,)


@pytest.mark.parametrize(
    ("requirement_id", "requires_data"),
    (
        ("C085", True),
        ("C086", False),
        ("C087", False),
        ("C088", False),
        ("C089", False),
        ("C090", True),
        ("C091", False),
        ("C092", False),
    ),
)
def test_outcome_metric_capture_binds_frozen_hfm_and_tick_inputs(
    tmp_path, monkeypatch, requirement_id, requires_data
):
    import cipherfx_clean.evidence_capture as capture_module

    snapshot = tmp_path / "inputs"
    snapshot.mkdir()
    (snapshot / "symbols.csv").write_text("symbol\n", encoding="utf-8")
    tick_source = tmp_path / "ticks.sqlite3"
    tick_source.write_text("source", encoding="utf-8")
    data_bundle = tmp_path / "data.zip"
    data_bundle.write_text("frozen", encoding="utf-8")
    expected = EvidenceBundle(tmp_path / "envelope.json", tmp_path / "manifest.json", ("proof",))
    captured = {}
    def snapshot_database(_source, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("snapshot", encoding="utf-8")

    monkeypatch.setattr(capture_module, "_snapshot_hfm_inputs", lambda **_kwargs: snapshot)
    monkeypatch.setattr(capture_module, "_snapshot_sqlite_database", snapshot_database)
    monkeypatch.setattr(capture_module, "_bundle_data_subject", lambda *_args: data_bundle)
    monkeypatch.setattr(
        capture_module,
        "verify_c085_c092_outcome_metrics",
        lambda **kwargs: {"requirement_id": kwargs["requirement_id"], "status": "PASS"},
    )
    monkeypatch.setattr(
        capture_module,
        "capture_verified_requirement",
        lambda **kwargs: captured.update(kwargs) or expected,
    )

    result = capture_live_outcome_metric_requirement(
        workspace_root=tmp_path,
        requirement_id=requirement_id,
        bridge_root=tmp_path,
        tick_database=tick_source,
        output_directory=tmp_path / "evidence",
        python_executable=Path("/python"),
        symbols=("XAUUSD", "UK100", "USA100", "USA500", "USA30"),
    )

    assert result is expected
    assert captured["verification"]["requirement_id"] == requirement_id
    assert captured["code_subject"].name == "outcome_metrics.py"
    assert captured["test_file"].name == "test_item_084_outcome_metrics.py"
    assert bool(captured["data_subjects"]) is requires_data


@pytest.mark.parametrize(
    ("requirement_id", "verifier_name", "code_name", "test_name", "requires_data"),
    (
        ("C093", "verify_c093_c098_edge_research", "edge_miner.py", "test_item_093_edge_research.py", False),
        ("C094", "verify_c093_c098_edge_research", "edge_miner.py", "test_item_093_edge_research.py", False),
        ("C095", "verify_c093_c098_edge_research", "research_pipeline.py", "test_item_093_edge_research.py", True),
        ("C096", "verify_c093_c098_edge_research", "hypothesis.py", "test_item_093_edge_research.py", False),
        ("C097", "verify_c093_c098_edge_research", "research.py", "test_item_093_edge_research.py", False),
        ("C098", "verify_c093_c098_edge_research", "validation_full.py", "test_item_093_edge_research.py", False),
        ("C099", "verify_c099_c105_outcome_libraries", "hypothesis.py", "test_item_099_outcome_libraries.py", False),
        ("C100", "verify_c099_c105_outcome_libraries", "data_library.py", "test_item_099_outcome_libraries.py", False),
        ("C101", "verify_c099_c105_outcome_libraries", "data_library.py", "test_item_099_outcome_libraries.py", False),
        ("C102", "verify_c099_c105_outcome_libraries", "data_library.py", "test_item_099_outcome_libraries.py", False),
        ("C103", "verify_c099_c105_outcome_libraries", "data_library.py", "test_item_099_outcome_libraries.py", False),
        ("C104", "verify_c099_c105_outcome_libraries", "data_library.py", "test_item_099_outcome_libraries.py", True),
        ("C105", "verify_c099_c105_outcome_libraries", "data_library.py", "test_item_099_outcome_libraries.py", False),
    ),
)
def test_research_capture_binds_frozen_hfm_inputs_to_the_verified_module(
    tmp_path, monkeypatch, requirement_id, verifier_name, code_name, test_name, requires_data
):
    import cipherfx_clean.evidence_capture as capture_module

    snapshot = tmp_path / "inputs"
    snapshot.mkdir()
    (snapshot / "symbols.csv").write_text("symbol\n", encoding="utf-8")
    data_bundle = tmp_path / "data.zip"
    data_bundle.write_text("frozen", encoding="utf-8")
    expected = EvidenceBundle(tmp_path / "envelope.json", tmp_path / "manifest.json", ("proof",))
    captured = {}
    monkeypatch.setattr(capture_module, "_snapshot_hfm_inputs", lambda **_kwargs: snapshot)
    monkeypatch.setattr(capture_module, "_bundle_data_subject", lambda *_args: data_bundle)
    monkeypatch.setattr(
        capture_module,
        verifier_name,
        lambda **kwargs: {"requirement_id": kwargs["requirement_id"], "status": "PASS"},
    )
    monkeypatch.setattr(
        capture_module,
        "capture_verified_requirement",
        lambda **kwargs: captured.update(kwargs) or expected,
    )

    result = capture_live_research_requirement(
        workspace_root=tmp_path,
        requirement_id=requirement_id,
        bridge_root=tmp_path,
        output_directory=tmp_path / "evidence",
        python_executable=Path("/python"),
        symbols=("XAUUSD", "UK100", "USA100", "USA500", "USA30"),
    )

    assert result is expected
    assert captured["verification"]["requirement_id"] == requirement_id
    assert captured["code_subject"].name == code_name
    assert captured["test_file"].name == test_name
    assert bool(captured["data_subjects"]) is requires_data


@pytest.mark.parametrize(
    ("requirement_id", "code_name", "evidence_kind"),
    (
        ("C113", "research_pipeline.py", "DATA"),
        ("C114", "research.py", "DATA"),
        ("C115", "store.py", None),
        ("C116", "edge_validation.py", "DATA"),
        ("C117", "cost_evidence.py", "BROKER"),
        ("C118", "execution_model.py", "BROKER"),
        ("C119", "hfm_data.py", "BROKER"),
        ("C120", "slippage.py", "BROKER"),
    ),
)
def test_governance_capture_uses_frozen_execution_inputs_and_correct_evidence_kind(
    tmp_path, monkeypatch, requirement_id, code_name, evidence_kind
):
    import cipherfx_clean.evidence_capture as capture_module

    hfm_snapshot = tmp_path / "hfm"
    hfm_snapshot.mkdir()
    (hfm_snapshot / "symbols.csv").write_text("symbol\n", encoding="utf-8")
    broker_snapshot = tmp_path / "broker"
    broker_snapshot.mkdir()
    for name in ("history_orders.csv", "deals.csv"):
        (broker_snapshot / name).write_text("id\n", encoding="utf-8")
    data_bundle = tmp_path / "inputs.zip"
    data_bundle.write_text("frozen", encoding="utf-8")
    expected = EvidenceBundle(tmp_path / "envelope.json", tmp_path / "manifest.json", ("proof",))
    captured = {}

    def snapshot_database(_source, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("snapshot", encoding="utf-8")

    monkeypatch.setattr(capture_module, "_snapshot_hfm_inputs", lambda **_kwargs: hfm_snapshot)
    monkeypatch.setattr(capture_module, "_snapshot_broker_exports", lambda **_kwargs: broker_snapshot)
    monkeypatch.setattr(capture_module, "_snapshot_sqlite_database", snapshot_database)
    monkeypatch.setattr(capture_module, "_bundle_data_subject", lambda *_args: data_bundle)
    monkeypatch.setattr(
        capture_module,
        "verify_c113_c120_governance_and_execution_model",
        lambda **kwargs: {"requirement_id": kwargs["requirement_id"], "status": "PASS"},
    )
    monkeypatch.setattr(
        capture_module,
        "capture_verified_requirement",
        lambda **kwargs: captured.update(kwargs) or expected,
    )

    result = capture_live_governance_requirement(
        workspace_root=tmp_path,
        requirement_id=requirement_id,
        bridge_root=tmp_path,
        output_directory=tmp_path / "evidence",
        python_executable=Path("/python"),
        symbols=("XAUUSD", "UK100", "USA100", "USA500", "USA30"),
    )

    assert result is expected
    assert captured["verification"]["requirement_id"] == requirement_id
    assert captured["code_subject"].name == code_name
    assert bool(captured["data_subjects"]) is (evidence_kind == "DATA")
    assert bool(captured["broker_subjects"]) is (evidence_kind == "BROKER")


def test_context_capture_reuses_one_frozen_multisession_snapshot_and_records_shadow(tmp_path, monkeypatch):
    import cipherfx_clean.evidence_capture as capture_module

    snapshot = tmp_path / "inputs"
    snapshot.mkdir()
    (snapshot / "symbols.csv").write_text("symbol\n", encoding="utf-8")
    news = tmp_path / "data" / "mt5_news_calendar_archive.csv"
    news.parent.mkdir()
    news.write_text("timestamp_utc,currency,impact\n", encoding="utf-8")
    captured = []
    monkeypatch.setattr(capture_module, "_snapshot_hfm_inputs", lambda **_kwargs: snapshot)
    monkeypatch.setattr(capture_module, "_snapshot_sqlite_database", lambda _source, destination: destination.write_text("shadow", encoding="utf-8"))
    monkeypatch.setattr(
        capture_module,
        "verify_c121_c128_context_validation_and_shadow",
        lambda **_kwargs: {"requirement_id": "C121", "status": "PASS"},
    )
    monkeypatch.setattr(
        capture_module,
        "capture_verified_requirement",
        lambda **kwargs: captured.append(kwargs) or EvidenceBundle(tmp_path / "envelope.json", tmp_path / f"{kwargs['requirement_id']}.json", ("proof",)),
    )

    bundles = capture_live_context_validation_group(
        workspace_root=tmp_path,
        bridge_root=tmp_path,
        output_directory=tmp_path / "evidence",
        python_executable=Path("/python"),
        symbols=("XAUUSD", "UK100", "USA100", "USA500", "USA30"),
    )

    assert set(bundles) == {f"C{number:03d}" for number in range(121, 129)}
    assert len(captured) == 8
    assert all(row["verification"]["requirement_id"] == row["requirement_id"] for row in captured)
    c128 = next(row for row in captured if row["requirement_id"] == "C128")
    assert c128["code_subject"].name == "shadow_runtime.py"
    assert bool(c128["shadow_subjects"])
    assert all(not row.get("shadow_subjects", ()) for row in captured if row["requirement_id"] != "C128")


def test_replay_capture_records_frozen_inputs_and_required_shadow_receipts(tmp_path, monkeypatch):
    import cipherfx_clean.evidence_capture as capture_module

    snapshot = tmp_path / "inputs"
    snapshot.mkdir()
    (snapshot / "symbols.csv").write_text("symbol\n", encoding="utf-8")
    frozen_bundle = tmp_path / "frozen.zip"
    frozen_bundle.write_text("frozen", encoding="utf-8")
    captured = []
    monkeypatch.setattr(capture_module, "_snapshot_hfm_inputs", lambda **_kwargs: snapshot)
    monkeypatch.setattr(capture_module, "_bundle_data_subject", lambda *_args: frozen_bundle)
    monkeypatch.setattr(
        capture_module,
        "verify_c129_c136_replay_monitoring_and_registry",
        lambda **_kwargs: {"requirement_id": "C129", "status": "PASS"},
    )
    monkeypatch.setattr(
        capture_module,
        "capture_verified_requirement",
        lambda **kwargs: captured.append(kwargs) or EvidenceBundle(tmp_path / "envelope.json", tmp_path / f"{kwargs['requirement_id']}.json", ("proof",)),
    )

    bundles = capture_live_replay_monitoring_registry_group(
        workspace_root=tmp_path,
        bridge_root=tmp_path,
        output_directory=tmp_path / "evidence",
        python_executable=Path("/python"),
        symbols=("XAUUSD", "UK100", "USA100", "USA500", "USA30"),
    )

    assert set(bundles) == {f"C{number:03d}" for number in range(129, 137)}
    assert len(captured) == 8
    assert all(row["verification"]["requirement_id"] == row["requirement_id"] for row in captured)
    assert {row["requirement_id"] for row in captured if row.get("data_subjects")} == {"C130", "C131"}
    assert {row["requirement_id"] for row in captured if row.get("shadow_subjects")} == {"C129", "C135"}


def test_attribution_capture_uses_frozen_full_day_history_for_validation(tmp_path, monkeypatch):
    import cipherfx_clean.evidence_capture as capture_module

    snapshot = tmp_path / "inputs"
    snapshot.mkdir()
    for symbol in ("XAUUSD", "UK100", "USA100", "USA500", "USA30"):
        (tmp_path / f"rates_{symbol}_M1_HISTORY.csv").write_text("time,open,high,low,close\n", encoding="utf-8")
    frozen_bundle = tmp_path / "frozen.zip"
    frozen_bundle.write_text("frozen", encoding="utf-8")
    captured = []
    monkeypatch.setattr(capture_module, "_snapshot_hfm_inputs", lambda **_kwargs: snapshot)
    monkeypatch.setattr(capture_module, "_bundle_data_subject", lambda *_args: frozen_bundle)
    monkeypatch.setattr(
        capture_module,
        "verify_c106_c112_attribution_validation",
        lambda **_kwargs: {"requirement_id": "C106", "status": "PASS"},
    )
    monkeypatch.setattr(
        capture_module,
        "capture_verified_requirement",
        lambda **kwargs: captured.append(kwargs) or EvidenceBundle(tmp_path / "envelope.json", tmp_path / f"{kwargs['requirement_id']}.json", ("proof",)),
    )

    bundles = capture_live_attribution_validation_group(
        workspace_root=tmp_path,
        bridge_root=tmp_path,
        output_directory=tmp_path / "evidence",
        python_executable=Path("/python"),
        symbols=("XAUUSD", "UK100", "USA100", "USA500", "USA30"),
    )

    assert set(bundles) == {f"C{number:03d}" for number in range(106, 113)}
    assert {row["requirement_id"] for row in captured if row.get("data_subjects")} == {"C109", "C112"}


def test_scorecard_capture_records_frozen_inputs_and_governance_shadow(tmp_path, monkeypatch):
    import cipherfx_clean.evidence_capture as capture_module

    snapshot = tmp_path / "inputs"
    snapshot.mkdir()
    (snapshot / "symbols.csv").write_text("symbol\n", encoding="utf-8")
    frozen_bundle = tmp_path / "frozen.zip"
    frozen_bundle.write_text("frozen", encoding="utf-8")
    captured = []
    monkeypatch.setattr(capture_module, "_snapshot_hfm_inputs", lambda **_kwargs: snapshot)
    monkeypatch.setattr(capture_module, "_bundle_data_subject", lambda *_args: frozen_bundle)
    monkeypatch.setattr(
        capture_module,
        "verify_c137_c144_scorecard_governance_and_contracts",
        lambda **_kwargs: {
            "requirement_id": "C137",
            "status": "PASS",
            "verified_at_utc": "2026-08-25T00:00:00+00:00",
            "promotion": {"statuses": ["CANDIDATE", "SHADOW"]},
            "source_policy": "READ_ONLY",
            "no_trade_side_effects": True,
        },
    )
    monkeypatch.setattr(
        capture_module,
        "capture_verified_requirement",
        lambda **kwargs: captured.append(kwargs) or EvidenceBundle(tmp_path / "envelope.json", tmp_path / f"{kwargs['requirement_id']}.json", ("proof",)),
    )

    bundles = capture_live_scorecard_governance_contract_group(
        workspace_root=tmp_path,
        bridge_root=tmp_path,
        output_directory=tmp_path / "evidence",
        python_executable=Path("/python"),
        symbols=("XAUUSD", "UK100", "USA100", "USA500", "USA30"),
    )

    assert set(bundles) == {f"C{number:03d}" for number in range(137, 145)}
    assert len(captured) == 8
    assert all(row["verification"]["requirement_id"] == row["requirement_id"] for row in captured)
    assert {row["requirement_id"] for row in captured if row.get("data_subjects")} == {"C138", "C140"}
    assert {row["requirement_id"] for row in captured if row.get("shadow_subjects")} == {"C138"}


def test_execution_feedback_capture_binds_broker_exports_without_live_order_calls(tmp_path, monkeypatch):
    import cipherfx_clean.evidence_capture as capture_module

    snapshot = tmp_path / "inputs"
    snapshot.mkdir()
    broker = tmp_path / "broker"
    broker.mkdir()
    for name in ("history_orders.csv", "deals.csv"):
        (broker / name).write_text("id\n", encoding="utf-8")
    frozen_bundle = tmp_path / "frozen.zip"
    frozen_bundle.write_text("frozen", encoding="utf-8")
    captured = []
    monkeypatch.setattr(capture_module, "_snapshot_hfm_inputs", lambda **_kwargs: snapshot)
    monkeypatch.setattr(capture_module, "_snapshot_broker_exports", lambda **_kwargs: broker)
    monkeypatch.setattr(capture_module, "_bundle_data_subject", lambda *_args: frozen_bundle)
    monkeypatch.setattr(
        capture_module,
        "verify_c145_c152_execution_feedback_and_replay",
        lambda **_kwargs: {"requirement_id": "C145", "status": "PASS"},
    )
    monkeypatch.setattr(
        capture_module,
        "capture_verified_requirement",
        lambda **kwargs: captured.append(kwargs) or EvidenceBundle(tmp_path / "envelope.json", tmp_path / f"{kwargs['requirement_id']}.json", ("proof",)),
    )

    bundles = capture_live_execution_feedback_group(
        workspace_root=tmp_path,
        bridge_root=tmp_path,
        output_directory=tmp_path / "evidence",
        python_executable=Path("/python"),
        symbols=("XAUUSD", "UK100", "USA100", "USA500", "USA30"),
    )

    assert set(bundles) == {f"C{number:03d}" for number in range(145, 153)}
    assert {row["requirement_id"] for row in captured if row.get("data_subjects")} == {"C150"}
    assert {row["requirement_id"] for row in captured if row.get("broker_subjects")} == {"C146", "C147", "C148", "C149", "C150"}


def test_similarity_capture_binds_audit_database_and_broker_exports(tmp_path, monkeypatch):
    import cipherfx_clean.evidence_capture as capture_module

    snapshot = tmp_path / "inputs"
    snapshot.mkdir()
    for symbol in ("XAUUSD", "UK100", "USA100", "USA500", "USA30"):
        (tmp_path / f"rates_{symbol}_M1_HISTORY.csv").write_text("time,open,high,low,close\n", encoding="utf-8")
    broker = tmp_path / "broker"
    broker.mkdir()
    for name in ("history_orders.csv", "deals.csv"):
        (broker / name).write_text("id\n", encoding="utf-8")
    frozen_bundle = tmp_path / "frozen.zip"
    frozen_bundle.write_text("frozen", encoding="utf-8")
    captured = []
    monkeypatch.setattr(capture_module, "_snapshot_hfm_inputs", lambda **_kwargs: snapshot)
    monkeypatch.setattr(capture_module, "_snapshot_broker_exports", lambda **_kwargs: broker)
    monkeypatch.setattr(capture_module, "_bundle_data_subject", lambda *_args: frozen_bundle)

    def verifier(**kwargs):
        kwargs["audit_database"].write_text("audit", encoding="utf-8")
        return {"requirement_id": "C153", "status": "PASS"}

    monkeypatch.setattr(capture_module, "verify_c153_c160_similarity_audit_dashboard_and_flow", verifier)
    monkeypatch.setattr(
        capture_module,
        "capture_verified_requirement",
        lambda **kwargs: captured.append(kwargs) or EvidenceBundle(tmp_path / "envelope.json", tmp_path / f"{kwargs['requirement_id']}.json", ("proof",)),
    )

    bundles = capture_live_similarity_audit_dashboard_group(
        workspace_root=tmp_path,
        bridge_root=tmp_path,
        output_directory=tmp_path / "evidence",
        python_executable=Path("/python"),
        symbols=("XAUUSD", "UK100", "USA100", "USA500", "USA30"),
    )

    assert set(bundles) == {f"C{number:03d}" for number in range(153, 161)}
    assert {row["requirement_id"] for row in captured if row.get("data_subjects")} == {"C153", "C154", "C157", "C160"}
    assert {row["requirement_id"] for row in captured if row.get("broker_subjects")} == {"C157", "C160"}


def test_hfm_input_snapshot_includes_combined_m1_sources_and_live_ticks(tmp_path):
    import cipherfx_clean.evidence_capture as capture_module

    source = tmp_path / "source"
    source.mkdir()
    for name in (
        "symbols.csv",
        "rates_XAUUSD_M1.csv",
        "deephistory_XAUUSD_M1.csv",
        "rates_XAUUSD_M1_HISTORY.csv",
        "tick_XAUUSD.txt",
    ):
        (source / name).write_text(name, encoding="utf-8")

    snapshot = capture_module._snapshot_hfm_inputs(
        destination=tmp_path / "evidence",
        requirement_id="C031",
        bridge_root=source,
        symbols=("XAUUSD",),
        timeframes=("M1",),
        include_combined_m1=True,
        include_ticks=True,
    )

    assert sorted(path.name for path in snapshot.iterdir()) == sorted(path.name for path in source.iterdir())


@pytest.mark.parametrize(
    ("requirement_id", "verification_name", "code_name", "test_name", "test_arguments"),
    (
        ("C032", "verify_c032_slippage_intelligence", "slippage.py", "test_item_047_slippage_intelligence.py", ("-k", "slippage")),
        ("C033", "verify_c033_latency_intelligence", "latency.py", "test_item_048_latency_intelligence.py", ("-k", "latency")),
    ),
)
def test_live_broker_capture_binds_exact_hfm_exports_as_broker_evidence(
    tmp_path,
    monkeypatch,
    requirement_id,
    verification_name,
    code_name,
    test_name,
    test_arguments,
):
    import cipherfx_clean.evidence_capture as capture_module

    exports = tmp_path / "exports"
    exports.mkdir()
    for name in ("history_orders.csv", "deals.csv"):
        (exports / name).write_text(name, encoding="utf-8")
    expected = EvidenceBundle(tmp_path / "envelope.json", tmp_path / "manifest.json", ("proof",))
    captured = {}
    monkeypatch.setattr(capture_module, "_snapshot_broker_exports", lambda **_kwargs: exports)
    monkeypatch.setattr(
        capture_module,
        verification_name,
        lambda **kwargs: {"requirement_id": requirement_id, "status": "PASS", "database": str(kwargs["database"])},
    )
    monkeypatch.setattr(
        capture_module,
        "capture_verified_requirement",
        lambda **kwargs: captured.update(kwargs) or expected,
    )

    result = capture_live_broker_requirement(
        workspace_root=tmp_path,
        requirement_id=requirement_id,
        bridge_root=tmp_path,
        database=tmp_path / "measurements.sqlite3",
        output_directory=tmp_path / "evidence",
        python_executable=Path("/python"),
        symbols=("XAUUSD", "UK100", "USA100", "USA500", "USA30"),
    )

    assert result is expected
    assert captured["code_subject"].name == code_name
    assert captured["test_file"].name == test_name
    assert captured["test_arguments"] == test_arguments
    with ZipFile(captured["broker_subjects"][0]) as archive:
        assert archive.namelist() == ["00_deals.csv", "01_history_orders.csv"]


def test_live_session_broker_capture_binds_exact_hfm_tick_exports_as_broker_evidence(tmp_path, monkeypatch):
    import cipherfx_clean.evidence_capture as capture_module

    exports = tmp_path / "exports"
    exports.mkdir()
    for symbol in ("XAUUSD", "UK100", "USA100", "USA500", "USA30"):
        (exports / f"tick_{symbol}.txt").write_text(symbol, encoding="utf-8")
    expected = EvidenceBundle(tmp_path / "envelope.json", tmp_path / "manifest.json", ("proof",))
    captured = {}
    monkeypatch.setattr(capture_module, "_snapshot_broker_tick_exports", lambda **_kwargs: exports)
    monkeypatch.setattr(
        capture_module,
        "verify_c041_session_engine",
        lambda **kwargs: {"requirement_id": "C041", "status": "PASS", "bridge_root": str(kwargs["bridge_root"])},
    )
    monkeypatch.setattr(
        capture_module,
        "capture_verified_requirement",
        lambda **kwargs: captured.update(kwargs) or expected,
    )

    result = capture_module.capture_live_session_broker_requirement(
        workspace_root=tmp_path,
        requirement_id="C041",
        bridge_root=tmp_path,
        output_directory=tmp_path / "evidence",
        python_executable=Path("/python"),
        symbols=("XAUUSD", "UK100", "USA100", "USA500", "USA30"),
    )

    assert result is expected
    assert captured["code_subject"].name == "session.py"
    assert captured["test_file"].name == "test_item_056_session_engine.py"
    assert captured["test_arguments"] == ()
    with ZipFile(captured["broker_subjects"][0]) as archive:
        assert archive.namelist() == [
            "00_tick_UK100.txt",
            "01_tick_USA100.txt",
            "02_tick_USA30.txt",
            "03_tick_USA500.txt",
            "04_tick_XAUUSD.txt",
        ]


def test_broker_tick_export_snapshot_contains_only_requested_exports(tmp_path):
    import cipherfx_clean.evidence_capture as capture_module

    source = tmp_path / "source"
    source.mkdir()
    (source / "tick_XAUUSD.txt").write_text("tick", encoding="utf-8")
    (source / "rates_XAUUSD_M1.csv").write_text("bar", encoding="utf-8")

    snapshot = capture_module._snapshot_broker_tick_exports(
        destination=tmp_path / "evidence",
        requirement_id="C041",
        bridge_root=source,
        symbols=("XAUUSD",),
    )

    assert [path.name for path in snapshot.iterdir()] == ["tick_XAUUSD.txt"]


def test_live_activity_broker_capture_binds_exact_hfm_activity_exports(tmp_path, monkeypatch):
    import cipherfx_clean.evidence_capture as capture_module

    exports = tmp_path / "exports"
    exports.mkdir()
    for name in ("symbols.csv", "rates_XAUUSD_M1.csv", "deephistory_XAUUSD_M1.csv", "tick_XAUUSD.txt"):
        (exports / name).write_text(name, encoding="utf-8")
    expected = EvidenceBundle(tmp_path / "envelope.json", tmp_path / "manifest.json", ("proof",))
    captured = {}
    monkeypatch.setattr(capture_module, "_snapshot_hfm_inputs", lambda **_kwargs: exports)
    monkeypatch.setattr(
        capture_module,
        "verify_c053_tick_volume_engine",
        lambda **kwargs: {"requirement_id": "C053", "status": "PASS", "bridge_root": str(kwargs["bridge_root"])},
    )
    monkeypatch.setattr(
        capture_module,
        "capture_verified_requirement",
        lambda **kwargs: captured.update(kwargs) or expected,
    )

    result = capture_module.capture_live_activity_broker_requirement(
        workspace_root=tmp_path,
        requirement_id="C053",
        bridge_root=tmp_path,
        output_directory=tmp_path / "evidence",
        python_executable=Path("/python"),
        symbols=("XAUUSD",),
    )

    assert result is expected
    assert captured["code_subject"].name == "tick_volume.py"
    assert captured["test_file"].name == "test_item_067_tick_volume.py"
    assert captured["test_arguments"] == ("-k", "tick_volume")
    with ZipFile(captured["broker_subjects"][0]) as archive:
        assert archive.namelist() == [
            "00_deephistory_XAUUSD_M1.csv",
            "01_rates_XAUUSD_M1.csv",
            "02_symbols.csv",
            "03_tick_XAUUSD.txt",
        ]


def test_c007_capture_binds_persisted_fresh_and_stale_tick_evidence(tmp_path, monkeypatch):
    import cipherfx_clean.evidence_capture as capture_module

    source = tmp_path / "live.sqlite3"
    source.write_text("live", encoding="utf-8")
    expected = EvidenceBundle(tmp_path / "envelope.json", tmp_path / "manifest.json", ("proof",))
    captured = {}
    monkeypatch.setattr(
        capture_module,
        "_snapshot_sqlite_database",
        lambda _source, destination: destination.write_text("snapshot", encoding="utf-8"),
    )
    monkeypatch.setattr(
        capture_module,
        "_c007_payload_from_quality_events",
        lambda _snapshot: {"results": ("persisted",)},
    )
    monkeypatch.setattr(
        capture_module,
        "verify_c007_tick_quality",
        lambda payload, **kwargs: {"requirement_id": "C007", "status": "PASS", "payload": payload, **kwargs},
    )
    monkeypatch.setattr(
        capture_module,
        "capture_verified_requirement",
        lambda **kwargs: captured.update(kwargs) or expected,
    )

    result = capture_module.capture_c007_live_tick_quality(
        workspace_root=tmp_path,
        tick_database=source,
        output_directory=tmp_path / "evidence",
        python_executable=Path("/python"),
        maximum_age_seconds=60.0,
    )

    assert result is expected
    assert captured["code_subject"].name == "observation.py"
    assert captured["test_file"].name == "test_item_030_requirement_runtime.py"
    assert captured["test_arguments"] == ("-k", "c007")
    assert captured["verification"]["payload"] == {"results": ("persisted",)}
    snapshot = captured["data_subjects"][0]
    assert snapshot.read_text(encoding="utf-8") == "snapshot"


def test_c007_payload_uses_persisted_tick_quality_events(tmp_path):
    import cipherfx_clean.evidence_capture as capture_module

    database = tmp_path / "quality.sqlite3"
    store = EvidenceStore(database)
    now = datetime(2026, 8, 24, 20, 0, tzinfo=timezone.utc)
    try:
        for symbol, status, tick_at, reason in (
            ("XAUUSD", "VALID", now - timedelta(seconds=1), "VALID_TICK"),
            ("UK100", "STALE", now - timedelta(seconds=61), "OUTSIDE_FRESHNESS_WINDOW"),
        ):
            store.write_tick_quality_event(
                f"{symbol}:{status}",
                symbol,
                now,
                "FRESH" if status == "VALID" else "STALE",
                {
                    "quality": status,
                    "quality_reason": reason,
                    "tick": {"timestamp": tick_at.isoformat()},
                    "source_path": f"/broker/tick_{symbol}.txt",
                },
            )
    finally:
        store.close()

    payload = capture_module._c007_payload_from_quality_events(database)

    assert payload["results"] == (
        {
            "symbol": "UK100",
            "status": "STALE",
            "reason": "OUTSIDE_FRESHNESS_WINDOW",
            "observed_at": now.isoformat(),
            "tick_at": (now - timedelta(seconds=61)).isoformat(),
            "source_path": "/broker/tick_UK100.txt",
        },
        {
            "symbol": "XAUUSD",
            "status": "VALID",
            "reason": "VALID_TICK",
            "observed_at": now.isoformat(),
            "tick_at": (now - timedelta(seconds=1)).isoformat(),
            "source_path": "/broker/tick_XAUUSD.txt",
        },
    )
