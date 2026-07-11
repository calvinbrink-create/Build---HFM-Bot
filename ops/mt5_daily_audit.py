#!/usr/bin/env python3
from __future__ import annotations
import json, os, sqlite3, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path
APP = Path('/opt/cipherfx_mt5')
sys.path.insert(0, str(APP))
DB = APP / 'state/mt5_state.db'
ENV = Path('/etc/scalpbot/scalpbot-mt5.env')
for raw in ENV.read_text().splitlines():
    raw = raw.strip()
    if not raw or raw.startswith("#") or "=" not in raw:
        continue
    key, value = raw.split("=", 1)
    os.environ.setdefault(key.strip(), value.strip().strip("\""))
os.environ.setdefault('CIPHERFX_BROKER_BACKEND', 'mt5')
os.environ.setdefault('SCALPBOT_STATE_DB', str(DB))
import mt5_bot
from mt5_xm_config import MT5RuntimeConfig
from dashboard.backend import state_store as db

def audit():
    checks = {}
    conn = sqlite3.connect(DB)
    status = dict(conn.execute('select key,value from bot_status'))
    today = db.trading_today()
    rows = conn.execute('select count(*) from trades where trade_date=?', (today,)).fetchone()[0]
    quick = conn.execute('pragma quick_check').fetchone()[0]
    conn.close()
    source = (APP / 'mt5_bot.py').read_text()
    env_text = ENV.read_text()
    runtime = MT5RuntimeConfig()
    export_status = {}
    try: export_status = json.loads(status.get('last_export_reconciliation', '{}'))
    except Exception: pass
    export_dir = APP / 'state/systematic_exports' / today
    files = [export_dir / 'closed_trades.csv', export_dir / 'executions.csv']
    checks['service_active'] = subprocess.run(['systemctl','is-active','--quiet','scalpbot-mt5.service']).returncode == 0
    checks['mt5_connected'] = str(status.get('mt5_connected','')).lower() == 'true'
    checks['bridge_csv_fresh'] = all(p.exists() and datetime.now().timestamp() - p.stat().st_mtime < 86400 for p in files)
    checks['sqlite_writable'] = os.access(DB, os.W_OK) and quick == 'ok'
    checks['max_open_aligned'] = os.getenv('MT5_MAX_OPEN_TRADES') == '30' and os.getenv('MT5_MAX_OPEN_TRADES_TOTAL') == '30' and runtime.max_open_trades == 30 and str(status.get('max_open_trades')) == '30'
    checks['rr_resolver_aligned'] = '_management_rr_for_position' in source and '_entry_rr_for_symbol' in source
    checks['profile_gate_active'] = source.find('_symbol_profile_gate_allows_entry') < source.find('_final_engine_policy_recheck')
    checks['cost_threshold_active'] = 'COST_TO_TARGET_BLOCKED' in source and 'MT5_DEMO_PRE_ORDER_COST_TO_TARGET_BLOCK' not in source and '0.30' in source
    checks['pending_ttl_active'] = 'return 90' in source and source.count('return 120') >= 3
    checks['last_export_reconciled'] = bool(export_status.get('reconciled')) and int(export_status.get('sqlite_trade_count', -1)) == int(rows)
    for name, value in checks.items(): print("[MT5_DAILY_AUDIT] %s=%s" % (name, "PASS" if value else "FAIL"))
    ok = all(checks.values())
    print("[MT5_DAILY_AUDIT] overall=%s generated_at=%s" % ("PASS" if ok else "FAIL", datetime.now(timezone.utc).isoformat()))
    return 0 if ok else 1

if __name__ == '__main__': raise SystemExit(audit())
