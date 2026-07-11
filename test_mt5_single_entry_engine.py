#!/usr/bin/env python3
from pathlib import Path


def main():
    source = Path(__file__).with_name("mt5_bot.py").read_text()
    assert "self._manage_profit_pyramiding(live)" not in source
    assert "fallback_sig = self._priority_fallback_signal" not in source
    assert "def _manage_open_position(" in source
    assert "def _manage_profit_pyramiding(" not in source
    assert "def _priority_fallback_signal(" not in source
    assert "import ta" not in source
    assert "sig = evaluate_probability(df, sym=sym, market=market)" in source
    pending_idx = source.index("pending_ok, pending_reason = self._pending_setup_allows_execution")
    place_idx = source.index("self._place_trade(sym, market, sig, volume, sl, tp, risk_meta)")
    assert pending_idx < place_idx, "pending setup gate must be before order placement"
    assert "MT5_ALLOW_DIRECT_5M_ENTRY" in source
    print("mt5 single entry engine check passed")


if __name__ == "__main__":
    main()
