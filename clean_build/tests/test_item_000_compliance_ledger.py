from datetime import datetime, timezone
from pathlib import Path

import pytest

from cipherfx_clean.compliance import (
    ComplianceLedger,
    MasterComplianceLedger,
    EvidenceKind,
    EvidenceRecord,
    RequirementStatus,
    parse_friday_ledger,
    parse_numbered_document,
)


ROOT = Path(__file__).resolve().parents[2]
SPECS = ROOT / "build_specs" / "2026-08-23"


def test_all_three_governing_documents_are_machine_readable():
    matrix = parse_numbered_document(SPECS / "spec_component_matrix.md", "C", 160)
    principles = parse_numbered_document(SPECS / "spec_edge_system_principles.md", "P", 42)
    friday = parse_friday_ledger(SPECS / "FRIDAY_SCOPE_LEDGER.md")

    assert len(matrix) == 160
    assert len(principles) == 42
    assert len(friday) == 65
    assert len({row.requirement_id for row in matrix + principles + friday}) == 267
    assert EvidenceKind.BROKER in next(row for row in matrix if row.requirement_id == "C002").required_evidence
    assert EvidenceKind.BROKER in next(row for row in matrix if row.requirement_id == "C003").required_evidence
    assert EvidenceKind.DATA in next(row for row in matrix if row.requirement_id == "C006").required_evidence
    assert EvidenceKind.BROKER not in next(row for row in matrix if row.requirement_id == "C024").required_evidence
    assert EvidenceKind.BROKER not in next(row for row in principles if row.requirement_id == "P014").required_evidence
    assert EvidenceKind.BROKER not in next(row for row in friday if row.requirement_id == "F11").required_evidence


def test_file_or_interface_presence_can_never_mark_a_requirement_pass():
    requirement = parse_friday_ledger(SPECS / "FRIDAY_SCOPE_LEDGER.md")[0]
    ledger = ComplianceLedger((requirement,))

    assert ledger.result(requirement.requirement_id).status is RequirementStatus.NOT_VERIFIED
    assert ledger.complete is False


def test_requirement_pass_requires_every_declared_evidence_kind():
    requirement = parse_friday_ledger(SPECS / "FRIDAY_SCOPE_LEDGER.md")[0]
    ledger = ComplianceLedger((requirement,))
    now = datetime.now(timezone.utc).isoformat()
    for kind in requirement.required_evidence:
        ledger.add(EvidenceRecord(f"{requirement.requirement_id}:{kind.value}", requirement.requirement_id, kind, "test-location", "sha256:test", now, "PASS"))

    assert ledger.result(requirement.requirement_id).status is RequirementStatus.PASS
    assert ledger.complete is True


def test_failing_evidence_is_rejected_instead_of_being_reported_as_pass():
    requirement = parse_friday_ledger(SPECS / "FRIDAY_SCOPE_LEDGER.md")[0]
    with pytest.raises(ValueError, match="only passing evidence"):
        EvidenceRecord("bad", requirement.requirement_id, EvidenceKind.TEST, "test", "sha256:test", datetime.now(timezone.utc).isoformat(), "FAIL")


def test_master_ledger_refuses_item_only_scope_as_build_certificate():
    requirement = parse_friday_ledger(SPECS / "FRIDAY_SCOPE_LEDGER.md")[1]

    with pytest.raises(ValueError, match="all 267 governing requirements"):
        MasterComplianceLedger((requirement,))


def test_master_ledger_reports_each_document_and_whole_build_independently():
    ledger = MasterComplianceLedger.from_spec_directory(SPECS)

    assert len(ledger.results()) == 267
    assert ledger.complete is False
    assert [(row.document, row.passed, row.required, row.status) for row in ledger.document_results()] == [
        ("component_matrix", 0, 160, RequirementStatus.NOT_VERIFIED),
        ("edge_system_principles", 0, 42, RequirementStatus.NOT_VERIFIED),
        ("friday_scope", 0, 65, RequirementStatus.NOT_VERIFIED),
    ]


def test_item_pass_does_not_promote_its_document_or_master_build():
    ledger = MasterComplianceLedger.from_spec_directory(SPECS)
    requirement = next(row.requirement for row in ledger.results() if row.requirement.requirement_id == "F02")
    now = datetime.now(timezone.utc).isoformat()
    for kind in requirement.required_evidence:
        ledger.add(EvidenceRecord(f"F02:{kind.value}", "F02", kind, "test-location", "sha256:test", now, "PASS"))

    assert ledger.result("F02").status is RequirementStatus.PASS
    friday = next(row for row in ledger.document_results() if row.document == "friday_scope")
    assert friday.passed == 1
    assert friday.status is RequirementStatus.NOT_VERIFIED
    assert ledger.complete is False
