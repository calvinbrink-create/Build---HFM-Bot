#!/usr/bin/env python3
from mt5_bot import XM_MT5_Bot


def make_bot():
    return XM_MT5_Bot.__new__(XM_MT5_Bot)


def main():
    bot = make_bot()

    for strategy in ("MOMENTUM", "PULLBACK", "MEAN_REV", "BB_SQUEEZE"):
        ok, reason = bot._strategy_allows_entry({"strategy": strategy})
        assert ok, f"{strategy} should be allowed: {reason}"

    for strategy in ("PRIORITY_TREND", "PRIORITY_PULLBACK", "PRIORITY_REVERSION", "VWAP_REV", "standard", "ema50"):
        ok, reason = bot._strategy_allows_entry({"strategy": strategy})
        assert not ok and "not an active live strategy" in reason, f"{strategy} should be blocked: {reason}"

    ok, reason = bot._strategy_allows_entry({})
    assert not ok and "missing strategy" in reason, reason

    print("mt5 strategy guard check passed")


if __name__ == "__main__":
    main()
