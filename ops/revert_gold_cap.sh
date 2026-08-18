#!/bin/bash
# One-shot revert of the 24h gold daily-loss-cap removal (set 2026-08-18).
# Restores MT5_MAX_XAUUSD_LOSSES_PER_DAY=2 and restarts the bot.
set -e
ENV=/etc/scalpbot/scalpbot-mt5.env
cp "$ENV" "${ENV}.bak_goldcap_autorevert_$(date +%Y%m%d%H%M)"
sed -i 's/^MT5_MAX_XAUUSD_LOSSES_PER_DAY=.*/MT5_MAX_XAUUSD_LOSSES_PER_DAY=2/' "$ENV"
systemctl restart scalpbot-mt5
logger -t cipherfx "gold daily loss cap auto-reverted to 2"
