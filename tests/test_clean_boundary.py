from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_entrypoint_uses_only_modular_runtime():
    source = (ROOT / "run_mt5_bot.py").read_text(encoding="utf-8")
    assert "cipherfx_platform.runtime" in source
    assert "runpy" not in source
    assert "mt5_bot" not in source


def test_runtime_import_does_not_load_retired_modules():
    code = (
        "import sys; import cipherfx_platform.runtime; "
        "assert not any(name in sys.modules for name in "
        "('mt5_bot', 'strategy_architecture_v1', 'trading.runtime'))"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, check=False)
    assert result.returncode == 0


def test_active_graph_has_one_proposal_and_execution_authority():
    runtime_imports = _imports(ROOT / "cipherfx_platform" / "runtime.py")
    router_imports = _imports(ROOT / "cipherfx_platform" / "engines" / "router.py")
    assert "execution" in runtime_imports
    assert "engines" in runtime_imports
    assert "strategy_dispatch" in router_imports
    assert (ROOT / "cipherfx_platform" / "execution.py").exists()
    assert (ROOT / "cipherfx_platform" / "engines" / "router.py").exists()


def test_dashboard_backend_has_no_order_transport():
    source = (ROOT / "dashboard" / "mt5_backend" / "main.py").read_text(encoding="utf-8")
    assert "read-only backend: trading runtime owns broker execution" in source
    assert "HTTPException(status_code=403" in source


def test_retired_architecture_files_are_absent():
    for name in (
        "mt5_bot.py",
        "strategy_architecture_v1.py",
        "strategy_live_shadow_v1.py",
        "scalping_bot_v4.py",
    ):
        assert not (ROOT / name).exists()


def test_asset_engines_have_no_retired_hidden_veto_paths():
    for name in ("forex.py", "indices.py", "metals.py"):
        source = (ROOT / "cipherfx_platform" / "engines" / name).read_text(encoding="utf-8")
        assert "MT5_BLOCKED_HOURS_UTC" not in source
        assert "MT5_DIRECTION_MODE" not in source
    assert '"direction_source":"H4_DIRECTION"' in (ROOT / "cipherfx_platform" / "engines" / "indices.py").read_text(encoding="utf-8")
    assert 'else m15["direction"]' not in (ROOT / "cipherfx_platform" / "engines" / "forex.py").read_text(encoding="utf-8")


def test_runtime_has_no_backtest_data_or_pattern_memory_artifacts():
    assert not (ROOT / "config" / "market_memory.json").exists()
    assert not (ROOT / "config" / "pattern_memory.npz").exists()
    assert not (ROOT / "audit_artifacts").exists()
