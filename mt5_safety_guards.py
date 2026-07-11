from __future__ import annotations

import os
from pathlib import Path

EXPECTED_BACKEND = "mt5"
EXPECTED_STATE_DB = "/opt/cipherfx_mt5/state/mt5_state.db"
DEFAULT_DISABLED_FLAG = "/opt/cipherfx_mt5/state/trading_disabled.flag"
_TRUE_VALUES = {"1", "true", "yes", "on"}


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in _TRUE_VALUES


def trading_disabled_flag() -> Path:
    return Path(os.getenv("MT5_TRADING_DISABLED_FLAG", DEFAULT_DISABLED_FLAG))


def _state_db_reason() -> str:
    backend = os.getenv("CIPHERFX_BROKER_BACKEND", "").strip().lower()
    db_path = os.getenv("SCALPBOT_STATE_DB", "").strip()
    if backend != EXPECTED_BACKEND:
        return f"CIPHERFX_BROKER_BACKEND must be {EXPECTED_BACKEND}"
    if not db_path:
        return "SCALPBOT_STATE_DB is required"
    if db_path != EXPECTED_STATE_DB:
        return f"SCALPBOT_STATE_DB must be {EXPECTED_STATE_DB}"
    if "/opt/scalp_v3" in db_path:
        return "SCALPBOT_STATE_DB must not point into /opt/scalp_v3"
    if Path(db_path).name == "bot_state.db":
        return "SCALPBOT_STATE_DB must not use bot_state.db for MT5"
    return ""


def new_entries_allowed() -> tuple[bool, str]:
    if _truthy(os.getenv("MT5_TRADING_DISABLED")):
        return False, "new entries disabled by MT5_TRADING_DISABLED"
    flag = trading_disabled_flag()
    if flag.exists():
        try:
            detail = flag.read_text(errors="ignore").strip().splitlines()[0]
        except Exception:
            detail = ""
        suffix = f": {detail}" if detail else ""
        return False, f"new entries disabled by flag {flag}{suffix}"
    reason = _state_db_reason()
    if reason:
        return False, reason
    return True, "OK"
