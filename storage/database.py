"""Shared database path and connection helpers.

No migrations or writes happen during import or path resolution.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from config.paths import RuntimePaths


def database_path(paths: RuntimePaths | None = None) -> Path:
    return (paths or RuntimePaths.from_env()).database_file


def connect(paths: RuntimePaths | None = None, *, read_only: bool = False) -> sqlite3.Connection:
    path = database_path(paths)
    if read_only:
        uri = f"file:{path}?mode=ro"
        return sqlite3.connect(uri, uri=True)
    return sqlite3.connect(path)
