#!/usr/bin/env python3
"""Authoritative process entry point for the modular CipherFX platform."""
from __future__ import annotations

import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning)

from cipherfx_platform.runtime import main


if __name__ == "__main__":
    raise SystemExit(main())
