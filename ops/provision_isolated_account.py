#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import socket
import subprocess
import sys
from pathlib import Path


SOURCE_APP = Path("/opt/cipherfx_mt5")
SOURCE_TERMINAL_PREFIX = Path("/root/.mt5")
CLIENT_ROOT = Path("/opt/cipherfx_users")
ENV_ROOT = Path("/etc/scalpbot/clients")
SERVICE_ROOT = Path("/etc/systemd/system")


APP_EXCLUDES = [
    ".git",
    ".venv",
    "__pycache__",
    "*.pyc",
    "backups",
    "state",
    "*.log",
    "dashboard/backend/*.db*",
    "dashboard/mt5_backend/*.db*",
    "mt5_bridge/commands/*",
    "mt5_bridge/results/*",
    "mt5_bridge/account.txt*",
    "mt5_bridge/positions.csv",
    "mt5_bridge/orders.csv",
    "mt5_bridge/deals.csv",
]

TERMINAL_EXCLUDES = [
    "drive_c/Program Files/MetaTrader 5/Config/*",
    "drive_c/Program Files/MetaTrader 5/Bases/*",
    "drive_c/Program Files/MetaTrader 5/logs/*",
    "drive_c/Program Files/MetaTrader 5/MQL5/Files/cipherfx/*",
    "drive_c/Program Files/MetaTrader 5/MQL5/logs/*",
    "drive_c/Program Files/MetaTrader 5/Tester/logs/*",
    "*.log",
]


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    if not slug:
        raise SystemExit("User name must produce a non-empty slug")
    return slug[:48]


def q(value: str | Path) -> str:
    return shlex.quote(str(value))


def run(cmd: list[str], apply: bool) -> None:
    print("+", " ".join(q(part) for part in cmd))
    if apply:
        subprocess.run(cmd, check=True)


def env_quote(value: str | int | float) -> str:
    text = str(value)
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_file(path: Path, content: str, apply: bool, mode: int = 0o644, force: bool = False) -> None:
    print(f"+ write {path}")
    if not apply:
        return
    if path.exists() and not force:
        raise SystemExit(f"Refusing to overwrite existing file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(mode)


def read_used_values() -> tuple[set[int], set[str]]:
    ports: set[int] = set()
    displays: set[str] = {":99"}
    for env_path in ENV_ROOT.glob("*.env"):
        try:
            lines = env_path.read_text(errors="ignore").splitlines()
        except OSError:
            continue
        for line in lines:
            if "=" not in line or line.strip().startswith("#"):
                continue
            key, raw = line.split("=", 1)
            value = raw.strip().strip('"').strip("'")
            if key == "CIPHERFX_DASHBOARD_PORT" and value.isdigit():
                ports.add(int(value))
            if key == "DISPLAY_NUM" and value:
                displays.add(value)
    return ports, displays


def port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def choose_port() -> int:
    used_ports, _ = read_used_values()
    for port in range(8020, 8100):
        if port not in used_ports and port_is_free(port):
            return port
    raise SystemExit("No free dashboard port found in 8020-8099")


def choose_display() -> str:
    _, used_displays = read_used_values()
    for number in range(100, 150):
        display = f":{number}"
        if display in used_displays:
            continue
        if Path(f"/tmp/.X11-unix/X{number}").exists():
            continue
        return display
    raise SystemExit("No free display found in :100-:149")


def derive_magic(slug: str, login: str) -> int:
    seed = f"{slug}:{login}".encode()
    return 300000 + (int(hashlib.sha256(seed).hexdigest()[:8], 16) % 600000)


def password_value(args: argparse.Namespace) -> str:
    if args.password_file:
        return Path(args.password_file).read_text().strip()
    return ""


def env_lines(values: dict[str, str | int | float]) -> str:
    return "\n".join(f"{key}={env_quote(value)}" for key, value in values.items()) + "\n"


def provision_mt5(args: argparse.Namespace) -> None:
    slug = args.slug or slugify(args.user_name)
    platform = "mt5"
    client_dir = CLIENT_ROOT / slug / platform
    app_dir = client_dir / "app"
    state_dir = client_dir / "state"
    cache_dir = client_dir / "cache"
    terminal_prefix = client_dir / "terminal"
    bridge_dir = terminal_prefix / "drive_c/Program Files/MetaTrader 5/MQL5/Files/cipherfx"
    terminal_path = terminal_prefix / "drive_c/Program Files/MetaTrader 5/terminal64.exe"
    env_file = ENV_ROOT / f"{slug}-{platform}.env"
    dashboard_port = args.dashboard_port or choose_port()
    display_num = args.display_num or choose_display()
    magic = args.magic or derive_magic(slug, args.login)
    bot_service = f"cipherfx-{slug}-{platform}-bot.service"
    dashboard_service = f"cipherfx-{slug}-{platform}-dashboard.service"

    if client_dir.exists() and not args.force:
        raise SystemExit(f"Client directory already exists: {client_dir}")
    if env_file.exists() and not args.force:
        raise SystemExit(f"Env file already exists: {env_file}")

    run(["mkdir", "-p", str(client_dir), str(state_dir), str(cache_dir), str(ENV_ROOT)], args.apply)

    rsync_app = ["rsync", "-a", "--delete"]
    for pattern in APP_EXCLUDES:
        rsync_app += ["--exclude", pattern]
    rsync_app += [str(SOURCE_APP) + "/", str(app_dir) + "/"]
    run(rsync_app, args.apply)

    if not args.skip_terminal_copy:
        rsync_terminal = ["rsync", "-a", "--delete"]
        for pattern in TERMINAL_EXCLUDES:
            rsync_terminal += ["--exclude", pattern]
        rsync_terminal += [str(SOURCE_TERMINAL_PREFIX) + "/", str(terminal_prefix) + "/"]
        run(rsync_terminal, args.apply)

    run(["mkdir", "-p", str(bridge_dir / "commands"), str(bridge_dir / "results")], args.apply)
    if args.apply:
        for clean_dir in [
            terminal_prefix / "drive_c/Program Files/MetaTrader 5/Config",
            terminal_prefix / "drive_c/Program Files/MetaTrader 5/Bases",
        ]:
            clean_dir.mkdir(parents=True, exist_ok=True)
        venv_link = app_dir / ".venv"
        if not venv_link.exists():
            venv_link.symlink_to(SOURCE_APP / ".venv")

    env = {
        "CIPHERFX_CLIENT_NAME": args.user_name,
        "CIPHERFX_CLIENT_SLUG": slug,
        "CIPHERFX_DASHBOARD_PORT": dashboard_port,
        "CIPHERFX_CACHE_DIR": str(cache_dir),
        "CIPHERFX_ACCOUNT_LABEL": f"{args.user_name} MT5 Demo",
        "CIPHERFX_BROKER_BACKEND": "mt5",
        "CIPHERFX_BROKER_NAME": "XM Global Demo",
        "CIPHERFX_PLATFORM_NAME": "MetaTrader 5",
        "CIPHERFX_PUBLIC_HTTPS": "1",
        "DISPLAY_NUM": display_num,
        "APP_DIR": str(app_dir),
        "ENV_FILE": str(env_file),
        "MT5_PREFIX": str(terminal_prefix),
        "MT5_LOGIN": args.login,
        "MT5_PASSWORD": password_value(args),
        "MT5_SERVER": args.server,
        "MT5_TRADE_MODE": "demo",
        "MT5_DRY_RUN": "1" if args.dry_run else "0",
        "MT5_EXECUTION_MODE": "bridge",
        "MT5_MAGIC": magic,
        "MT5_TERMINAL_PATH": str(terminal_path),
        "MT5_BRIDGE_DIR": str(bridge_dir),
        "MT5_STATE_FILE": str(state_dir / "mt5_runtime_state.json"),
        "SCALPBOT_STATE_DB": str(state_dir / "mt5_state.db"),
        "MT5_SYMBOLS_FILE": str(app_dir / "mt5_symbols.json"),
        "MT5_POLL_SECONDS": "0.2",
        "MT5_HEARTBEAT_SECONDS": "1.0",
        "MT5_CLOSE_RECONCILE_SECONDS": "0.5",
        "MT5_DEAL_RECONCILE_SECONDS": "3.0",
        "MT5_SCAN_SECONDS": "900",
        "MT5_ALIGN_FULL_SCAN_TO_M15": "1",
        "MT5_M15_SCAN_DELAY_SECONDS": "8",
        "MT5_PRIORITY_SCAN_ENABLE": "1",
        "MT5_PRIORITY_SCAN_SECONDS": "45",
        "MT5_PRIORITY_SYMBOLS": "NAS100,US30,SPX500,US2000,EURUSD",
        "MT5_INDEX_MAX_SPREAD_ATR_FRAC": "0.17",
        "MT5_PRIORITY_INDEX_MAX_SPREAD_ATR_FRAC": "0.17",
        "MT5_SCALP_TP_ENABLE": "1",
        "MT5_SCALP_TP_R": "1.00",
        "MT5_SCALP_TP_R_METALS": "0.70",
        "MT5_SCALP_CLOSE_ENABLE": "1",
        "MT5_SCALP_CLOSE_R": "0.85",
        "MT5_SCALP_CLOSE_R_METALS": "0.45",
        "MT5_ENABLE_PYRAMIDING": "1",
        "MT5_ALLOW_SIGNAL_PYRAMIDING": "0",
        "MT5_PYRAMID_PROFIT_ENABLE": "1",
        "MT5_PYRAMID_TRIGGER_R": "0.20",
        "MT5_PYRAMID_MIN_PROFIT_USD": "2",
        "MT5_PYRAMID_MAX_POSITIONS_PER_SYMBOL": "8",
        "MT5_PYRAMID_BURST_SIZE": "8",
        "MT5_PYRAMID_COOLDOWN_SECONDS": "0.5",
        "MT5_PYRAMID_MAX_ENTRY_AGE_SECONDS": "300",
        "MT5_PYRAMID_LOT_MULT": "1.0",
        "MT5_PYRAMID_MAX_LOT": "2.00",
        "MT5_PYRAMID_STOP_RISK_FRACTION": "0.50",
        "MT5_PYRAMID_MAX_STOP_DISTANCE_INDICES": "100",
        "MT5_PYRAMID_MAX_STOP_DISTANCE_METALS": "3.0",
        "MT5_PYRAMID_MAX_TRADE_RISK_USD": "220",
        "MT5_MIN_SCORE_XAUUSD": "96",
        "MT5_MAX_STOP_DISTANCE_XAUUSD": "4.0",
        "MT5_BLOCK_ENTRY_UTC_WINDOWS_METALS": "21:55-22:10,00:55-02:05",
        "MT5_MAX_POSITION_PCT_INDICES": "12.0",
        "MT5_MAX_POSITION_PCT_FOREX": "25.0",
        "MT5_MAX_POSITION_PCT_STOCKS": "8.0",
        "MT5_MAX_POSITION_PCT_CRYPTO": "8.0",
        "MT5_MAX_POSITION_PCT_METALS": "1.0",
        "MT5_RISK_PCT": "2.0",
        "MT5_RISK_PCT_INDICES": "3.0",
        "MT5_RISK_PCT_FOREX": "2.0",
        "MT5_RISK_PCT_STOCKS": "2.0",
        "MT5_RISK_PCT_CRYPTO": "1.0",
        "MT5_RISK_PCT_METALS": "0.35",
        "MT5_MAX_LOT_DEFAULT": "0.90",
        "MT5_MAX_LOT_FOREX": "0.90",
        "MT5_MAX_LOT_STOCKS": "2.00",
        "MT5_MAX_LOT_XAUUSD": "0.01",
        "MT5_MAX_LOT_GOLD": "0.01",
        "MT5_MAX_LOT_METALS": "0.01",
        "MT5_MAX_LOT_INDICES": "2.00",
        "MT5_MAX_LOT_CRYPTO": "0.90",
        "MT5_MAX_TRADE_RISK_USD": "150",
        "MT5_MAX_TRADE_RISK_INDICES_USD": "220",
        "MT5_MAX_TRADE_RISK_FOREX_USD": "150",
        "MT5_MAX_TRADE_RISK_STOCKS_USD": "150",
        "MT5_MAX_TRADE_RISK_CRYPTO_USD": "100",
        "MT5_MAX_TRADE_RISK_METALS_USD": "15",
        "MT5_MAX_OPEN_POSITION_LOSS_INDICES_USD": "220",
        "MT5_MAX_OPEN_POSITION_LOSS_STOCKS_USD": "150",
        "MT5_MAX_OPEN_POSITION_LOSS_FOREX_USD": "150",
        "MT5_MAX_OPEN_POSITION_LOSS_CRYPTO_USD": "100",
        "MT5_MAX_OPEN_POSITION_LOSS_METALS_USD": "25",
        "MT5_MAX_SYMBOL_FLOATING_LOSS_INDICES_USD": "600",
        "MT5_MAX_SYMBOL_FLOATING_LOSS_STOCKS_USD": "300",
        "MT5_MAX_SYMBOL_FLOATING_LOSS_FOREX_USD": "300",
        "MT5_MAX_SYMBOL_FLOATING_LOSS_CRYPTO_USD": "200",
        "MT5_MAX_SYMBOL_FLOATING_LOSS_METALS_USD": "35",
        "MT5_MAX_ACCOUNT_FLOATING_LOSS_USD": "1000",
        "MT5_MAX_DAILY_LOSS_USD": "1000",
        "MT5_DAILY_LOSS_OVERRIDE_USD": "1000",
        "MT5_MAX_DAILY_LOSING_STREAK": "10",
        "MT5_LOSS_COUNT_MIN_USD": "5",
        "MT5_LOSS_COUNT_MIN_USD_XAUUSD": "5",
        "MT5_STARTUP_SYMBOL": args.startup_symbol,
        "MT5_SYMBOL_ALIASES": "XAUUSD:GOLD",
    }
    write_file(env_file, env_lines(env), args.apply, mode=0o600, force=args.force)

    bot_unit = f"""[Unit]
Description=Cipher FX isolated MT5 bot for {args.user_name}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={app_dir}
Environment=HOME=/root
Environment=USER=root
Environment=APP_DIR={app_dir}
Environment=ENV_FILE={env_file}
EnvironmentFile={env_file}
ExecStart=/usr/bin/env bash {app_dir}/ops/start_mt5_stack.sh
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
"""
    dashboard_unit = f"""[Unit]
Description=Cipher FX isolated MT5 dashboard API for {args.user_name}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
Group=root
WorkingDirectory={app_dir}/dashboard/mt5_backend
Environment=APP_DIR={app_dir}
Environment=ENV_FILE={env_file}
EnvironmentFile={env_file}
ExecStart={app_dir}/.venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port {dashboard_port}
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
"""
    write_file(SERVICE_ROOT / bot_service, bot_unit, args.apply, force=args.force)
    write_file(SERVICE_ROOT / dashboard_service, dashboard_unit, args.apply, force=args.force)

    manifest = {
        "client_name": args.user_name,
        "client_slug": slug,
        "platform": platform,
        "app_dir": str(app_dir),
        "state_dir": str(state_dir),
        "terminal_prefix": str(terminal_prefix),
        "bridge_dir": str(bridge_dir),
        "env_file": str(env_file),
        "bot_service": bot_service,
        "dashboard_service": dashboard_service,
        "dashboard_port": dashboard_port,
        "display_num": display_num,
        "magic": magic,
        "dry_run": args.dry_run,
        "start_after_create": args.start,
    }
    write_file(client_dir / "client_manifest.json", json.dumps(manifest, indent=2) + "\n", args.apply, force=args.force)

    if args.apply:
        run(["systemctl", "daemon-reload"], True)
        if args.start:
            run(["systemctl", "enable", "--now", bot_service, dashboard_service], True)
        print(json.dumps(manifest, indent=2))
    else:
        print("\nPreview only. Re-run with --apply to create this isolated account.")
        print(json.dumps(manifest, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Provision an isolated Cipher FX client account on the VPS.")
    parser.add_argument("--user-name", required=True, help="Client/user display name")
    parser.add_argument("--slug", help="Optional stable folder/service slug; defaults from user name")
    parser.add_argument("--platform", default="mt5", choices=["mt5"], help="Platform template to create")
    parser.add_argument("--login", required=True, help="MT5 demo/login account id")
    parser.add_argument("--server", required=True, help="MT5 server name")
    parser.add_argument("--password-file", help="Root-only file containing the MT5 password")
    parser.add_argument("--dashboard-port", type=int, help="Optional dashboard API port; default finds a free port")
    parser.add_argument("--display-num", help="Optional X display, e.g. :100; default finds a free display")
    parser.add_argument("--magic", type=int, help="Optional MT5 magic number; default derives a unique value")
    parser.add_argument("--startup-symbol", default="EURUSD")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Create account in MT5_DRY_RUN=1 mode")
    parser.add_argument("--live-demo-routing", dest="dry_run", action="store_false", help="Set MT5_DRY_RUN=0 for demo execution")
    parser.add_argument("--skip-terminal-copy", action="store_true", help="Do not copy the MT5 Wine prefix yet")
    parser.add_argument("--start", action="store_true", help="Enable and start the new isolated services")
    parser.add_argument("--force", action="store_true", help="Allow overwriting an existing same-slug provision")
    parser.add_argument("--apply", action="store_true", help="Actually create files; omitted means preview only")
    args = parser.parse_args()

    if os.geteuid() != 0:
        raise SystemExit("Run this on the VPS as root.")
    if not SOURCE_APP.exists():
        raise SystemExit(f"Missing source app: {SOURCE_APP}")
    if not SOURCE_TERMINAL_PREFIX.exists() and not args.skip_terminal_copy:
        raise SystemExit(f"Missing source terminal prefix: {SOURCE_TERMINAL_PREFIX}")
    provision_mt5(args)


if __name__ == "__main__":
    main()
