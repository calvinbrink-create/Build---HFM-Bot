#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path


DEFAULT_BRIDGE = Path("/root/.mt5/drive_c/Program Files/MetaTrader 5/MQL5/Files/cipherfx")
DEFAULT_CONFIG = Path("/opt/cipherfx_mt5/mt5_symbols.json")


def _ticker_from_description(description: str) -> str:
    match = re.search(r"\(([^)]+)\)", description or "")
    if not match:
        return ""
    return match.group(1).split(".")[0].upper().replace("_", "").replace("-", "")


def _broker_lookup(symbols_csv: Path) -> dict[str, dict]:
    lookup: dict[str, dict] = {}
    with symbols_csv.open(newline="") as handle:
        for row in csv.DictReader(handle):
            try:
                if int(float(row.get("trade_mode") or 0)) <= 0:
                    continue
            except Exception:
                continue
            path = str(row.get("path") or "")
            if not path.startswith("Stocks\\"):
                continue
            ticker = _ticker_from_description(str(row.get("description") or ""))
            if not ticker:
                continue
            lookup.setdefault(ticker, row)
    return lookup


def _broker_symbols(symbols_csv: Path) -> set[str]:
    symbols: set[str] = set()
    with symbols_csv.open(newline="") as handle:
        for row in csv.DictReader(handle):
            try:
                if int(float(row.get("trade_mode") or 0)) <= 0:
                    continue
            except Exception:
                continue
            symbol = str(row.get("symbol") or "")
            if symbol:
                symbols.add(symbol)
    return symbols


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit MT5 US stock/ETF symbols against broker catalogue.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--bridge", type=Path, default=DEFAULT_BRIDGE)
    args = parser.parse_args()

    config = json.loads(args.config.read_text())
    aliases = {str(k).upper(): str(v) for k, v in (config.get("aliases") or {}).items()}
    rates = {p.name[len("rates_") : -len("_M15.csv")] for p in args.bridge.glob("rates_*_M15.csv")}
    broker = _broker_lookup(args.bridge / "symbols.csv")
    broker_symbols = _broker_symbols(args.bridge / "symbols.csv")

    print(f"Config: {args.config}")
    print(f"Bridge: {args.bridge}")
    print(f"Broker stock catalogue: {len(broker)} tickers")
    print(f"M15 rate files: {len(rates)}")

    missing_rates: list[str] = []
    missing_broker: list[str] = []
    for symbol in config.get("stocks", []):
        canonical = str(symbol).upper()
        resolved = aliases.get(canonical, symbol)
        key = canonical.replace(".", "").replace("-", "")
        if key not in broker and str(resolved) not in broker_symbols and str(resolved) not in rates:
            missing_broker.append(canonical)
        if str(resolved) not in rates:
            missing_rates.append(f"{canonical}->{resolved}")

    missing_etfs = [str(symbol).upper() for symbol in config.get("etfs", [])]

    print(f"Configured stocks: {len(config.get('stocks', []))}")
    print(f"Configured ETFs: {len(config.get('etfs', []))}")
    print(f"Stocks without current M15 rate file: {len(missing_rates)}")
    if missing_rates:
        print("Missing/currently not refreshed:")
        for item in missing_rates[:120]:
            print(f"  {item}")
    if missing_broker:
        print("Not found in broker stock catalogue:")
        for item in missing_broker:
            print(f"  {item}")
    if missing_etfs:
        print("ETFs configured, but XM catalogue should be checked separately:")
        for item in missing_etfs:
            print(f"  {item}")
    return 1 if missing_broker else 0


if __name__ == "__main__":
    raise SystemExit(main())
