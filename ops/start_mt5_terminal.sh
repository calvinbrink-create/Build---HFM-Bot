#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MT5_PREFIX="${MT5_PREFIX:-$HOME/.mt5}"
DISPLAY_NUM="${DISPLAY_NUM:-:99}"
TERMINAL_PATH="${MT5_TERMINAL_PATH:-$MT5_PREFIX/drive_c/Program Files/MetaTrader 5/terminal64.exe}"
APP_DIR="${APP_DIR:-$(cd "${SCRIPT_DIR}/.." && pwd)}"
BRIDGE_DIR="${MT5_BRIDGE_DIR:-$MT5_PREFIX/drive_c/Program Files/MetaTrader 5/MQL5/Files/cipherfx}"
CACHE_DIR="${CIPHERFX_CACHE_DIR:-$HOME/.cache/cipherfx}"
STARTUP_INI="${CACHE_DIR}/mt5-startup.ini"
STARTUP_INI_WIN="Z:$(printf '%s' "$STARTUP_INI" | sed 's#/#\\#g')"
STARTUP_SYMBOL="${MT5_STARTUP_SYMBOL:-EURUSD}"
BRIDGE_PROFILE="${MT5_BRIDGE_PROFILE:-CipherFxBridge}"
MT5_SERVER="${MT5_SERVER:-}"
MT5_LOGIN="${MT5_LOGIN:-}"
MT5_PASSWORD="${MT5_PASSWORD:-}"
MT5_FORCE_LOGIN="${MT5_FORCE_LOGIN:-0}"

mkdir -p "$CACHE_DIR"
mkdir -p "$BRIDGE_DIR/commands" "$BRIDGE_DIR/results"

MT5_HOME="${MT5_PREFIX}/drive_c/Program Files/MetaTrader 5"
CHARTS_DIR="${MT5_HOME}/MQL5/Profiles/Charts"
BRIDGE_PROFILE_DIR="${CHARTS_DIR}/${BRIDGE_PROFILE}"
EXPERTS_DIR="${MT5_HOME}/MQL5/Experts"
BRIDGE_SOURCE="${EXPERTS_DIR}/CipherFxBridge.mq5"
BRIDGE_BINARY="${EXPERTS_DIR}/CipherFxBridge.ex5"
BRIDGE_COMPILE_LOG="${EXPERTS_DIR}/CipherFxBridge.log"
METAEDITOR_LOG="${MT5_HOME}/logs/metaeditor.log"
mkdir -p "$BRIDGE_PROFILE_DIR"
# Keep the bridge profile empty so the startup Expert can always open its chart.
# The old Default profile can accumulate enough charts to hit MT5's open-chart limit.
find "$BRIDGE_PROFILE_DIR" -maxdepth 1 -type f \( -name 'chart*.chr' -o -name 'order.wnd' \) -delete 2>/dev/null || true

if [ "$MT5_FORCE_LOGIN" = "1" ]; then
  ts="$(date +%Y%m%d%H%M%S)"
  for path in \
    "$MT5_PREFIX/drive_c/Program Files/MetaTrader 5/Config/accounts.dat" \
    "$MT5_PREFIX/drive_c/Program Files/MetaTrader 5/MQL5/Files/cipherfx/account.txt"; do
    if [ -f "$path" ]; then
      mv "$path" "${path}.bak-${ts}"
    fi
  done
fi

if [ -f "$APP_DIR/mt5_bridge/CipherFxBridge.mq5" ]; then
  install -m 0644 "$APP_DIR/mt5_bridge/CipherFxBridge.mq5" "$BRIDGE_SOURCE"
fi
if [ ! -s "$BRIDGE_SOURCE" ] || ! cmp -s "$APP_DIR/mt5_bridge/CipherFxBridge.mq5" "$BRIDGE_SOURCE"; then
  echo "ERROR: deployed MQL source does not match the authoritative source" >&2
  exit 69
fi

cat >"$STARTUP_INI" <<EOF
[Common]
Login=${MT5_LOGIN}
Password=${MT5_PASSWORD}
Server=${MT5_SERVER}
AutoConfiguration=false
KeepPrivate=1

[Charts]
ProfileLast=${BRIDGE_PROFILE}

[Experts]
AllowLiveTrading=1
AllowDllImport=1
Enabled=1
Account=0
Profile=0

[StartUp]
Expert=CipherFxBridge.ex5
Symbol=${STARTUP_SYMBOL}
Period=M15
EOF
chmod 0600 "$STARTUP_INI"

if ! pgrep -f "Xvfb ${DISPLAY_NUM}" >/dev/null 2>&1; then
  nohup Xvfb "${DISPLAY_NUM}" -screen 0 1440x900x24 >"$CACHE_DIR/xvfb.log" 2>&1 &
  sleep 2
fi

export DISPLAY="${DISPLAY_NUM}"
export WINEPREFIX="${MT5_PREFIX}"

compile_started_epoch="$(date +%s)"
rm -f "$BRIDGE_COMPILE_LOG"
set +e
(
  cd "$MT5_HOME"
  wine "$MT5_HOME/MetaEditor64.exe" /compile:"MQL5\\Experts\\CipherFxBridge.mq5" /log
) >"$CACHE_DIR/mt5-compile.log" 2>&1
compile_rc=$?
set -e
sleep 2

if [ ! -s "$METAEDITOR_LOG" ] || [ "$(stat -c %Y "$METAEDITOR_LOG")" -lt "$compile_started_epoch" ]; then
  echo "ERROR: MetaEditor did not update its compiler log (rc=$compile_rc): $METAEDITOR_LOG" >&2
  cat "$CACHE_DIR/mt5-compile.log" >&2
  exit 70
fi
compile_summary="$(iconv -f UTF-16LE -t UTF-8 "$METAEDITOR_LOG")"
compile_result="$(grep -F "Compile" <<<"$compile_summary" | grep -F "CipherFxBridge.mq5" | tail -1)"
if ! grep -Fq "0 errors, 0 warnings" <<<"$compile_result"; then
  echo "ERROR: latest MQL compile result is not clean (rc=$compile_rc)" >&2
  printf '%s\n' "$compile_result" >&2
  exit 71
fi
if [ ! -s "$BRIDGE_BINARY" ] || [ "$(stat -c %Y "$BRIDGE_BINARY")" -lt "$compile_started_epoch" ]; then
  echo "ERROR: compiled EX5 is missing, empty, or stale: $BRIDGE_BINARY" >&2
  exit 72
fi
echo "MQL_COMPILE_OK ex5_sha256=$(sha256sum "$BRIDGE_BINARY" | awk '{print $1}')"

while IFS= read -r pid; do
  [ -n "$pid" ] || continue
  cmdline="$(tr '\0' ' ' <"/proc/${pid}/cmdline" 2>/dev/null || true)"
  case "$cmdline" in
    *"$TERMINAL_PATH"*) kill "$pid" >/dev/null 2>&1 || true ;;
  esac
done < <(pgrep -f "terminal64.exe" 2>/dev/null || true)
sleep 2
nohup wine "${TERMINAL_PATH}" /portable /login:"${MT5_LOGIN}" /config:"${STARTUP_INI_WIN}" >"$CACHE_DIR/mt5-terminal.log" 2>&1 &
echo "MT5 terminal started on DISPLAY=${DISPLAY}"
