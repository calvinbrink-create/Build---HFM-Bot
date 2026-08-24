"""Explicit clean-build configuration with no hidden defaults or legacy reads."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class RuntimeSettings:
    environment: str
    symbols: tuple[str, ...]
    timeframes: tuple[str, ...]
    execution_enabled: bool


def load_settings(values: Mapping[str, object]) -> RuntimeSettings:
    required = ("environment", "symbols", "timeframes", "execution_enabled")
    missing = tuple(key for key in required if key not in values)
    if missing:
        raise ValueError(f"missing explicit settings: {','.join(missing)}")
    symbols = tuple(str(item) for item in values["symbols"])
    timeframes = tuple(str(item) for item in values["timeframes"])
    if not symbols or not timeframes:
        raise ValueError("symbols and timeframes must be explicit and non-empty")
    return RuntimeSettings(str(values["environment"]), symbols, timeframes, bool(values["execution_enabled"]))
