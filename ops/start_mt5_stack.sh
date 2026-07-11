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

bash "$APP_DIR/ops/start_mt5_terminal.sh"

if [ ! -d "$VENV_DIR" ]; then
  python3 -m venv "$VENV_DIR"
fi

. "$VENV_DIR/bin/activate"
exec python "$APP_DIR/run_mt5_bot.py"
