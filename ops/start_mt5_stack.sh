#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${APP_DIR:-$(cd "${SCRIPT_DIR}/.." && pwd)}"
VENV_DIR="${MT5_VENV_DIR:-$APP_DIR/.venv_mt5}"

cd "$APP_DIR"

if [ -n "${ENV_FILE:-}" ]; then
  if [ ! -f "$ENV_FILE" ]; then
    echo "ERROR: explicit ENV_FILE does not exist: $ENV_FILE" >&2
    exit 64
  fi
  set -a
  . "$ENV_FILE"
  set +a
fi

"$APP_DIR/ops/guard_mt5_env.sh"

bridge_dir="${MT5_BRIDGE_DIR:-$HOME/.mt5/drive_c/Program Files/MetaTrader 5/MQL5/Files/cipherfx}"
terminal_path="${MT5_TERMINAL_PATH:-$HOME/.mt5/drive_c/Program Files/MetaTrader 5/terminal64.exe}"

# The account receipt is a runtime handshake. Remove only that receipt so a
# restart cannot make Python trust an old terminal session.
rm -f "$bridge_dir/account.txt"

# Start the terminal first; Python starts immediately afterwards and its
# gateway waits for the fresh bridge receipt while its WebSocket listener is
# already available for the EA.
bash "$APP_DIR/ops/start_mt5_terminal.sh"
sleep 2

terminal_pid="$(pgrep -f -- "$terminal_path /portable" | tail -1 || true)"
if [ -z "$terminal_pid" ]; then
  echo "ERROR: MT5 terminal did not remain running after startup" >&2
  exit 74
fi
echo "MT5_TERMINAL_STARTED pid=$terminal_pid"

# A dead terminal must stop the Python process as well. This prevents a
# stale bridge snapshot from being treated as a live market feed.
main_pid="$$"
bridge_account_file="$bridge_dir/account.txt"
bot_state_file="$APP_DIR/state/mt5_runtime_state.json"
bot_heartbeat_file="${MT5_HEARTBEAT_FILE:-$APP_DIR/state/mt5_heartbeat.json}"
health_grace_seconds="$MT5_STARTUP_HEALTH_GRACE_SECONDS"
account_max_age_seconds="$MT5_BRIDGE_ACCOUNT_MAX_AGE_SECONDS"
feed_max_age_seconds="${MT5_BRIDGE_FEED_MAX_AGE_SECONDS:-180}"
bot_max_age_seconds="$MT5_BOT_HEARTBEAT_MAX_AGE_SECONDS"
watchdog_failure_limit="${MT5_WATCHDOG_FAILURE_LIMIT:-3}"
latest_tick_mtime=0
(
  started_at="$(date +%s)"
  shutdown_requested=0
  health_failures=0
  trap 'shutdown_requested=1' INT TERM
  while true; do
    [ "$shutdown_requested" -ne 0 ] && exit 0
    now="$(date +%s)"
    if ! kill -0 "$terminal_pid" 2>/dev/null; then
      [ "$shutdown_requested" -ne 0 ] && exit 0
      echo "NOTICE: MT5 terminal PID $terminal_pid exited; requesting controlled bot shutdown" >&2
      kill -INT "$main_pid" 2>/dev/null || true
      exit 0
    fi
    if [ "$((now - started_at))" -ge "$health_grace_seconds" ]; then
      account_bad=0
      feed_bad=0
      bot_bad=0
      if [ -s "$bridge_account_file" ]; then
        # account.txt is a connection receipt, not the market-feed heartbeat.
        # It may be absent briefly while MT5 rebuilds its bridge files.
        grep -q '^terminal_connected=1$' "$bridge_account_file" 2>/dev/null || account_bad=1
      fi
      # MT5 refreshes symbol exports in a staggered cycle. Use the newest
      # tick export to detect a dead feed without restarting healthy terminals.
      latest_tick_mtime=0
      for tick_file in "$bridge_dir"/tick_*.txt; do
        [ -s "$tick_file" ] || continue
        tick_mtime="$(stat -c %Y "$tick_file" 2>/dev/null || echo 0)"
        [ "$tick_mtime" -gt "$latest_tick_mtime" ] && latest_tick_mtime="$tick_mtime"
      done
      [ "$latest_tick_mtime" -gt 0 ] && [ "$((now - latest_tick_mtime))" -le "$feed_max_age_seconds" ] || feed_bad=1
      if [ ! -s "$bot_heartbeat_file" ]; then
        bot_bad=1
      else
        bot_mtime="$(stat -c %Y "$bot_heartbeat_file" 2>/dev/null || echo 0)"
        [ "$bot_mtime" -gt 0 ] && [ "$((now - bot_mtime))" -le "$bot_max_age_seconds" ] || bot_bad=1
      fi
      if [ "$shutdown_requested" -ne 0 ]; then
        exit 0
      fi
      if [ "$feed_bad" -ne 0 ] || [ "$bot_bad" -ne 0 ] || [ "$account_bad" -ne 0 ]; then
        health_failures="$((health_failures + 1))"
        feed_age_seconds=unknown
        [ "$latest_tick_mtime" -gt 0 ] && feed_age_seconds="$((now - latest_tick_mtime))"
        echo "WARN: bridge health failed attempt=$health_failures/$watchdog_failure_limit account_bad=$account_bad feed_bad=$feed_bad bot_bad=$bot_bad feed_age_seconds=$feed_age_seconds" >&2
        if [ "$health_failures" -lt "$watchdog_failure_limit" ]; then
          sleep 5
          continue
        fi
        echo "ERROR: bridge health failed consecutively; requesting controlled systemd restart" >&2
        kill -INT "$main_pid" 2>/dev/null || true
        sleep 10
        kill -TERM "$main_pid" 2>/dev/null || true
        exit 0
      fi
      health_failures=0
    fi
    sleep 5
  done
) &
terminal_watch_pid=$!
trap 'kill "$terminal_watch_pid" 2>/dev/null || true' EXIT

if [ ! -d "$VENV_DIR" ]; then
  python3 -m venv "$VENV_DIR"
fi

. "$VENV_DIR/bin/activate"
exec python "$APP_DIR/run_mt5_bot.py"
