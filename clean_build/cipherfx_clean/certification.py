"""CLI for requirement-by-requirement clean-build certification."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Mapping

from .compliance import MasterComplianceLedger, certify_evidence_manifest


def certification_report(ledger: MasterComplianceLedger) -> Mapping[str, object]:
    requirements = tuple(
        {
            "requirement_id": row.requirement.requirement_id,
            "title": row.requirement.title,
            "status": row.status.value,
            "required_evidence": tuple(sorted(kind.value for kind in row.requirement.required_evidence)),
            "observed_evidence": tuple(sorted(item.kind.value for item in row.evidence)),
            "missing_evidence": tuple(sorted(kind.value for kind in row.missing)),
            "evidence_ids": tuple(sorted(item.evidence_id for item in row.evidence)),
        }
        for row in ledger.results()
    )
    documents = tuple(
        {
            "document": row.document,
            "status": row.status.value,
            "passed": row.passed,
            "required": row.required,
            "missing_requirement_ids": row.missing_requirement_ids,
        }
        for row in ledger.document_results()
    )
    return {
        "schema_version": 1,
        "complete": ledger.complete,
        "documents": documents,
        "requirements": requirements,
        "status": "PASS" if ledger.complete else "NOT_VERIFIED",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec-directory", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        ledger = certify_evidence_manifest(
            spec_directory=args.spec_directory,
            manifest_path=args.manifest,
            workspace_root=args.workspace_root,
        )
    except (KeyError, OSError, ValueError) as exc:
        payload = {
            "schema_version": 1,
            "complete": False,
            "documents": (),
            "requirements": (),
            "status": "NOT_VERIFIED",
            "reason": f"EVIDENCE_MANIFEST_REJECTED:{type(exc).__name__}:{exc}",
        }
        exit_code = 2
    else:
        payload = certification_report(ledger)
        exit_code = 0 if ledger.complete else 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
