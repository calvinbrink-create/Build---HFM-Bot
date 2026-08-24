from datetime import datetime, timezone
from pathlib import Path

import pytest

from cipherfx_clean.compliance import EvidenceKind, RequirementStatus, certify_evidence_manifest
from cipherfx_clean.evidence_capture import EvidenceSubject, write_evidence_bundle


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
