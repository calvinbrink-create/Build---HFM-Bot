#!/usr/bin/env bash
set -euo pipefail
sed -i "s/^MT5_MAX_DAILY_LOSS_USD=.*/MT5_MAX_DAILY_LOSS_USD=50/" /etc/scalpbot/scalpbot-mt5.env
sed -i "s/^MT5_DAILY_LOSS_OVERRIDE_USD=.*/MT5_DAILY_LOSS_OVERRIDE_USD=0/" /etc/scalpbot/scalpbot-mt5.env
sed -i "s/^input double DailyLossLimitUSD = .*/input double DailyLossLimitUSD = 200.00;/" /opt/cipherfx_mt5/mt5_bridge/CipherFxBridge.mq5
sed -i "s/^input double DailyLossLimitUSD = .*/input double DailyLossLimitUSD = 200.00;/" "/root/.mt5/drive_c/Program Files/MetaTrader 5/MQL5/Experts/CipherFxBridge.mq5"
systemctl restart scalpbot-mt5.service
