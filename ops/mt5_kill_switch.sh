#!/usr/bin/env bash
set -euo pipefail

FLAG="${MT5_TRADING_DISABLED_FLAG:-/opt/cipherfx_mt5/state/trading_disabled.flag}"
CMD="${1:-status}"

case "$CMD" in
  disable)
    REASON="${2:-manual disable}"
    mkdir -p "$(dirname "$FLAG")"
    {
      printf 'disabled_at_utc=%s
' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
      printf 'reason=%s
' "$REASON"
      printf 'scope=%s
' 'new entries only; existing positions are not closed'
    } > "$FLAG"
    chmod 644 "$FLAG"
    printf 'MT5 new entries disabled only. Existing positions are not closed. Flag: %s
' "$FLAG"
    ;;
  enable)
    rm -f "$FLAG"
    printf 'MT5 new entries enabled. Flag removed: %s
' "$FLAG"
    ;;
  status)
    if [ -f "$FLAG" ]; then
      printf 'MT5 new entries disabled only. Existing positions are not closed. Flag: %s
' "$FLAG"
      sed -n '1,20p' "$FLAG"
    else
      printf 'MT5 new entries enabled. No disable flag at: %s
' "$FLAG"
    fi
    ;;
  *)
    printf 'Usage: %s [status|disable "reason"|enable]
' "$0" >&2
    exit 64
    ;;
esac
