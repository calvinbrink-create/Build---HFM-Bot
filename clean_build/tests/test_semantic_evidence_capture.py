from datetime import datetime, timezone
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
