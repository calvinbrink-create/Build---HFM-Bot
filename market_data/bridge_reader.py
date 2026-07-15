"""Explicit bridge reader boundary for the trading process."""
from __future__ import annotations

from typing import Any, Protocol


class RatesGateway(Protocol):
    def connect(self) -> None: ...
    def rates(self, symbol: str, timeframe: str, count: int): ...
    def symbol_tick(self, symbol: str): ...


class BridgeReader:
    """Thin adapter; it performs I/O only when a caller invokes a method."""

    def __init__(self, gateway: RatesGateway):
        self._gateway = gateway

    def connect(self) -> None:
        self._gateway.connect()

    def rates(self, symbol: str, timeframe: str, count: int):
        return self._gateway.rates(symbol, timeframe, count)

    def tick(self, symbol: str) -> Any:
        _resolved, tick = self._gateway.symbol_tick(symbol)
        return tick
