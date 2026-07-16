"""Retired runtime entry module.

The production service imports cipherfx_platform.runtime directly. This module
is intentionally kept import-safe for non-production tooling and has no legacy
trading route.
"""
from cipherfx_platform.runtime import ModularTradingRuntime, main

__all__ = ["ModularTradingRuntime", "main"]
