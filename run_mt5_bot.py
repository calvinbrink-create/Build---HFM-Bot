#!/usr/bin/env python3
"""Small production entry point for the single trading process."""
from __future__ import annotations

import warnings
from typing import Sequence

warnings.filterwarnings("ignore", category=DeprecationWarning)

from trading.runtime import main


if __name__ == "__main__":
    raise SystemExit(main())
