"""Dataset manifest and causal sample utilities."""
from __future__ import annotations
import csv
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

def dataset_manifest(rows: Iterable[Mapping[str, Any]], *, source: str,
                     feature_version: str = "causal_features_v1") -> dict[str, Any]:
    data = [dict(row) for row in rows]
    timestamps = [str(row.get("timestamp", "")) for row in data if row.get("timestamp")]
    symbols = sorted({str(row.get("symbol", "")) for row in data if row.get("symbol")})
    payload = {"source": source, "feature_version": feature_version,
               "rows": len(data), "symbols": symbols,
               "first_timestamp": min(timestamps) if timestamps else "",
               "last_timestamp": max(timestamps) if timestamps else ""}
    payload["manifest_hash"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return payload

def write_csv(rows: Iterable[Mapping[str, Any]], output_path: str | Path) -> str:
    data = [dict(row) for row in rows]
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    keys = sorted({key for row in data for key in row})
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=keys)
        writer.writeheader()
        writer.writerows({key: row.get(key, "") for key in keys} for row in data)
    return str(path)

def write_manifest(manifest: Mapping[str, Any], output_path: str | Path) -> str:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(manifest), indent=2, sort_keys=True) + "\n")
    return str(path)
