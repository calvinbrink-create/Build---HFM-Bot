#!/usr/bin/env bash
set -euo pipefail

EXPECTED_BACKEND="mt5"
EXPECTED_DB="/opt/cipherfx_mt5/state/mt5_state.db"
EXPECTED_STATE_DIR="/opt/cipherfx_mt5/state"

fail() {
  printf '%s
' "[guard_mt5_env] ERROR: $1" >&2
  exit 64
}

if [ "${CIPHERFX_BROKER_BACKEND:-}" != "$EXPECTED_BACKEND" ]; then
  fail "CIPHERFX_BROKER_BACKEND must be $EXPECTED_BACKEND"
fi

if [ -z "${SCALPBOT_STATE_DB:-}" ]; then
  fail "SCALPBOT_STATE_DB is required"
fi

if [ "${SCALPBOT_STATE_DB:-}" != "$EXPECTED_DB" ]; then
  fail "SCALPBOT_STATE_DB must be $EXPECTED_DB"
fi

case "${SCALPBOT_STATE_DB:-}" in
  *"/opt/scalp_v3"*)
    fail "SCALPBOT_STATE_DB must not point into /opt/scalp_v3"
    ;;
esac

if [ "$(basename -- "${SCALPBOT_STATE_DB:-}")" = "bot_state.db" ]; then
  fail "SCALPBOT_STATE_DB must not use bot_state.db for MT5"
fi

if [ ! -d "$EXPECTED_STATE_DIR" ]; then
  fail "required MT5 state directory is missing: $EXPECTED_STATE_DIR"
fi

printf '%s
' '[guard_mt5_env] OK'
