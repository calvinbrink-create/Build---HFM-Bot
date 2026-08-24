from pathlib import Path

from cipherfx_clean.architecture_audit import build_baseline, module_inventory
from cipherfx_clean.traceability import requirement_bindings


ROOT = Path(__file__).resolve().parents[2]
SPECS = ROOT / "build_specs" / "2026-08-23"
PACKAGE = ROOT / "clean_build" / "cipherfx_clean"


def test_baseline_contains_all_requirements_and_never_infers_a_pass():
    baseline = build_baseline(
        spec_directory=SPECS,
        package_root=PACKAGE,
        declared_entrypoints=("cipherfx_clean.live_runtime",),
    )

    assert baseline["scope"]["requirement_count"] == 267
    assert baseline["scope"]["complete"] is False
    assert {row["status"] for row in baseline["requirements"]} == {"NOT_VERIFIED"}
    assert all(row["missing_evidence"] for row in baseline["requirements"])
    assert all(row["binding"]["modules"] for row in baseline["requirements"])
    assert not any(row["binding"]["missing_modules"] for row in baseline["requirements"])
    assert baseline["static_architecture"]["static_reachability_is_completion_evidence"] is False
    assert baseline["status"] == "NOT_VERIFIED"


def test_every_governing_requirement_has_one_explicit_binding():
    bindings = requirement_bindings()

    assert len(bindings) == 267
    assert set(bindings) == {
        *(f"C{number:03d}" for number in range(1, 161)),
        *(f"P{number:03d}" for number in range(1, 43)),
        *(f"F{number:02d}" for number in range(65)),
    }


def test_static_inventory_follows_real_internal_imports_and_exposes_disconnected_modules():
    rows = module_inventory(
        PACKAGE,
        declared_entrypoints=("cipherfx_clean.live_runtime",),
    )
    by_module = {row.module: row for row in rows}

    assert by_module["cipherfx_clean.live_runtime"].statically_reachable
    assert by_module["cipherfx_clean"].statically_reachable
    assert by_module["cipherfx_clean.runtime"].statically_reachable
    assert by_module["cipherfx_clean.intelligence"].statically_reachable
    assert by_module["cipherfx_clean.intelligence.engine"].statically_reachable
    assert not by_module["cipherfx_clean.management"].statically_reachable
    assert not by_module["cipherfx_clean.mt5_boundary"].statically_reachable
    assert by_module["cipherfx_clean.sharded_research"].has_cli_entrypoint
    assert not by_module["cipherfx_clean.sharded_research"].statically_reachable


def test_cli_tools_are_only_reachable_when_explicitly_requested():
    rows = {
        row.module: row
        for row in module_inventory(PACKAGE, include_cli_entrypoints=True)
    }

    assert rows["cipherfx_clean.sharded_research"].statically_reachable


def test_package_relative_imports_resolve_inside_the_actual_package():
    rows = {row.module: row for row in module_inventory(PACKAGE)}

    assert "cipherfx_clean.intelligence.engine" in rows["cipherfx_clean.intelligence"].imports
    assert "cipherfx_clean.engine" not in rows["cipherfx_clean.intelligence"].imports


def test_clean_package_has_no_forbidden_runtime_imports():
    baseline = build_baseline(
        spec_directory=SPECS,
        package_root=PACKAGE,
    )

    assert baseline["static_architecture"]["forbidden_imports"] == ()
