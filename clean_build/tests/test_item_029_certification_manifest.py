from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys

import pytest

from cipherfx_clean.certification import certification_report, main
from cipherfx_clean.compliance import (
    EvidenceKind,
    RequirementStatus,
    certify_evidence_manifest,
    load_verified_evidence_manifest,
)


ROOT = Path(__file__).resolve().parents[2]
SPECS = ROOT / "build_specs" / "2026-08-23"


def _manifest(tmp_path, *, requirement_id="F02", kind=EvidenceKind.CODE):
    subject = tmp_path / "subject.txt"
    subject.write_text("verified subject\n", encoding="utf-8")
    observed_at = datetime.now(timezone.utc).isoformat()
    evidence_id = f"{requirement_id}:{kind.value}:1"
    artifact = tmp_path / "artifact.json"
    artifact.write_text(json.dumps({
        "schema_version": 1,
        "evidence_claims": {
            evidence_id: {
                "evidence_id": evidence_id,
                "requirement_id": requirement_id,
                "kind": kind.value,
                "result": "PASS",
                "observed_at": observed_at,
                "subject": {
                    "path": subject.name,
                    "digest": f"sha256:{sha256(subject.read_bytes()).hexdigest()}",
                },
            },
        },
    }), encoding="utf-8")
    payload = {
        "schema_version": 2,
        "evidence": [
            {
                "evidence_id": evidence_id,
                "requirement_id": requirement_id,
                "kind": kind.value,
                "location": f"{artifact.name}#{evidence_id}",
                "digest": f"sha256:{sha256(artifact.read_bytes()).hexdigest()}",
                "observed_at": observed_at,
                "result": "PASS",
            }
        ],
    }
    manifest = tmp_path / "evidence.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    return manifest, artifact


def test_verified_manifest_loads_content_addressed_evidence(tmp_path):
    manifest, _ = _manifest(tmp_path)
    rows = load_verified_evidence_manifest(manifest, workspace_root=tmp_path)

    assert len(rows) == 1
    assert rows[0].requirement_id == "F02"
    assert rows[0].kind is EvidenceKind.CODE


def test_tampered_artifact_is_rejected(tmp_path):
    manifest, artifact = _manifest(tmp_path)
    artifact.write_text("changed after evidence capture\n", encoding="utf-8")

    with pytest.raises(ValueError, match="digest mismatch"):
        load_verified_evidence_manifest(manifest, workspace_root=tmp_path)


def test_evidence_location_cannot_escape_workspace(tmp_path):
    manifest, _ = _manifest(tmp_path)
    payload = json.loads(manifest.read_text())
    payload["evidence"][0]["location"] = "../outside.json#F02:CODE:1"
    manifest.write_text(json.dumps(payload))

    with pytest.raises(ValueError, match="escapes workspace"):
        load_verified_evidence_manifest(manifest, workspace_root=tmp_path)


def test_partial_manifest_never_certifies_complete_requirement_or_build(tmp_path):
    manifest, _ = _manifest(tmp_path)
    ledger = certify_evidence_manifest(
        spec_directory=SPECS,
        manifest_path=manifest,
        workspace_root=tmp_path,
    )
    report = certification_report(ledger)

    assert ledger.result("F02").status is RequirementStatus.NOT_VERIFIED
    assert ledger.result("F02").missing == {EvidenceKind.TEST, EvidenceKind.RUNTIME}
    assert report["complete"] is False
    assert report["status"] == "NOT_VERIFIED"
    assert len(report["requirements"]) == 267


def test_unknown_requirement_cannot_be_smuggled_into_certificate(tmp_path):
    manifest, _ = _manifest(tmp_path, requirement_id="F99")

    with pytest.raises(KeyError, match="F99"):
        certify_evidence_manifest(
            spec_directory=SPECS,
            manifest_path=manifest,
            workspace_root=tmp_path,
        )


def test_duplicate_evidence_identity_is_rejected(tmp_path):
    manifest, _ = _manifest(tmp_path)
    payload = json.loads(manifest.read_text())
    payload["evidence"].append(dict(payload["evidence"][0]))
    manifest.write_text(json.dumps(payload))

    with pytest.raises(ValueError, match="duplicate evidence id"):
        load_verified_evidence_manifest(manifest, workspace_root=tmp_path)


def test_v1_manifest_without_requirement_specific_claims_is_rejected(tmp_path):
    manifest, _ = _manifest(tmp_path)
    payload = json.loads(manifest.read_text())
    payload["schema_version"] = 1
    manifest.write_text(json.dumps(payload))

    with pytest.raises(ValueError, match="schema_version must be 2"):
        load_verified_evidence_manifest(manifest, workspace_root=tmp_path)


def test_claim_cannot_be_relabelled_as_a_different_evidence_kind(tmp_path):
    manifest, _ = _manifest(tmp_path, kind=EvidenceKind.CODE)
    payload = json.loads(manifest.read_text())
    payload["evidence"][0]["kind"] = EvidenceKind.BROKER.value
    payload["evidence"][0]["evidence_id"] = "F02:BROKER:1"
    payload["evidence"][0]["location"] = "artifact.json#F02:BROKER:1"
    manifest.write_text(json.dumps(payload))

    with pytest.raises(ValueError, match="evidence claim is missing"):
        load_verified_evidence_manifest(manifest, workspace_root=tmp_path)


def test_tampered_subject_is_rejected_even_when_envelope_hash_matches(tmp_path):
    manifest, _ = _manifest(tmp_path)
    subject = tmp_path / "subject.txt"
    subject.write_text("changed subject\n", encoding="utf-8")
    payload = json.loads(manifest.read_text())
    artifact = tmp_path / "artifact.json"
    payload["evidence"][0]["digest"] = f"sha256:{sha256(artifact.read_bytes()).hexdigest()}"
    manifest.write_text(json.dumps(payload))

    with pytest.raises(ValueError, match="subject digest mismatch"):
        load_verified_evidence_manifest(manifest, workspace_root=tmp_path)


def test_certification_cli_writes_not_verified_report_when_evidence_is_stale(tmp_path, monkeypatch):
    manifest, artifact = _manifest(tmp_path)
    artifact.write_text("changed after evidence capture\n", encoding="utf-8")
    output = tmp_path / "certification.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "certification",
            "--spec-directory", str(SPECS),
            "--manifest", str(manifest),
            "--workspace-root", str(tmp_path),
            "--output", str(output),
        ],
    )

    assert main() == 2
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "NOT_VERIFIED"
    assert report["complete"] is False
    assert report["reason"].startswith("EVIDENCE_MANIFEST_REJECTED:ValueError:")
