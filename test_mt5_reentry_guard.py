#!/usr/bin/env python3
from datetime import datetime, timedelta
from unittest.mock import patch

from mt5_bot import XM_MT5_Bot


class DummyRuntime:
    def canonical_symbol(self, symbol: str) -> str:
        return str(symbol or "").upper()


def make_bot(rows):
    bot = XM_MT5_Bot.__new__(XM_MT5_Bot)
    bot.runtime = DummyRuntime()
    bot.priority_symbols = {"XAUUSD"}
    bot.symbol_profiles = {}
    bot.state = {"last_symbol_closes": {}}
    bot._today_closed_trades = lambda limit=250: list(rows)
    return bot


def closed_trade(outcome, minutes_ago, realized, sym="XAUUSD"):
    return {
        "sym": sym,
        "market": "metal",
        "outcome": outcome,
        "realized": realized,
        "closed_at": (datetime.utcnow() - timedelta(minutes=minutes_ago)).isoformat(),
    }


def main():
    env = {
        "MT5_LOSS_COUNT_MIN_USD": "5",
        "MT5_LOSS_COUNT_MIN_USD_XAUUSD": "5",
        "MT5_PRIORITY_MAX_SYMBOL_LOSSES_PER_DAY": "1",
        "MT5_PRIORITY_SYMBOL_COOLDOWN_MIN": "90",
    }
    with patch.dict("os.environ", env, clear=False), patch("mt5_bot._ss.set_status"):
        bot = make_bot([closed_trade("loss", 120, -4.02)])
        ok, reason = bot._symbol_risk_allows_entry("XAUUSD")
        assert not ok and "daily loss limit" in reason, reason
        assert bot._daily_loss_streak() == 1

        bot = make_bot([closed_trade("win", 1, 1.41)])
        ok, reason = bot._symbol_risk_allows_entry("XAUUSD")
        assert not ok and "fresh M15 setup required" in reason, reason

        bot = make_bot([closed_trade("win", 16, 1.41)])
        assert bot._symbol_risk_allows_entry("XAUUSD") == (True, "OK")

        bot = make_bot([closed_trade("win", 1, 25.0, sym="GBPUSD")])
        assert bot._symbol_risk_allows_entry("XAUUSD") == (True, "OK")

    print("mt5 re-entry guard check passed")


if __name__ == "__main__":
    main()
