from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_bool(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name, str(int(default)))).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)) or default)
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default)) or default))
    except Exception:
        return int(default)


def _clean_symbol_list(values) -> list[str]:
    if not isinstance(values, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        sym = str(value or "").strip().upper()
        if not sym or sym in seen:
            continue
        seen.add(sym)
        out.append(sym)
    return out


def _clean_symbol_aliases(values) -> dict[str, str]:
    if not isinstance(values, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in values.items():
        canonical = str(key or "").strip().upper()
        resolved = str(value or "").strip()
        if canonical and resolved:
            out[canonical] = resolved
    return out


def _env_symbol_aliases(name: str) -> dict[str, str]:
    raw = os.getenv(name, "")
    aliases: dict[str, str] = {}
    for item in raw.split(","):
        sep = "=" if "=" in item else ":" if ":" in item else ""
        if not sep:
            continue
        key, value = item.split(sep, 1)
        canonical = key.strip().upper()
        resolved = value.strip()
        if canonical and resolved:
            aliases[canonical] = resolved
    return aliases


@dataclass
class MT5RuntimeConfig:
    login: int = int(os.getenv("MT5_LOGIN", "0") or "0")
    live_login: int = int(os.getenv("MT5_LIVE_LOGIN", "0") or "0")
    password: str = os.getenv("MT5_PASSWORD", "")
    server: str = os.getenv("MT5_SERVER", "XMGlobal-MT5 7")
    terminal_path: str | None = os.getenv("MT5_TERMINAL_PATH") or None
    symbol_suffix: str = os.getenv("MT5_SYMBOL_SUFFIX", "")
    trade_mode: str = os.getenv("MT5_TRADE_MODE", "paper").strip().lower()
    magic: int = int(os.getenv("MT5_MAGIC", "260626"))
    slippage_points: int = int(os.getenv("MT5_DEVIATION_POINTS", "20"))
    poll_seconds: float = _env_float("MT5_POLL_SECONDS", 1.0)
    scan_seconds: int = _env_int("MT5_SCAN_SECONDS", 890)
    history_bars: int = int(os.getenv("MT5_HISTORY_BARS", "220"))
    dry_run: bool = _env_bool("MT5_DRY_RUN", False)
    demo_balance: float = float(os.getenv("MT5_DEMO_BALANCE", "0") or "0")
    capital_cap_usd: float = _env_float("MT5_CAPITAL_CAP_USD", 5000.0)
    max_position_pct: float = _env_float("MT5_MAX_POSITION_PCT", 1.0)
    max_open_trades: int = _env_int("MT5_MAX_OPEN_TRADES", 30)
    max_daily_trades: int = _env_int("MT5_MAX_DAILY_TRADES", 1000)
    loss_cooldown_seconds: int = _env_int("MT5_LOSS_COOLDOWN_SECONDS", 600)
    daily_loss_limit_usd: float = _env_float(
        "MT5_LIVE_DAILY_LOSS_LIMIT_USD"
        if os.getenv("MT5_TRADE_MODE", "paper").strip().lower() == "live"
        else "MT5_MAX_DAILY_LOSS_USD",
        5000.0 if os.getenv("MT5_TRADE_MODE", "paper").strip().lower() == "live" else 10000.0,
    )
    max_trades_per_symbol: int = _env_int("MT5_MAX_DAILY_TRADES_PER_SYMBOL", 60)
    max_xauusd_losses_per_day: int = _env_int("MT5_MAX_XAUUSD_LOSSES_PER_DAY", 2)
    # Prevent opposite-direction bursts on the same symbol after an entry.
    symbol_reentry_cooldown_seconds: int = _env_int("MT5_SYMBOL_REENTRY_COOLDOWN_SECONDS", 900)
    max_pyramid_trades: int = _env_int("MT5_MAX_PYRAMID_TRADES", 4)
    execution_mode: str = (os.getenv("MT5_EXECUTION_MODE", "bridge").strip().lower() or "bridge")
    state_file: Path = Path(os.getenv("MT5_STATE_FILE", "mt5_runtime_state.json"))
    symbols_file: Path = Path(os.getenv("MT5_SYMBOLS_FILE", "mt5_symbols.json"))
    bridge_dir: Path = Path(os.getenv("MT5_BRIDGE_DIR", "mt5_bridge"))
    dashboard_broker_label: str = os.getenv("CIPHERFX_ACCOUNT_LABEL", "XM Global MT5")
    enabled_markets: str = os.getenv(
        "MT5_MARKETS",
        "forex,metals,energies,crypto,indices,stocks,etfs",
    )
    # Was ["USDJPY","USDCAD","EURJPY"]. mt5_symbols.json says "forex": [], but the
    # loader below skips empty lists, so the default silently survived - leaving
    # three symbols SIMPLE does not claim, which fell through the router to the
    # old FADE/SDZONE/ZONE/ORB/CRT/FOREX stack. Removed 2026-08-14.
    forex_symbols: list[str] = field(default_factory=list)
    metals_symbols: list[str] = field(default_factory=list)
    energies_symbols: list[str] = field(default_factory=list)
    crypto_symbols: list[str] = field(default_factory=list)
    indices_symbols: list[str] = field(default_factory=list)
    stocks_symbols: list[str] = field(default_factory=list)
    etfs_symbols: list[str] = field(default_factory=list)
    symbol_aliases: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.symbols_file.exists():
            try:
                payload = json.loads(self.symbols_file.read_text())
            except Exception:
                payload = {}
            for key, attr in (
                ("forex", "forex_symbols"),
                ("metals", "metals_symbols"),
                ("energies", "energies_symbols"),
                ("crypto", "crypto_symbols"),
                ("indices", "indices_symbols"),
                ("stocks", "stocks_symbols"),
                ("etfs", "etfs_symbols"),
            ):
                values = _clean_symbol_list(payload.get(key))
                if values:
                    setattr(self, attr, values)
            self.symbol_aliases.update(_clean_symbol_aliases(payload.get("aliases")))
        self.symbol_aliases.update(_env_symbol_aliases("MT5_SYMBOL_ALIASES"))
        self.execution_mode = self.execution_mode if self.execution_mode in {"auto", "native", "bridge"} else "auto"
        self.trade_mode = self.trade_mode if self.trade_mode in {"paper", "demo", "live"} else "paper"
        self.capital_cap_usd = max(0.0, float(self.capital_cap_usd or 0.0))
        self.max_position_pct = min(1.25, max(0.01, float(self.max_position_pct or 1.0)))
        self.max_open_trades = max(1, int(self.max_open_trades or 1))
        self.max_daily_trades = max(1, int(self.max_daily_trades or 1))
        self.symbol_reentry_cooldown_seconds = max(0, int(self.symbol_reentry_cooldown_seconds or 0))
        self.poll_seconds = max(0.1, float(self.poll_seconds or 1.0))
        # Was max(60, ...) - a hard floor that silently raised any configured
        # scan interval to 60s. MT5_SCAN_SECONDS has read 30 since 2026-08-13
        # and was never honoured. Measured 2026-08-14: 63.6s per symbol, while
        # MT5_PROPOSAL_TTL_SECONDS=10 expired every setup after 10s - so the
        # bot spent ~54s of every minute with nothing live and no way to
        # re-derive. Floor lowered so the configured value is respected.
        self.scan_seconds = max(1, int(self.scan_seconds or 890))

    def resolved_symbol(self, canonical: str) -> str:
        code = str(canonical or "").strip().upper()
        resolved = self.symbol_aliases.get(code, code)
        suffix = self.symbol_suffix.upper()
        if suffix and not resolved.upper().endswith(suffix):
            resolved = f"{resolved}{suffix}"
        return resolved

    def canonical_symbol(self, resolved_symbol: str) -> str:
        resolved = str(resolved_symbol or "").strip().upper()
        suffix = self.symbol_suffix.upper()
        if suffix and resolved.endswith(suffix):
            resolved = resolved[: -len(suffix)]
        for canonical, alias in self.symbol_aliases.items():
            if str(alias or "").strip().upper() == resolved:
                return canonical
        return resolved

    @property
    def enabled_market_set(self) -> set[str]:
        raw = {item.strip().lower() for item in str(self.enabled_markets or "").split(",") if item.strip()}
        if not raw or "all" in raw:
            return {"forex", "metals", "energies", "crypto", "indices", "stocks", "etfs"}
        return raw

    def symbol_groups(self) -> dict[str, list[str]]:
        groups = {
            "forex": self.forex_symbols,
            "metals": self.metals_symbols,
            "energies": self.energies_symbols,
            "crypto": self.crypto_symbols,
            "indices": self.indices_symbols,
            "stocks": self.stocks_symbols,
            "etfs": self.etfs_symbols,
        }
        enabled = self.enabled_market_set
        return {group: symbols for group, symbols in groups.items() if group in enabled and symbols}

    def all_symbols(self) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for symbols in self.symbol_groups().values():
            for sym in symbols:
                if sym not in seen:
                    seen.add(sym)
                    out.append(sym)
        return out

    def group_for_symbol(self, symbol: str) -> str:
        canonical = self.canonical_symbol(symbol)
        for group, symbols in self.symbol_groups().items():
            if canonical in symbols:
                return group
        return "unknown"

    @property
    def commands_dir(self) -> Path:
        return self.bridge_dir / "commands"

    @property
    def results_dir(self) -> Path:
        return self.bridge_dir / "results"
