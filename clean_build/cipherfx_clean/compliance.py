"""Machine-checkable requirement and evidence ledger for the clean build.

A requirement is never inferred to pass from a module, class, or test name.
Every PASS requires explicit code, test, runtime-path, and (when required)
dataset or broker evidence records.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Iterable, Mapping, Sequence


GOVERNING_DOCUMENTS: Mapping[str, tuple[str, int, int, int]] = {
    "component_matrix": ("C", 1, 160, 3),
    "edge_system_principles": ("P", 1, 42, 3),
    "friday_scope": ("F", 0, 65, 2),
}
MASTER_REQUIREMENT_COUNT = sum(count for _, _, count, _ in GOVERNING_DOCUMENTS.values())

BROKER_EVIDENCE_REQUIREMENTS = frozenset(
    {
        "C001", "C002", "C003", "C004", "C005", "C032", "C033", "C041",
        "C053", "C117", "C118", "C119", "C120", "C146", "C147", "C148",
        "C149", "C150", "C157", "C160",
        "P003", "P029", "P030", "P037", "P038", "P039",
        "F06", "F07", "F17", "F21", "F25", "F31", "F38", "F45", "F59",
        "F61", "F64",
    }
)


class EvidenceKind(str, Enum):
    CODE = "CODE"
    TEST = "TEST"
    RUNTIME = "RUNTIME"
    DATA = "DATA"
    BROKER = "BROKER"
    SHADOW = "SHADOW"


class RequirementStatus(str, Enum):
    NOT_VERIFIED = "NOT_VERIFIED"
    PASS = "PASS"


@dataclass(frozen=True)
class Requirement:
    requirement_id: str
    title: str
    text: str
    required_evidence: frozenset[EvidenceKind]


@dataclass(frozen=True)
class EvidenceRecord:
    evidence_id: str
    requirement_id: str
    kind: EvidenceKind
    location: str
    digest: str
    observed_at: str
    result: str

    def __post_init__(self) -> None:
        if not all((self.evidence_id, self.requirement_id, self.location, self.digest, self.observed_at)):
            raise ValueError("evidence identity, location, digest, and timestamp are required")
        if self.result != "PASS":
            raise ValueError("only passing evidence can satisfy a requirement")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "EvidenceRecord":
        try:
            kind = EvidenceKind(str(value["kind"]))
            return cls(
                evidence_id=str(value["evidence_id"]),
                requirement_id=str(value["requirement_id"]),
                kind=kind,
                location=str(value["location"]),
                digest=str(value["digest"]),
                observed_at=str(value["observed_at"]),
                result=str(value["result"]),
            )
        except KeyError as exc:
            raise ValueError(f"evidence record is missing {exc.args[0]}") from exc

    def to_mapping(self) -> Mapping[str, object]:
        return {
            "evidence_id": self.evidence_id,
            "requirement_id": self.requirement_id,
            "kind": self.kind.value,
            "location": self.location,
            "digest": self.digest,
            "observed_at": self.observed_at,
            "result": self.result,
        }


@dataclass(frozen=True)
class RequirementResult:
    requirement: Requirement
    status: RequirementStatus
    evidence: tuple[EvidenceRecord, ...]
    missing: frozenset[EvidenceKind] = field(default_factory=frozenset)


@dataclass(frozen=True)
class DocumentResult:
    document: str
    status: RequirementStatus
    passed: int
    required: int
    missing_requirement_ids: tuple[str, ...]


def parse_numbered_document(path: Path, prefix: str, count: int) -> tuple[Requirement, ...]:
    """Parse the compact numbered source documents without reformatting them."""

    raw = path.read_text(encoding="utf-8").strip()
    starts: list[tuple[int, int]] = []
    cursor = 0
    for number in range(1, count + 1):
        match = re.search(rf"(?<!\d){number}(?=[A-Z])", raw[cursor:])
        if match is None:
            raise ValueError(f"missing requirement {number} in {path}")
        absolute = cursor + match.start()
        starts.append((number, absolute))
        cursor = cursor + match.end()
    rows: list[Requirement] = []
    for index, (number, start) in enumerate(starts):
        end = starts[index + 1][1] if index + 1 < len(starts) else len(raw)
        body = raw[start + len(str(number)):end].strip()
        title = _title_from_compact_body(body)
        rows.append(
            Requirement(
                requirement_id=f"{prefix}{number:03d}",
                title=title,
                text=body,
                required_evidence=_required_evidence_for_text(
                    f"{title} {body}",
                    f"{prefix}{number:03d}",
                ),
            )
        )
    return tuple(rows)


def parse_friday_ledger(path: Path) -> tuple[Requirement, ...]:
    row = re.compile(r"^\| (F\d{2}) \| ([^|]+) \| ([^|]+) \|$")
    requirements: list[Requirement] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = row.match(line)
        if match is None:
            continue
        requirement_id, title, text = (value.strip() for value in match.groups())
        requirements.append(
            Requirement(
                requirement_id,
                title,
                text,
                _required_evidence_for_text(f"{title} {text}", requirement_id),
            )
        )
    if len(requirements) != 65:
        raise ValueError(f"expected 65 Friday requirements, found {len(requirements)}")
    return tuple(requirements)


def _title_from_compact_body(body: str) -> str:
    heading = re.split(r"(?:What must be built|What ChatGPT must do|Required output|Hard rule|Purpose|Current status)", body, maxsplit=1)[0]
    heading = re.sub(r"\s+", " ", heading).strip(" :-")
    return heading[:160] or "Untitled requirement"


def _required_evidence_for_text(
    text: str,
    requirement_id: str = "",
) -> frozenset[EvidenceKind]:
    required = {EvidenceKind.CODE, EvidenceKind.TEST, EvidenceKind.RUNTIME}
    lowered = text.lower()
    if any(
        token in lowered
        for token in (
            "histor",
            "dataset",
            "outcome",
            "oos",
            "out-of-sample",
            "walk-forward",
            "hfm",
            "memory",
            "backtest",
            "market data",
        )
    ):
        required.add(EvidenceKind.DATA)
    if requirement_id in BROKER_EVIDENCE_REQUIREMENTS:
        required.add(EvidenceKind.BROKER)
    if "shadow" in lowered or "forward evidence" in lowered or "forward-test" in lowered:
        required.add(EvidenceKind.SHADOW)
    return frozenset(required)


class ComplianceLedger:
    def __init__(self, requirements: Iterable[Requirement]):
        rows = tuple(requirements)
        self._requirements = {row.requirement_id: row for row in rows}
        if len(self._requirements) != len(rows):
            raise ValueError("duplicate requirement id")
        self._evidence: dict[str, dict[EvidenceKind, EvidenceRecord]] = {}

    def add(self, evidence: EvidenceRecord) -> None:
        if evidence.requirement_id not in self._requirements:
            raise KeyError(evidence.requirement_id)
        bucket = self._evidence.setdefault(evidence.requirement_id, {})
        previous = bucket.get(evidence.kind)
        if previous is not None and previous.evidence_id != evidence.evidence_id:
            raise ValueError("one immutable evidence record per kind and requirement")
        bucket[evidence.kind] = evidence

    def result(self, requirement_id: str) -> RequirementResult:
        requirement = self._requirements[requirement_id]
        evidence = tuple(self._evidence.get(requirement_id, {}).values())
        observed = frozenset(row.kind for row in evidence)
        missing = requirement.required_evidence - observed
        status = RequirementStatus.PASS if not missing else RequirementStatus.NOT_VERIFIED
        return RequirementResult(requirement, status, evidence, missing)

    def results(self) -> tuple[RequirementResult, ...]:
        return tuple(self.result(key) for key in self._requirements)

    def counts(self) -> Mapping[RequirementStatus, int]:
        rows = self.results()
        return {status: sum(row.status is status for row in rows) for status in RequirementStatus}

    @property
    def complete(self) -> bool:
        rows = self.results()
        return bool(rows) and all(row.status is RequirementStatus.PASS for row in rows)


class MasterComplianceLedger(ComplianceLedger):
    """Fixed-scope ledger for certifying the complete three-document build.

    A normal ``ComplianceLedger`` is useful while validating one requirement.
    It is deliberately not a build certificate.  This class refuses any scope
    other than the complete 267-requirement governing set, so an Item 2 or
    Item 3 PASS cannot be misreported as end-to-end completion.
    """

    def __init__(self, requirements: Iterable[Requirement]):
        rows = tuple(requirements)
        _validate_master_scope(rows)
        super().__init__(rows)

    @classmethod
    def from_spec_directory(cls, spec_directory: Path) -> "MasterComplianceLedger":
        requirements = (
            *parse_numbered_document(spec_directory / "spec_component_matrix.md", "C", 160),
            *parse_numbered_document(spec_directory / "spec_edge_system_principles.md", "P", 42),
            *parse_friday_ledger(spec_directory / "FRIDAY_SCOPE_LEDGER.md"),
        )
        return cls(requirements)

    def document_results(self) -> tuple[DocumentResult, ...]:
        results = {row.requirement.requirement_id: row for row in self.results()}
        documents: list[DocumentResult] = []
        for document, (prefix, start, required, width) in GOVERNING_DOCUMENTS.items():
            rows = tuple(
                results[f"{prefix}{number:0{width}d}"]
                for number in range(start, start + required)
            )
            missing = tuple(
                row.requirement.requirement_id
                for row in rows
                if row.status is not RequirementStatus.PASS
            )
            documents.append(
                DocumentResult(
                    document=document,
                    status=RequirementStatus.PASS if not missing else RequirementStatus.NOT_VERIFIED,
                    passed=required - len(missing),
                    required=required,
                    missing_requirement_ids=missing,
                )
            )
        return tuple(documents)

    @property
    def complete(self) -> bool:
        rows = self.results()
        return (
            len(rows) == MASTER_REQUIREMENT_COUNT
            and all(row.status is RequirementStatus.PASS for row in rows)
            and all(row.status is RequirementStatus.PASS for row in self.document_results())
        )


def load_verified_evidence_manifest(
    manifest_path: Path,
    *,
    workspace_root: Path,
) -> tuple[EvidenceRecord, ...]:
    """Load content-addressed, requirement-specific evidence claims.

    The manifest is an index, not proof.  Each row must point to a JSON
    evidence envelope whose named claim repeats the requirement, evidence
    kind, result, and observation time.  The claim must also identify a
    content-addressed subject under the workspace.  This prevents a generic
    test trace or a source file from being relabelled as runtime, data, broker,
    or shadow proof merely by changing a manifest row.
    """

    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping) or raw.get("schema_version") != 2:
        raise ValueError("evidence manifest schema_version must be 2")
    values = raw.get("evidence")
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise ValueError("evidence manifest requires an evidence array")

    root = workspace_root.resolve()
    records: list[EvidenceRecord] = []
    observed_ids: set[str] = set()
    for value in values:
        if not isinstance(value, Mapping):
            raise ValueError("evidence rows must be objects")
        record = EvidenceRecord.from_mapping(value)
        if record.evidence_id in observed_ids:
            raise ValueError(f"duplicate evidence id: {record.evidence_id}")
        observed_ids.add(record.evidence_id)
        artifact_name, separator, claim_id = record.location.partition("#")
        if not separator or not claim_id:
            raise ValueError(
                "evidence location must select its immutable claim with #<evidence_id>"
            )
        if claim_id != record.evidence_id:
            raise ValueError(
                f"evidence location claim does not match record identity: {record.evidence_id}"
            )
        artifact = (root / artifact_name).resolve()
        try:
            artifact.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"evidence location escapes workspace: {record.location}") from exc
        if not artifact.is_file():
            raise ValueError(f"evidence artifact does not exist: {record.location}")
        expected = f"sha256:{sha256(artifact.read_bytes()).hexdigest()}"
        if record.digest != expected:
            raise ValueError(
                f"evidence digest mismatch for {record.evidence_id}: "
                f"expected {expected}, observed {record.digest}"
            )
        _validate_evidence_claim(record, artifact, root)
        records.append(record)
    return tuple(records)


def _validate_evidence_claim(
    record: EvidenceRecord,
    artifact: Path,
    workspace_root: Path,
) -> None:
    """Verify the artifact itself makes the exact claim in the manifest."""

    try:
        payload = json.loads(artifact.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"evidence artifact must be a JSON evidence envelope: {record.location}"
        ) from exc
    if not isinstance(payload, Mapping) or payload.get("schema_version") != 1:
        raise ValueError(f"evidence envelope schema_version must be 1: {record.location}")
    claims = payload.get("evidence_claims")
    if not isinstance(claims, Mapping):
        raise ValueError(f"evidence envelope has no evidence_claims: {record.location}")
    claim = claims.get(record.evidence_id)
    if not isinstance(claim, Mapping):
        raise ValueError(f"evidence claim is missing: {record.evidence_id}")
    expected_fields = {
        "evidence_id": record.evidence_id,
        "requirement_id": record.requirement_id,
        "kind": record.kind.value,
        "result": record.result,
        "observed_at": record.observed_at,
    }
    for field, expected in expected_fields.items():
        if claim.get(field) != expected:
            raise ValueError(
                f"evidence claim {field} mismatch for {record.evidence_id}: "
                f"expected {expected!r}, observed {claim.get(field)!r}"
            )

    subject = claim.get("subject")
    if not isinstance(subject, Mapping):
        raise ValueError(f"evidence claim has no content-addressed subject: {record.evidence_id}")
    subject_path_text = subject.get("path")
    subject_digest = subject.get("digest")
    if not isinstance(subject_path_text, str) or not isinstance(subject_digest, str):
        raise ValueError(f"evidence claim subject is incomplete: {record.evidence_id}")
    subject_path = (workspace_root / subject_path_text).resolve()
    try:
        subject_path.relative_to(workspace_root)
    except ValueError as exc:
        raise ValueError(f"evidence subject escapes workspace: {record.evidence_id}") from exc
    if not subject_path.is_file():
        raise ValueError(f"evidence subject does not exist: {record.evidence_id}")
    expected_subject_digest = f"sha256:{sha256(subject_path.read_bytes()).hexdigest()}"
    if subject_digest != expected_subject_digest:
        raise ValueError(
            f"evidence subject digest mismatch for {record.evidence_id}: "
            f"expected {expected_subject_digest}, observed {subject_digest}"
        )


def certify_evidence_manifest(
    *,
    spec_directory: Path,
    manifest_path: Path,
    workspace_root: Path,
) -> MasterComplianceLedger:
    """Build the fixed-scope ledger from verified external evidence only."""

    ledger = MasterComplianceLedger.from_spec_directory(spec_directory)
    for record in load_verified_evidence_manifest(
        manifest_path,
        workspace_root=workspace_root,
    ):
        ledger.add(record)
    return ledger


def _validate_master_scope(requirements: tuple[Requirement, ...]) -> None:
    expected = {
        f"{prefix}{number:0{width}d}"
        for prefix, start, count, width in GOVERNING_DOCUMENTS.values()
        for number in range(start, start + count)
    }
    observed = {row.requirement_id for row in requirements}
    if len(requirements) != MASTER_REQUIREMENT_COUNT or observed != expected:
        missing = sorted(expected - observed)
        unexpected = sorted(observed - expected)
        raise ValueError(
            "master compliance scope must contain all 267 governing requirements; "
            f"missing={missing[:10]} unexpected={unexpected[:10]}"
        )
