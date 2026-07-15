"""Single trading-process entry point.

The legacy implementation is loaded lazily only when this process is actually
started. Importing the module cannot connect to MT5, start a worker, or submit
an order.
"""
from __future__ import annotations

import argparse
from typing import Sequence

from config.settings import ServiceSettings, load_settings


class TradingRuntime:
    """Compatibility-preserving owner of the single live trading process."""

    def __init__(self, settings: ServiceSettings):
        self.settings = settings

    def run(self) -> int:
        # Lazy import is deliberate: API/frontend/intelligence imports cannot
        # accidentally load the live runtime or start a worker.
        from mt5_bot import XM_MT5_Bot

        result = XM_MT5_Bot(self.settings.runtime).run()
        return int(result) if isinstance(result, int) else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CipherFX MT5 trading service")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    settings = load_settings()
    if args.dry_run:
        settings = settings.with_dry_run(True)
    return TradingRuntime(settings).run()
