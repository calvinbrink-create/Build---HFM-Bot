# VPS MT5 Ghost Trade Audit - 2026-07-08

VPS: `root@167.233.36.200` (`prod-app-1`)  
Live deployment: `/opt/cipherfx_mt5`  
Backup before fix: `/opt/cipherfx_mt5/backups/codex_ghost_trade_guard_20260708_182431`

## A. Trade Entry Map

Final user-directed risk caps after correction:

- `MT5_MAX_DAILY_TRADES=60`
- `MT5_MAX_DAILY_LOSS_USD=1000`
- `MT5_DAILY_LOSS_OVERRIDE_USD=1000`

Important: the ghost-trade fix is the execution gate / duplicate prevention / strategy guard work. It does not depend on lowering the demo account to 30 trades or a $250 daily loss cap.

- `/opt/cipherfx_mt5/mt5_bot.py`
  - `XM_MT5_Bot._scan_priority_symbols()` current line 1362: priority scan loop.
  - `XM_MT5_Bot._scan_and_trade()` current line 1423: full scan loop.
  - `XM_MT5_Bot._scan_symbol()` current line 2166: builds signal, risk, SL/TP/volume, now calls the execution gate.
  - `XM_MT5_Bot._execution_gate_allows_entry()` current line 1853: single official scanner execution gate added in this fix.
  - `XM_MT5_Bot._place_trade()` current line 2506: only Python scanner order-send function; now kill-switches without a gate ID.

- `/opt/cipherfx_mt5/mt5_xm_gateway.py`
  - `place_market_order()` line 307: writes bridge `OPEN` command or native `TRADE_ACTION_DEAL`.
  - `place_pending_order()` line 357: writes bridge/native pending order.
  - `close_position()` line 418, `modify_position()` line 454, `cancel_order()` line 484.
  - `_send_native_order()` line 655: native fill-mode retry path.

- `/opt/cipherfx_mt5/dashboard/mt5_backend/main.py`
  - `place_market_order()` current line 1024 and `place_pending_order()` current line 1054 were independent authenticated manual order paths. They now require `_require_manual_trading_enabled()` at line 406.

- `/opt/cipherfx_mt5/mt5_bridge/CipherFxBridge.mq5`
  - `ExecuteOpen()` line 837 and `ExecutePending()` line 973 send MT5 orders from bridge command files.
  - `CanOpenNewTrade()` line 308 is the EA-side risk/pyramid gate.
  - `NormalizeLotSize()` line 263 previously could inflate approved volume; now rejects invalid/too-small volume instead.

- `/opt/cipherfx_mt5/scalping_bot_v4.py`
  - `Orders.bracket()` line 2789 and `Orders.manage_pyramid()` line 3087 are legacy IBKR paths, not the active MT5 service path. They still need the separate strategy-quality audit noted by the user.

## B. Execution Flow

Current fixed scanner flow:

`_scan_priority_symbols()` / `_scan_and_trade()` -> `_scan_symbol()` -> `evaluate_probability()` -> `execution_ok()` -> symbol risk -> strategy/quality/spread gates -> SL/TP/volume sizing -> `_execution_gate_allows_entry()` -> `_place_trade()` -> `MT5Gateway.place_market_order()` -> bridge `OPEN` command -> `CipherFxBridge.ExecuteOpen()` -> MT5 confirmation -> DB trade row.

The official scanner path is now `_execution_gate_allows_entry()` before `_place_trade()`. `_place_trade()` refuses execution when `MT5_KILL_ON_UNEXPECTED_TRADE_PATH=1` and the signal lacks `_execution_gate_id`.

## C. Bug Findings

1. Unsafe VPS runtime limits
   - File: `/etc/scalpbot/scalpbot-mt5.env`
   - Function: n/a
   - Evidence: pre-fix VPS env had `MT5_MAX_OPEN_TRADES=30`, `MT5_MAX_DAILY_TRADES=60`, `MT5_MAX_DAILY_LOSS_USD=1000`, `MT5_MAX_DAILY_LOSING_STREAK=10`.
   - Impact: allowed many simultaneous positions and repeated same-symbol entries. User confirmed the demo account daily cap should remain 60 trades and $1000 daily loss.
   - Example: DB showed `daily_trade_count=37`; XAUUSD alone had 23 closed trades today.
   - Fix: kept user-requested demo caps at max daily `60` and daily loss `$1000`; tightened max open, losing streak, per-symbol/day caps, and duplicate/re-entry gates.

2. Missing single execution gate before MT5 send
   - File: `/opt/cipherfx_mt5/mt5_bot.py`
   - Function: old `_scan_symbol()` / `_place_trade()`; current fix at `_execution_gate_allows_entry()` line 1853 and `_place_trade()` line 2506.
   - What code did: pre-fix `_scan_symbol()` wrote `"OK"` and called `_place_trade()` directly after quality/spread/size.
   - Why bad trades opened: there was no final centralized check for strategy allowlist, stale signal, duplicate live position, pending order, symbol lock, per-symbol daily count, or magic validation.
   - Example: XAUUSD trade closed at `2026-07-08T17:23:32` with +1.16, then another XAUUSD opened at `2026-07-08T17:23:49` and lost -4.08.
   - Fix: added execution gate, symbol lock, signal expiry, duplicate prevention, magic validation, audit log, and `_place_trade()` kill switch.

3. Priority strategies were live despite tests expecting them blocked
   - File: `/opt/cipherfx_mt5/mt5_bot.py`
   - Function: `_quality_allows_entry()` current line 1887; `_strategy_allows_entry()` current line 1644.
   - What code did: pre-fix quality checks allowed `PRIORITY_TREND`, `PRIORITY_PULLBACK`, and `PRIORITY_REVERSION`.
   - Why bad trades opened: today’s XAUUSD churn was all priority strategy labels, while `test_mt5_strategy_guard.py` expected those labels blocked as inactive live strategies.
   - Example: XAUUSD had 23 trades: `PRIORITY_TREND`, `PRIORITY_PULLBACK`, `PRIORITY_REVERSION`.
   - Fix: `_strategy_allows_entry()` allows only `MOMENTUM`, `PULLBACK`, `MEAN_REV`, `BB_SQUEEZE` unless code is explicitly changed.

4. Dashboard manual order bypass
   - File: `/opt/cipherfx_mt5/dashboard/mt5_backend/main.py`
   - Function: `place_market_order()` current line 1024, `place_pending_order()` current line 1054.
   - What code did: authenticated dashboard endpoints called `gateway.place_market_order()` / `place_pending_order()` directly.
   - Why bad trades could open: no scanner signal, no daily loss/count gate, no spread gate, no symbol lock.
   - Example scenario: a POST to `/api/orders/market` would create an MT5 bridge `OPEN` command without approved scanner signal.
   - Fix: manual order opens now require `MT5_DASHBOARD_MANUAL_TRADING_ENABLED=1`; VPS env sets it to `0`.

5. Bridge-side pyramiding and lot mutation
   - File: `/opt/cipherfx_mt5/mt5_bridge/CipherFxBridge.mq5`
   - Function: `NormalizeLotSize()` current line 263, `CanOpenNewTrade()` line 308, `ExecuteOpen()` line 837.
   - What code did: pre-fix bridge allowed `MaxPyramidTrades=8`, `MaxPyramidTradesPerSignal=8`, `AllowSameCandlePyramids=true`, `MaxTradesPerDay=60`; `NormalizeLotSize()` could raise requested volume to `MinLotSize`.
   - Why bad trades could open: EA could accept repeated same-symbol/same-comment add-ons and alter approved lot size.
   - Example scenario: Python approved one setup, EA could still permit multiple same-signal opens if commands arrived.
   - Fix: bridge now defaults to max pyramid `1`, same-candle pyramids false, max daily `30`, no net-profit TP mutation, and no lot inflation.

6. Re-entry race after close
   - File: `/opt/cipherfx_mt5/mt5_bot.py`
   - Function: close/reconcile paths current lines 533, 825, 1049, 1338.
   - What code did: pre-fix re-entry relied mostly on closed-trade DB rows; very fast close/reopen windows could beat reconciliation.
   - Why bad trades opened: a just-closed symbol could be eligible again before the close was fully reflected in the gate.
   - Example: XAUUSD opened again 17 seconds after a winning close.
   - Fix: close commands and history reconciliation now stamp `last_symbol_closes` in state; execution gate enforces `MT5_SYMBOL_REENTRY_COOLDOWN_SECONDS=900`.

## D. Safety Gate Review

- Max 60 trades/day: enforced by `_daily_trade_count_allows_entry()` and VPS env `MT5_MAX_DAILY_TRADES=60`.
- Max daily loss: enforced by `_daily_risk_allows_entry()` with `MT5_DAILY_LOSS_OVERRIDE_USD=1000`.
- Max 1 loss/day for strict symbols: `MT5_MAX_SYMBOL_LOSSES_PER_DAY=1`, `MT5_PRIORITY_MAX_SYMBOL_LOSSES_PER_DAY=1`.
- Symbol-specific logic: enforced by symbol aliases and per-symbol env keys; XAUUSD/GOLD caps added.
- No stale signals: `MT5_SIGNAL_EXPIRY_SECONDS=120`; gate requires signal timestamp.
- No duplicate orders: gate checks live positions and pending orders immediately before send.
- No pyramiding unless allowed: Python env `MT5_ALLOW_SIGNAL_PYRAMIDING=0`; bridge max pyramid now `1`.
- Spread/ADX/z-score/ATR gates: still enforced before execution gate in `_quality_allows_entry()` / `_spread_allows_entry()`.
- No trade after blocked/rejected signal: `_place_trade()` kill-switch requires `_execution_gate_id`.
- No reconnect/retry duplicate: pre-send duplicate check runs after the gate and before bridge command write.

## E. Duplicate Bot Check

Checked PM2, systemd, cron, Docker, Python processes, Wine/MT5 processes.

Current running owners:

- `scalpbot-mt5.service`: one `python /opt/cipherfx_mt5/run_mt5_bot.py`
- one MT5 `terminal64.exe`
- `scalpbot-dashboard-mt5.service`: one uvicorn dashboard API

No PM2 processes, no cron jobs, no Docker containers running bot copies. Duplicate bot instance was not the root cause.

## F. MT5 Order Audit

Evidence from `/opt/cipherfx_mt5/state/mt5_state.db` and MT5 bridge `deals.csv`:

- XAUUSD had 23 closed trades today: 9 wins, 14 losses.
- All 37 MT5 deal groups since the 15:55 UTC session restart matched bot DB trade rows after applying MT5 broker-time offset of `broker_time - 3h`.
- Unmatched MT5 groups: 0.
- Current open MT5 positions were adopted/matched in bot state.

Important evidence:

- XAUUSD win: opened `2026-07-08T17:22:14`, closed `2026-07-08T17:23:32`, realized `+1.16`.
- Next XAUUSD: opened `2026-07-08T17:23:49`, closed `2026-07-08T17:25:47`, realized `-4.08`.
- This proves the churn came from approved bot logic, not an unmatched outside MT5 actor.

Post-fix:

- Bot status after user-directed risk restore: `daily_trade_count=37`, `max_daily_trades=60`, `daily_loss_limit_usd=1000.0`, `mt5_connected=true`.
- Audit log: `/opt/cipherfx_mt5/state/mt5_decision_audit.jsonl` contains `daily_trade_count_gate` block entries.
- No bridge result files were created after final restart at `2026-07-08T18:32:10 UTC`, so no new MT5 orders were sent during verification.

## G. Hard Fix Requirements Implemented

- Single execution gate before order send: `_execution_gate_allows_entry()`.
- Trade lock per symbol: `symbol_trade_locks`.
- Daily trade counter global and per symbol: `MT5_MAX_DAILY_TRADES=60`, `MT5_MAX_DAILY_TRADES_PER_SYMBOL`.
- Strict loss stop per symbol: symbol loss limits set to `1`.
- Signal expiry timestamp: `generated_at` plus `MT5_SIGNAL_EXPIRY_SECONDS=120`.
- Duplicate order prevention: live positions, pending orders, and tracked pending state checked before send.
- Magic number validation: `_magic_number_allows_entry()`.
- Audit log for decisions: `mt5_decision_audit.jsonl`.
- Dry-run mode: existing `MT5_DRY_RUN` remains supported.
- Kill switch: `MT5_KILL_ON_UNEXPECTED_TRADE_PATH=1`.
- Dashboard manual entry disabled by default.
- Bridge lot/TP/pyramid defaults hardened.

## H. Final Verdict

- Hidden or unexpected trade-opening code: yes. Dashboard manual endpoints and bridge EA command execution existed outside the scanner path.
- Logic bugs: yes. The live bot allowed priority strategies and repeated re-entry without a central execution gate.
- Duplicate bot instances: no evidence. Only one bot service and one MT5 terminal were running after the fix.
- Stale/reused signals: risk existed because there was no signal expiry; fixed with timestamp expiry.
- Pyramiding/retry logic: bridge-side pyramiding was unsafe; fixed to one active setup and no same-candle pyramids.
- Symbol-specific overwrite: XAUUSD/GOLD was not protected enough by runtime env; fixed with strict per-symbol/day and loss caps.
- Exact prevention: a gated `_place_trade()` plus strict VPS env and bridge hardening now blocks the path that produced the XAUUSD churn.
