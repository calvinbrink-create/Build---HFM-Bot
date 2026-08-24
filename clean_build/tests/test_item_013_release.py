from datetime import datetime, timezone

from cipherfx_clean.release import (
    CertificationInput,
    build_release_bundle,
    certify_edge,
    verify_release_bundle,
)


def _certification(**changes):
    values = {
        "edge_id": "edge-1",
        "edge_version": 1,
        "dataset_ids": ("dataset-1",),
        "dataset_quality_status": "PASS",
        "validation_id": "validation-1",
        "validation_status": "PASS",
        "path_fidelity": "HFM_EXECUTABLE_TICKS",
        "shadow_evidence_id": "shadow-1",
        "shadow_status": "PASS",
        "shadow_samples": 100,
        "minimum_shadow_samples": 100,
        "cost_evidence_id": "cost-profile-1",
        "cost_evidence_status": "COMPLETE",
    }
    values.update(changes)
    return certify_edge(CertificationInput(**values))


def test_certification_fails_closed_without_executable_and_shadow_evidence():
    result = _certification(
        path_fidelity="BAR_OHLC_NO_EXECUTABLE_TICKS",
        shadow_evidence_id=None,
        shadow_status="NOT_RUN",
        shadow_samples=0,
    )

    assert result.status == "BLOCKED"
    assert "EXECUTABLE_TICK_EVIDENCE_MISSING" in result.reasons
    assert "SHADOW_FORWARD_EVIDENCE_NOT_PASS" in result.reasons


def test_certification_fails_closed_without_measured_cost_evidence():
    result = _certification(cost_evidence_id=None, cost_evidence_status="INSUFFICIENT_SAMPLES")

    assert result.status == "BLOCKED"
    assert "MEASURED_COST_EVIDENCE_NOT_COMPLETE" in result.reasons


def test_release_identity_is_reproducible_and_independent_verifier_detects_change(tmp_path):
    code = tmp_path / "code.py"
    config = tmp_path / "config.json"
    code.write_text("value = 1\n")
    config.write_text('{"mode":"shadow"}\n')
    manifest = tmp_path / "release.json"

    first = build_release_bundle(
        root=tmp_path,
        artifacts={"CODE": code, "CONFIGURATION": config},
        certifications=(_certification(),),
        output=manifest,
        created_at=datetime(2026, 8, 23, tzinfo=timezone.utc),
    )
    second = build_release_bundle(
        root=tmp_path,
        artifacts={"CONFIGURATION": config, "CODE": code},
        certifications=(_certification(),),
        output=manifest,
        created_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
    )

    assert first.release_id == second.release_id
    assert verify_release_bundle(tmp_path, manifest).status == "PASS"
    code.write_text("value = 2\n")
    changed = verify_release_bundle(tmp_path, manifest)
    assert changed.status == "FAIL"
    assert any(reason.startswith("DIGEST_MISMATCH") for reason in changed.reasons)
