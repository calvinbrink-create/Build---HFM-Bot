#!/usr/bin/env bash
# Dedicated MT5 terminal for MANUAL / TradingView-via-PineConnector trading.
#
# Deliberately separate from start_mt5_terminal.sh: that script compiles and
# attaches CipherFxBridge and refuses to start without it. This one runs NO
# CipherFX bot. Nothing here reads or writes the bot's bridge directory, so the
# trading runtime cannot see or manage positions opened from TradingView.
#
# Required env (from /etc/scalpbot/scalpbot-mt5-manual.env):
#   MT5_PREFIX          wine prefix for this terminal, e.g. /root/.mt5_manual
#   MT5_LOGIN/PASSWORD/MT5_SERVER   the XM account this terminal logs into
#   MT5_MANUAL_EXPERT   filename of the EA to auto-attach (PineConnector .ex5)
#   PINECONNECTOR_LICENCE  licence ID for this connection
set -euo pipefail

MT5_PREFIX="${MT5_PREFIX:-/root/.mt5_manual}"
TEMPLATE_PREFIX="${MT5_TEMPLATE_PREFIX:-/root/.mt5}"
MT5_HOME="$MT5_PREFIX/drive_c/Program Files/MetaTrader 5"
DISPLAY_NUM="${MT5_DISPLAY:-:103}"
PROFILE="${MT5_MANUAL_PROFILE:-Manual}"
EXPERT="${MT5_MANUAL_EXPERT:-}"
SYMBOL="${MT5_STARTUP_SYMBOL:-US30Cash}"
CACHE_DIR="${CIPHERFX_CACHE_DIR:-/root/.cache/cipherfx-manual}"
mkdir -p "$CACHE_DIR"

# clone a working terminal on first run (gets the XM install + symbol set)
if [ ! -f "$MT5_HOME/terminal64.exe" ]; then
  echo "cloning terminal from $TEMPLATE_PREFIX ..."
  mkdir -p "$MT5_PREFIX"
  cp -a "$TEMPLATE_PREFIX"/. "$MT5_PREFIX"/
  # strip the bot's EA and its bridge output so this terminal cannot run it
  rm -f "$MT5_HOME/MQL5/Experts/CipherFxBridge."* 2>/dev/null || true
  rm -rf "$MT5_HOME/MQL5/Files/cipherfx" 2>/dev/null || true
fi

PROFILE_DIR="$MT5_HOME/MQL5/Profiles/Charts/$PROFILE"
mkdir -p "$PROFILE_DIR"
STARTUP_INI="$MT5_HOME/Config/manual_startup.ini"

{
  echo "[Common]"
  echo "Login=${MT5_LOGIN}"
  echo "Password=${MT5_PASSWORD}"
  echo "Server=${MT5_SERVER}"
  echo "AutoConfiguration=false"
  echo "KeepPrivate=1"
  echo ""
  echo "[Charts]"
  echo "ProfileLast=${PROFILE}"
  echo "MaxBarsInChart=5000000"
  echo ""
  echo "[Experts]"
  echo "AllowLiveTrading=1"
  echo "AllowDllImport=1"
  echo "AllowWebRequest=1"
  # PineConnector's EA polls these endpoints for signals
  echo "WebRequestURL=https://api.pineconnector.com"
  echo "WebRequestURL=https://license.pineconnector.com"
  echo "Enabled=1"
  echo "Account=0"
  echo "Profile=0"
  if [ -n "$EXPERT" ]; then
    echo ""
    echo "[StartUp]"
    echo "Expert=${EXPERT}"
    echo "Symbol=${SYMBOL}"
    echo "Period=M15"
  fi
} >"$STARTUP_INI"
chmod 0600 "$STARTUP_INI"

if ! pgrep -f "Xvfb ${DISPLAY_NUM}" >/dev/null 2>&1; then
  nohup Xvfb "${DISPLAY_NUM}" -screen 0 1440x900x24 >"$CACHE_DIR/xvfb.log" 2>&1 &
  sleep 2
fi
export DISPLAY="${DISPLAY_NUM}"
export WINEPREFIX="${MT5_PREFIX}"

if [ -z "$EXPERT" ]; then
  echo "NOTE: MT5_MANUAL_EXPERT is unset - terminal will start with no EA attached."
  echo "      Drop the PineConnector .ex5 into $MT5_HOME/MQL5/Experts/ and set it."
fi

cd "$MT5_HOME"
exec wine "$MT5_HOME/terminal64.exe" /portable /config:"$STARTUP_INI"
