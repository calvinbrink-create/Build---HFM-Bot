import json
import os
import sys
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).parents[1].resolve()
PACKAGE = (ROOT / "cipherfx_clean").resolve()
sys.path.insert(0, str(ROOT))

_EXECUTED: dict[str, set[str]] = {}
_RESULTS = {"passed": 0, "failed": 0, "skipped": 0}


def _trace(frame, event, _argument):
    if event != "call":
        return
    path = Path(frame.f_code.co_filename).resolve()
    try:
        relative = path.relative_to(PACKAGE)
    except ValueError:
        return
    module = ".".join(("cipherfx_clean", *relative.with_suffix("").parts))
    if module.endswith(".__init__"):
        module = module.removesuffix(".__init__")
    _EXECUTED.setdefault(module, set()).add(frame.f_code.co_name)


def pytest_sessionstart(session):
    del session
    if os.getenv("CIPHERFX_TEST_TRACE_OUTPUT"):
        sys.setprofile(_trace)


def pytest_runtest_logreport(report):
    if report.when != "call":
        return
    if report.passed:
        _RESULTS["passed"] += 1
    elif report.failed:
        _RESULTS["failed"] += 1
    elif report.skipped:
        _RESULTS["skipped"] += 1


def pytest_sessionfinish(session, exitstatus):
    output = os.getenv("CIPHERFX_TEST_TRACE_OUTPUT")
    if not output:
        return
    sys.setprofile(None)
    files = []
    for path in sorted(PACKAGE.rglob("*.py")):
        data = path.read_bytes()
        relative = path.relative_to(ROOT)
        module = ".".join(relative.with_suffix("").parts)
        if module.endswith(".__init__"):
            module = module.removesuffix(".__init__")
        files.append({
            "module": module,
            "path": str(relative),
            "sha256": sha256(data).hexdigest(),
        })
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "exit_status": int(exitstatus),
        "collected": int(session.testscollected),
        "results": dict(_RESULTS),
        "executed_modules": {
            module: sorted(functions)
            for module, functions in sorted(_EXECUTED.items())
        },
        "code_manifest": files,
    }
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
