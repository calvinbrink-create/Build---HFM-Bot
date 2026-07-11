#!/usr/bin/env python3
from datetime import datetime, timedelta, timezone

from mt5_bot import XM_MT5_Bot
from mt5_xm_gateway import MT5PositionView


class DummyRuntime:
    def canonical_symbol(self, symbol: str) -> str:
        return str(symbol or "").upper()


class DummyGateway:
    def __init__(self, positions):
        self._positions = positions

    def positions(self):
        return list(self._positions)


def make_bot(positions):
    bot = XM_MT5_Bot.__new__(XM_MT5_Bot)
    bot.runtime = DummyRuntime()
    bot.gateway = DummyGateway(positions)
    bot.state = {"open_trades": {}}
    return bot


def make_position(age_seconds, profit, direction="BUY"):
    return MT5PositionView(
        ticket=1,
        symbol="NAS100",
        direction=direction,
        volume=0.1,
        price_open=20000.0,
        price_current=20010.0,
        sl=19900.0,
        tp=20200.0,
        profit=profit,
        time=datetime.now(timezone.utc) - timedelta(seconds=age_seconds),
    )


def main():
    assert make_bot([])._pyramiding_allows_entry("NAS100", "BUY") == (True, "OK")

    ok, reason = make_bot([make_position(120, 1.0)])._pyramiding_allows_entry("NAS100", "BUY")
    assert ok, reason

    ok, reason = make_bot([make_position(301, 1.0)])._pyramiding_allows_entry("NAS100", "BUY")
    assert not ok and "older" in reason, reason

    ok, reason = make_bot([make_position(120, 0.0)])._pyramiding_allows_entry("NAS100", "BUY")
    assert not ok and "not in profit" in reason, reason

    ok, reason = make_bot([make_position(120, 1.0, direction="SELL")])._pyramiding_allows_entry("NAS100", "BUY")
    assert not ok and "not BUY" in reason, reason

    print("mt5 pyramiding gate check passed")


if __name__ == "__main__":
    main()
