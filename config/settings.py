"""Validated service settings without connecting to MT5 at import time."""
from __future__ import annotations

from dataclasses import dataclass, replace

from mt5_xm_config import MT5RuntimeConfig

from .paths import RuntimePaths


@dataclass(frozen=True)
class ServiceSettings:
    runtime: MT5RuntimeConfig
    paths: RuntimePaths

    def with_dry_run(self, enabled: bool) -> "ServiceSettings":
        return replace(self, runtime=replace(self.runtime, dry_run=bool(enabled)))


def load_settings() -> ServiceSettings:
    """Load the existing runtime configuration without opening a bridge or database."""
    return ServiceSettings(runtime=MT5RuntimeConfig(), paths=RuntimePaths.from_env())
