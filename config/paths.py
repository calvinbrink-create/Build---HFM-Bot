"""Authoritative filesystem paths shared by trading and read-only services."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


@dataclass(frozen=True)
class RuntimePaths:
    app_dir: Path
    state_file: Path
    database_file: Path
    bridge_dir: Path
    symbols_file: Path
    decision_audit_file: Path

    @classmethod
    def from_env(cls, app_dir: str | Path | None = None) -> "RuntimePaths":
        root = Path(app_dir or os.getenv("APP_DIR") or Path.cwd()).expanduser().resolve()

        def path_env(name: str, default: Path) -> Path:
            value = os.getenv(name)
            return Path(value).expanduser() if value else default

        return cls(
            app_dir=root,
            state_file=path_env("MT5_STATE_FILE", root / "state" / "mt5_runtime_state.json"),
            database_file=path_env("SCALPBOT_STATE_DB", root / "state" / "mt5_state.db"),
            bridge_dir=path_env(
                "MT5_BRIDGE_DIR",
                Path.home() / ".mt5" / "drive_c" / "Program Files" / "MetaTrader 5" / "MQL5" / "Files" / "cipherfx",
            ),
            symbols_file=path_env("MT5_SYMBOLS_FILE", root / "mt5_symbols.json"),
            decision_audit_file=path_env("MT5_DECISION_AUDIT_FILE", root / "state" / "mt5_decision_audit.jsonl"),
        )
