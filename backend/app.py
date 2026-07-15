"""Read-only API application adapter.

The existing dashboard service remains the deployed HTTP application during
this refactor. Importing this module does not start uvicorn or the trading
runtime.
"""
from __future__ import annotations


def get_application():
    from dashboard.mt5_backend.main import app

    return app
