"""Reproducible research certification and independently verified bundles."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import Mapping, Sequence

from .store import EvidenceStore


@dataclass(frozen=True)
class CertificationInput:
    edge_id: str
    edge_version: int
    dataset_ids: tuple[str, ...]
    dataset_quality_status: str
    validation_id: str
    validation_status: str
    path_fidelity: str
    shadow_evidence_id: str | None
    shadow_status: str
    shadow_samples: int
    minimum_shadow_samples: int
    cost_evidence_id: str | None
    cost_evidence_status: str


@dataclass(frozen=True)
class EdgeCertification:
    certification_id: str
    edge_id: str
    edge_version: int
    status: str
    reasons: tuple[str, ...]
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class ReleaseArtifact:
    role: str
    path: str
    digest: str
    size: int


@dataclass(frozen=True)
class ReleaseBundle:
    release_id: str
    created_at_utc: str
    status: str
    artifacts: tuple[ReleaseArtifact, ...]
    certification_ids: tuple[str, ...]


@dataclass(frozen=True)
class ReleaseVerification:
    release_id: str
    status: str
    reasons: tuple[str, ...]
    checked_artifacts: int


def certify_edge(item: CertificationInput) -> EdgeCertification:
    reasons = []
    if not item.dataset_ids:
        reasons.append("MISSING_DATASET_IDENTITY")
    if item.dataset_quality_status != "PASS":
        reasons.append("DATASET_QUALITY_NOT_PASS")
    if not item.validation_id or item.validation_status != "PASS":
        reasons.append("HISTORICAL_VALIDATION_NOT_PASS")
    if item.path_fidelity != "HFM_EXECUTABLE_TICKS":
        reasons.append("EXECUTABLE_TICK_EVIDENCE_MISSING")
    if not item.cost_evidence_id or item.cost_evidence_status != "COMPLETE":
        reasons.append("MEASURED_COST_EVIDENCE_NOT_COMPLETE")
    if not item.shadow_evidence_id or item.shadow_status != "PASS":
        reasons.append("SHADOW_FORWARD_EVIDENCE_NOT_PASS")
    if item.shadow_samples < item.minimum_shadow_samples:
        reasons.append("INSUFFICIENT_SHADOW_SAMPLE")
    payload = asdict(item)
    certification_id = _digest(payload)
    evidence = tuple(
        value
        for value in (
            *item.dataset_ids,
            item.validation_id,
            item.cost_evidence_id,
            item.shadow_evidence_id,
        )
        if value
    )
    return EdgeCertification(
        certification_id,
        item.edge_id,
        item.edge_version,
        "PASS" if not reasons else "BLOCKED",
        tuple(reasons),
        evidence,
    )


def build_release_bundle(
    *,
    root: Path,
    artifacts: Mapping[str, Path],
    certifications: Sequence[EdgeCertification],
    output: Path,
    store: EvidenceStore | None = None,
    created_at: datetime | None = None,
) -> ReleaseBundle:
    if not artifacts:
        raise ValueError("release requires artifacts")
    root = root.resolve()
    rows = []
    for role, path in sorted(artifacts.items()):
        resolved = path.resolve()
        if not resolved.is_file():
            raise FileNotFoundError(resolved)
        try:
            relative = resolved.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"artifact is outside release root: {resolved}") from exc
        data = resolved.read_bytes()
        rows.append(ReleaseArtifact(role, str(relative), sha256(data).hexdigest(), len(data)))
    status = "READY" if certifications and all(item.status == "PASS" for item in certifications) else "BLOCKED"
    created = (created_at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    identity_payload = {
        "artifacts": [asdict(item) for item in rows],
        "certification_ids": [item.certification_id for item in certifications],
        "status": status,
    }
    release_id = _digest(identity_payload)
    bundle = ReleaseBundle(
        release_id,
        created,
        status,
        tuple(rows),
        tuple(item.certification_id for item in certifications),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(asdict(bundle), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if store is not None:
        store.write_release_artifact(release_id, "RELEASE_MANIFEST", _file_digest(output), bundle)
        for item in rows:
            store.write_release_artifact(
                _digest((release_id, item.role, item.path)), item.role, item.digest, item
            )
    return bundle


def verify_release_bundle(root: Path, manifest: Path) -> ReleaseVerification:
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    reasons = []
    artifacts = tuple(ReleaseArtifact(**item) for item in raw.get("artifacts", ()))
    for item in artifacts:
        path = (root / item.path).resolve()
        try:
            path.relative_to(root.resolve())
        except ValueError:
            reasons.append(f"ARTIFACT_OUTSIDE_ROOT:{item.path}")
            continue
        if not path.is_file():
            reasons.append(f"MISSING_ARTIFACT:{item.path}")
            continue
        if path.stat().st_size != item.size:
            reasons.append(f"SIZE_MISMATCH:{item.path}")
        if _file_digest(path) != item.digest:
            reasons.append(f"DIGEST_MISMATCH:{item.path}")
    identity_payload = {
        "artifacts": [asdict(item) for item in artifacts],
        "certification_ids": raw.get("certification_ids", []),
        "status": raw.get("status"),
    }
    expected_id = _digest(identity_payload)
    if expected_id != raw.get("release_id"):
        reasons.append("RELEASE_ID_MISMATCH")
    if raw.get("status") != "READY":
        reasons.append("RELEASE_NOT_CERTIFIED_READY")
    return ReleaseVerification(
        str(raw.get("release_id", "")),
        "PASS" if not reasons else "FAIL",
        tuple(reasons),
        len(artifacts),
    )


def _file_digest(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _digest(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()
