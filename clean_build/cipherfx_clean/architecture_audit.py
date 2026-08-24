"""Evidence-first architecture baseline for the complete clean-build scope.

Static reachability is inventory evidence only. It never marks a governing
requirement as passed and cannot substitute for runtime, data, shadow, or
broker evidence.
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .compliance import MasterComplianceLedger
from .traceability import requirement_bindings


FORBIDDEN_IMPORT_PREFIXES = (
    "cipherfx" + "_platform",
    "mt5" + "_xm_gateway",
    "mt5_bridge",
    "CipherFx" + "Bridge",
    "legacy",
    "old",
    "archive",
)


@dataclass(frozen=True)
class ModuleInventory:
    module: str
    path: str
    imports: tuple[str, ...]
    all_imports: tuple[str, ...]
    public_symbols: tuple[str, ...]
    has_cli_entrypoint: bool
    statically_reachable: bool
    sha256: str


def module_inventory(
    package_root: Path,
    *,
    declared_entrypoints: Sequence[str] = (),
) -> tuple[ModuleInventory, ...]:
    root = package_root.resolve()
    package = root.name
    parsed: dict[str, tuple[Path, ast.Module]] = {}
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        module = _module_name(root, path, package)
        parsed[module] = (path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
    all_imports = {
        module: _resolved_imports(
            tree,
            module,
            is_package=path.name == "__init__.py",
        )
        for module, (path, tree) in parsed.items()
    }
    imports = {
        module: tuple(
            dependency
            for dependency in all_imports[module]
            if dependency == package or dependency.startswith(f"{package}.")
        )
        for module in parsed
    }
    discovered_entrypoints = {
        module for module, (_, tree) in parsed.items() if _has_cli_entrypoint(tree)
    }
    entrypoints = set(declared_entrypoints) | discovered_entrypoints
    unknown = entrypoints - set(parsed)
    if unknown:
        raise ValueError(f"unknown declared entrypoints: {sorted(unknown)}")
    reachable = _reachable(imports, entrypoints)
    rows = []
    for module, (path, tree) in sorted(parsed.items()):
        content = path.read_bytes()
        rows.append(ModuleInventory(
            module=module,
            path=str(path.relative_to(root.parent)),
            imports=imports[module],
            all_imports=all_imports[module],
            public_symbols=_public_symbols(tree),
            has_cli_entrypoint=module in discovered_entrypoints,
            statically_reachable=module in reachable,
            sha256=sha256(content).hexdigest(),
        ))
    return tuple(rows)


def build_baseline(
    *,
    spec_directory: Path,
    package_root: Path,
    declared_entrypoints: Sequence[str] = (),
) -> Mapping[str, object]:
    ledger = MasterComplianceLedger.from_spec_directory(spec_directory)
    bindings = requirement_bindings()
    inventory = module_inventory(
        package_root,
        declared_entrypoints=declared_entrypoints,
    )
    forbidden = tuple(
        {
            "module": row.module,
            "import": dependency,
        }
        for row in inventory
        for dependency in row.all_imports
        if dependency.startswith(FORBIDDEN_IMPORT_PREFIXES)
    )
    inventory_by_module = {row.module: row for row in inventory}
    requirements = []
    for row in ledger.results():
        binding = bindings[row.requirement.requirement_id]
        missing_modules = tuple(module for module in binding.modules if module not in inventory_by_module)
        reachable_modules = tuple(
            module for module in binding.modules
            if module in inventory_by_module and inventory_by_module[module].statically_reachable
        )
        if missing_modules:
            binding_status = "MAPPED_MISSING_MODULE"
        elif len(reachable_modules) == len(binding.modules):
            binding_status = "MAPPED_STATIC_REACHABLE"
        elif reachable_modules:
            binding_status = "MAPPED_PARTIAL_REACHABILITY"
        else:
            binding_status = "MAPPED_DISCONNECTED"
        requirements.append({
            "requirement_id": row.requirement.requirement_id,
            "title": row.requirement.title,
            "status": row.status.value,
            "required_evidence": tuple(sorted(kind.value for kind in row.requirement.required_evidence)),
            "observed_evidence": tuple(sorted(item.kind.value for item in row.evidence)),
            "missing_evidence": tuple(sorted(kind.value for kind in row.missing)),
            "binding": {
                "modules": binding.modules,
                "stages": binding.stages,
                "missing_modules": missing_modules,
                "reachable_modules": reachable_modules,
                "status": binding_status,
                "is_completion_evidence": False,
            },
        })
    requirements = tuple(requirements)
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
    reachable = tuple(row.module for row in inventory if row.statically_reachable)
    disconnected = tuple(row.module for row in inventory if not row.statically_reachable)
    return {
        "schema_version": 1,
        "scope": {
            "requirement_count": len(requirements),
            "documents": documents,
            "complete": ledger.complete,
        },
        "requirements": requirements,
        "static_architecture": {
            "module_count": len(inventory),
            "reachable_count": len(reachable),
            "disconnected_count": len(disconnected),
            "reachable_modules": reachable,
            "disconnected_modules": disconnected,
            "modules": tuple(asdict(row) for row in inventory),
            "forbidden_imports": forbidden,
            "static_reachability_is_completion_evidence": False,
        },
        "status": "NOT_VERIFIED",
        "reason": "NO_REQUIREMENT_HAS_ALL_REQUIRED_EVIDENCE_KINDS",
    }


def write_baseline(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _module_name(root: Path, path: Path, package: str) -> str:
    relative = path.relative_to(root)
    if relative.name == "__init__.py":
        parts = relative.parent.parts
    else:
        parts = relative.with_suffix("").parts
    return ".".join((package, *parts)) if parts else package


def _resolved_imports(
    tree: ast.Module,
    current_module: str,
    *,
    is_package: bool = False,
) -> tuple[str, ...]:
    rows: set[str] = set()
    current_parts = current_module.split(".")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            rows.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = current_parts if is_package else current_parts[:-1]
                keep = len(base) - node.level + 1
                prefix = base[:max(1, keep)]
                target = ".".join((*prefix, *(node.module or "").split("."))).rstrip(".")
            else:
                target = node.module or ""
            if target:
                rows.add(target)
    return tuple(sorted(rows))


def _has_cli_entrypoint(tree: ast.Module) -> bool:
    for node in tree.body:
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not isinstance(test, ast.Compare) or len(test.ops) != 1:
            continue
        if not isinstance(test.left, ast.Name) or test.left.id != "__name__":
            continue
        if any(isinstance(value, ast.Constant) and value.value == "__main__" for value in test.comparators):
            return True
    return False


def _public_symbols(tree: ast.Module) -> tuple[str, ...]:
    return tuple(sorted(
        node.name
        for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and not node.name.startswith("_")
    ))


def _reachable(graph: Mapping[str, Iterable[str]], entrypoints: Iterable[str]) -> frozenset[str]:
    seen: set[str] = set()
    stack = list(entrypoints)
    while stack:
        module = stack.pop()
        if module in seen:
            continue
        seen.add(module)
        stack.extend(dependency for dependency in graph.get(module, ()) if dependency in graph)
    for module in tuple(seen):
        parts = module.split(".")
        seen.update(".".join(parts[:index]) for index in range(1, len(parts)))
    return frozenset(seen)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec-directory", type=Path, required=True)
    parser.add_argument("--package-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--entrypoint", action="append", default=[])
    args = parser.parse_args()
    payload = build_baseline(
        spec_directory=args.spec_directory,
        package_root=args.package_root,
        declared_entrypoints=tuple(args.entrypoint),
    )
    write_baseline(args.output, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
