#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import smtplib
import sys
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

APP_DIR = Path('/opt/cipherfx_mt5')
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

os.environ.setdefault('CIPHERFX_BROKER_BACKEND', 'mt5')
os.environ.setdefault('SCALPBOT_STATE_DB', '/opt/cipherfx_mt5/state/mt5_state.db')

from dashboard.backend import state_store as db

OUTBOX = Path(os.getenv('CIPHERFX_REPORT_OUTBOX', '/opt/cipherfx_mt5/state/reports'))
TZ = ZoneInfo('Africa/Johannesburg')


def money(value: float) -> str:
    num = float(value or 0.0)
    sign = '+' if num > 0 else '-' if num < 0 else ''
    return f"{sign}${abs(num):.2f}"


def build_report(period: str) -> tuple[str, str, dict]:
    payload = db.read_deals_period(period=period, limit=1000)
    summary = payload.get('summary', {})
    now = datetime.now(TZ)
    label = {'today': 'Daily', 'daily': 'Daily', 'week': 'Weekly', 'weekly': 'Weekly', 'month': 'Monthly', 'monthly': 'Monthly'}.get(period, period.title())
    subject = f"Cipher FX MT5 {label} Trades Report - {now:%Y-%m-%d %H:%M SAST}"
    lines = [
        subject,
        '',
        f"Period: {payload.get('start_date') or 'all'} to {payload.get('end_date') or 'all'}",
        f"Trades: {summary.get('total_trades', 0)}",
        f"P&L: {money(summary.get('pnl', 0))}",
        f"Win rate: {summary.get('win_rate', 0)}%",
        f"Profit factor: {summary.get('profit_factor', 0)}",
        f"Wins/Losses/BE: {summary.get('wins', 0)}/{summary.get('losses', 0)}/{summary.get('breakeven', 0)}",
        f"Best/Worst: {money(summary.get('best_trade', 0))} / {money(summary.get('worst_trade', 0))}",
        '',
        'By symbol:',
    ]
    for row in summary.get('by_symbol', []):
        lines.append(f"- {row.get('symbol')}: {row.get('trades')} deal(s), P&L {money(row.get('pnl', 0))}, W/L {row.get('wins', 0)}/{row.get('losses', 0)}")
    lines.extend(['', 'Recent deals:'])
    for row in payload.get('deals', [])[:25]:
        lines.append(f"- {row.get('closed_label') or row.get('closed_at') or row.get('trade_date')} {row.get('symbol') or row.get('sym')} {row.get('direction') or row.get('side')} {row.get('qty') or row.get('volume')} P&L {money(row.get('pnl') or row.get('realized') or 0)}")
    body = '\n'.join(lines) + '\n'
    return subject, body, payload


def send_email(subject: str, body: str) -> str:
    to_addr = os.getenv('CIPHERFX_REPORT_EMAIL_TO', '').strip()
    host = os.getenv('CIPHERFX_SMTP_HOST', '').strip()
    if not to_addr or not host:
        return 'outbox_only_no_email_config'
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = os.getenv('CIPHERFX_REPORT_EMAIL_FROM', os.getenv('CIPHERFX_SMTP_USER', 'cipherfx@localhost'))
    msg['To'] = to_addr
    msg.set_content(body)
    port = int(os.getenv('CIPHERFX_SMTP_PORT', '587'))
    user = os.getenv('CIPHERFX_SMTP_USER', '')
    password = os.getenv('CIPHERFX_SMTP_PASSWORD', '')
    with smtplib.SMTP(host, port, timeout=20) as smtp:
        if os.getenv('CIPHERFX_SMTP_TLS', '1').strip().lower() not in {'0', 'false', 'no', 'off'}:
            smtp.starttls()
        if user:
            smtp.login(user, password)
        smtp.send_message(msg)
    return f'email_sent_to_{to_addr}'


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--period', choices=['today', 'week', 'month'], required=True)
    args = parser.parse_args()
    OUTBOX.mkdir(parents=True, exist_ok=True)
    subject, body, payload = build_report(args.period)
    stamp = datetime.now(TZ).strftime('%Y%m%d_%H%M%S')
    txt_path = OUTBOX / f"mt5_{args.period}_report_{stamp}.txt"
    json_path = OUTBOX / f"mt5_{args.period}_report_{stamp}.json"
    txt_path.write_text(body)
    json_path.write_text(json.dumps(payload, indent=2, default=str))
    status = send_email(subject, body)
    latest = OUTBOX / f"mt5_{args.period}_report_latest.txt"
    latest.write_text(body + f"\nDelivery status: {status}\n")
    print(f"report={txt_path}")
    print(f"json={json_path}")
    print(f"delivery={status}")
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
