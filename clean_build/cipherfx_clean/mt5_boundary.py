"""Deployment contract for the existing MT5 bot; no MT5 client is imported."""

from __future__ import annotations

from typing import Protocol

from .execution import BotExecutionResponse, BotInstruction


class MT5ExecutionPort(Protocol):
    """Implemented by deployment code that owns all MT5 order operations."""

    def submit(self, instruction: BotInstruction) -> BotExecutionResponse:
        ...
