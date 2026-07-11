# MT5 Strategy Core Refactor Plan

This is a future-work plan only. Do not refactor the live strategy core without parity tests and rollback.

## Current Coupling

`/opt/cipherfx_mt5/mt5_bot.py` currently imports these helpers from `/opt/cipherfx_mt5/scalping_bot_v4.py`:

- `CFG`
- `_compute_stop_distance`
- `_global_trading_enabled`
- `_gross_pnl_usd`
- `_rr_for_market`
- `_score_details_from_signal`
- `evaluate_probability`
- `execution_ok`
- `in_trade_window`
- optional `ACTIVE_PROBABILITY_STRATEGIES`
- optional `get_usdzar`

This keeps MT5 execution coupled to IBKR-derived strategy helper code. That is acceptable for now only because changing it would be a strategy-core refactor with trading-impact risk.

## Future Structure

Proposed split:

- `/opt/cipherfx_mt5/strategy_core/` for broker-neutral signal math, trade windows, scoring, RR, stop-distance, and score detail functions.
- `/opt/cipherfx_mt5/mt5_execution/` for MT5 gateway, sizing, bridge, order placement, reconciliation, and dashboard state.
- `/opt/scalp_v3/ibkr_execution/` for IBKR-specific execution, gateway, contracts, and IBKR state.

## Required Tests Before Switching Imports

- Output parity tests for `evaluate_probability`.
- Stop distance parity tests for `_compute_stop_distance`.
- RR parity tests for `_rr_for_market` and symbol overrides.
- Trade window parity tests for `in_trade_window`.
- Score detail parity tests for `_score_details_from_signal`.
- Full dry-run scan parity over the 15 MT5 priority symbols.
- Rollback plan that restores old imports and service units.

## Warning

Do not refactor live strategy core without parity tests and rollback. Safety guards, news gates, reporting, and isolation can be changed independently; strategy math should not be moved or altered in a live patch.
