"""Per-symbol UTC hour/weekday entry blocks.

Deep-history backtest (2026-07-22, ~15 months / ~11,600 trades via the MQL5
deephistory export - see mt5_bridge/CipherFxBridge.mq5 ExportDeepHistoryOnce)
found these windows structurally negative and reproducible: avg_R <= -0.05
independently in BOTH halves of the sample, n>=10 in each half. Applying all
of them turned the full 14-symbol backtest total from -145.86R to +37.47R
with zero symbols made worse (XAUUSD/XAGUSD/US30 had no qualifying windows
and are untouched). Config: config/symbol_time_filters.json. Symbols absent
from the file (or the file missing/unparseable) trade unrestricted.

Supersedes the narrower UK100-only BLOCKED_UTC_HOURS special case that was
briefly deployed directly in indices.py on 2026-07-22 - this file's UK100
entry is the fuller validated set from the same methodology (adds hours
15,19,23 and weekdays Mon/Wed/Thu on top of the original 01:00-04:00).
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

_PATH = Path(
    os.getenv(
        "MT5_TIME_FILTERS_FILE",
        str(Path(__file__).resolve().parent.parent / "config" / "symbol_time_filters.json"),
    )
)
_cache: dict | None = None


def _load() -> dict:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(_PATH.read_text())
        except (OSError, json.JSONDecodeError):
            _cache = {}
    return _cache


def blocked(symbol: str, captured_at: datetime) -> bool:
    spec = _load().get(symbol)
    if not spec:
        return False
    if captured_at.hour in spec.get("hours", ()):
        return True
    if captured_at.weekday() in spec.get("weekdays", ()):
        return True
    return False
