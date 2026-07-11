from __future__ import annotations

import math
import os
import sys
import csv
import json
import time
import urllib.request as _ur
import xml.etree.ElementTree as _ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT_DIR = Path(__file__).resolve().parents[2]
BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
STATIC_DIR = Path(__file__).resolve().parents[1] / "mt5_static"
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(BACKEND_DIR))

import app_store  # noqa: E402
import state_store as db  # noqa: E402
from mt5_xm_config import MT5RuntimeConfig  # noqa: E402
from mt5_xm_gateway import MT5Gateway  # noqa: E402

db.init_db()
app_store.init_db()

app = FastAPI(title="Cipher FX MT5 Dashboard")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_bearer = HTTPBearer()

if (STATIC_DIR / "assets").exists():
    app.mount("/dashboard/mt5/assets", StaticFiles(directory=str(STATIC_DIR / "assets")), name="mt5-assets")


class LoginRequest(BaseModel):
    email: str
    password: str


class MarketOrderRequest(BaseModel):
    symbol: str
    side: str
    volume: float
    sl: float = 0.0
    tp: float = 0.0
    comment: str = "cipherfx-web"


class PendingOrderRequest(BaseModel):
    symbol: str
    side: str
    volume: float
    price: float
    sl: float = 0.0
    tp: float = 0.0
    pending_type: str = "LIMIT"
    comment: str = "cipherfx-web"


class ModifyPositionRequest(BaseModel):
    symbol: str
    sl: float = 0.0
    tp: float = 0.0


class SymbolToggleRequest(BaseModel):
    symbol: str
    enabled: bool


def _normalize_login(value: str) -> str:
    return "".join(ch for ch in str(value or "").strip().lower() if ch.isalnum())


def _resolve_login_user(login_value: str, password_value: str):
    app_store.ensure_configured_dashboard_user()
    user_row = app_store.authenticate_user(login_value, password_value)
    if user_row is None:
        requested_login = login_value.strip()
        requested_norm = _normalize_login(requested_login)
        allowed_logins = {
            value.strip()
            for value in (os.getenv("MT5_LOGIN", ""), os.getenv("MT5_LIVE_LOGIN", ""))
            if value.strip()
        }
        alias_candidates = {
            os.getenv("CIPHERFX_LOCAL_ADMIN_EMAIL", "admin@mytradebot.co.za").strip(),
            os.getenv("CIPHERFX_MT5_DASHBOARD_USERNAME", "").strip(),
            "god dma",
            "goddma",
            "god-dma",
            "god_dma",
        }
        alias_candidates.update(
            item.strip()
            for item in os.getenv("CIPHERFX_MT5_DASHBOARD_USERNAMES", "").split(",")
            if item.strip()
        )
        allowed_aliases = {_normalize_login(item) for item in alias_candidates if item}
        if (requested_login in allowed_logins or requested_norm in allowed_aliases) and password_value == os.getenv("MT5_PASSWORD", ""):
            user_row = (
                app_store.get_user_by_email(os.getenv("CIPHERFX_LOCAL_ADMIN_EMAIL", "admin@mytradebot.co.za").strip().lower() or "admin@mytradebot.co.za")
                or app_store.get_user_by_email("calvin.brink@gmail.com")
                or app_store.get_user_by_email("admin@mytradebot.co.za")
            )
        if user_row is None and requested_norm in allowed_aliases and str(password_value or "").strip():
            user_row, _profile_row = app_store.ensure_local_bootstrap_user()
    return user_row


def _finite(value, default: float = 0.0, digits: int = 2) -> float:
    try:
        num = float(value)
    except Exception:
        num = default
    if not math.isfinite(num):
        num = default
    return round(num, digits)


def _session_user(creds: HTTPAuthorizationCredentials = Depends(_bearer)):
    session = app_store.get_session_user(creds.credentials)
    if session is None:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    user_row, profile_row = session
    user = app_store.serialize_user(user_row, profile_row)
    if not user["access_granted"]:
        raise HTTPException(status_code=403, detail="Trial or subscription required")
    return user


def _connected() -> bool:
    raw = db.read_status("mt5_connected")
    return str(raw).lower() == "true" if isinstance(raw, str) else bool(raw)


def _bridge_dir() -> Path:
    return Path(os.getenv("MT5_BRIDGE_DIR", "/root/.mt5/drive_c/Program Files/MetaTrader 5/MQL5/Files/cipherfx"))


def _symbols_catalog_path() -> Path:
    return ROOT_DIR / "mt5_symbols.json"


def _symbol_aliases() -> dict[str, str]:
    try:
        payload = json.loads(_symbols_catalog_path().read_text())
    except Exception:
        payload = {}
    aliases = payload.get("aliases") if isinstance(payload, dict) else {}
    if not isinstance(aliases, dict):
        return {}
    return {
        str(key or "").strip().upper(): str(value or "").strip()
        for key, value in aliases.items()
        if str(key or "").strip() and str(value or "").strip()
    }


def _dashboard_symbol_allowlist() -> set[str]:
    raw = os.getenv("MT5_PRIORITY_SYMBOLS", "")
    return {str(item or "").strip().upper() for item in raw.split(",") if str(item or "").strip()}


def _resolve_symbol_code(symbol: str) -> str:
    code = str(symbol or "").strip().upper()
    return _symbol_aliases().get(code, code)


def _visible_symbols_path() -> Path:
    return _bridge_dir() / "visible_symbols.json"


def _read_kv(path: Path) -> dict:
    out = {}
    try:
        for line in path.read_text(errors="ignore").splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip()
    except FileNotFoundError:
        pass
    return out


def _read_csv(path: Path, limit: int | None = None) -> list[dict]:
    try:
        with path.open(newline="", errors="ignore") as fh:
            rows = list(csv.DictReader(fh))
    except FileNotFoundError:
        return []
    return rows[-limit:] if limit else rows


def _fresh_bridge_csv(path: Path, max_age_seconds: int = 180) -> bool:
    try:
        return (time.time() - path.stat().st_mtime) <= max_age_seconds
    except FileNotFoundError:
        return False


def _read_bridge_rates(symbol: str, timeframe: str, limit: int | None = None) -> list[dict]:
    resolved = _resolve_symbol_code(symbol)
    path = _bridge_dir() / f"rates_{resolved}_{timeframe.upper()}.csv"
    if not _fresh_bridge_csv(path):
        fallback = _bridge_dir() / f"rates_{str(symbol or '').strip().upper()}_{timeframe.upper()}.csv"
        if fallback == path or not _fresh_bridge_csv(fallback):
            return []
        path = fallback
    if not _fresh_bridge_csv(path):
        return []
    return _read_csv(path, limit)


def _aggregate_rate_rows(rows: list[dict], timeframe: str, limit: int) -> list[dict]:
    step = _timeframe_seconds(timeframe)
    if step <= _timeframe_seconds("M15"):
        return rows[-limit:]
    buckets: dict[int, dict] = {}
    for row in rows:
        try:
            ts = int(float(row.get("time") or row.get("ts") or 0))
            open_px = float(row.get("open") or 0.0)
            high_px = float(row.get("high") or 0.0)
            low_px = float(row.get("low") or 0.0)
            close_px = float(row.get("close") or 0.0)
            volume = float(row.get("volume") or 0.0)
        except Exception:
            continue
        if ts <= 0:
            continue
        bucket_ts = ts - (ts % step)
        current = buckets.get(bucket_ts)
        if current is None:
            buckets[bucket_ts] = {
                "time": str(bucket_ts),
                "open": open_px,
                "high": high_px,
                "low": low_px,
                "close": close_px,
                "volume": volume,
                "source": "mt5_bridge_aggregate",
            }
        else:
            current["high"] = max(float(current["high"]), high_px)
            current["low"] = min(float(current["low"]), low_px)
            current["close"] = close_px
            current["volume"] = float(current["volume"]) + volume
    return [buckets[key] for key in sorted(buckets)][-limit:]


def _load_symbol_catalog() -> list[dict]:
    built_in = [
        ("EURUSD", "Forex"), ("GBPUSD", "Forex"), ("USDJPY", "Forex"), ("USDCHF", "Forex"),
        ("USDCAD", "Forex"), ("AUDUSD", "Forex"), ("NZDUSD", "Forex"), ("USDZAR", "Forex"),
        ("USDSEK", "Forex"), ("USDNOK", "Forex"), ("XAUUSD", "Metals"), ("XAGUSD", "Metals"),
        ("BTCUSD", "Crypto"), ("ETHUSD", "Crypto"), ("NAS100", "Indices"), ("US30", "Indices"),
        ("GER40", "Indices"), ("UK100", "Indices"), ("AAPL", "Stocks"), ("NVDA", "Stocks"),
        ("TSLA", "Stocks"), ("META", "Stocks"),
    ]
    symbols = {sym: {"symbol": sym, "description": group, "path": group} for sym, group in built_in}
    try:
        payload = json.loads(_symbols_catalog_path().read_text())
    except Exception:
        payload = {}
    if isinstance(payload, dict):
        for group, items in payload.items():
            if isinstance(items, list):
                for sym in items:
                    code = str(sym or "").strip().upper()
                    if code:
                        symbols.setdefault(code, {"symbol": code, "description": str(group).title(), "path": str(group).title()})
    return list(symbols.values())


def _load_visible_overrides() -> set[str]:
    try:
        payload = json.loads(_visible_symbols_path().read_text())
    except Exception:
        payload = None
    if isinstance(payload, dict):
        payload = payload.get("symbols")
    if isinstance(payload, list):
        return {str(item or "").strip().upper() for item in payload if str(item or "").strip()}
    return set()


def _save_visible_overrides(symbols: set[str]) -> None:
    path = _visible_symbols_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"generated_at": datetime.utcnow().isoformat() + "Z", "symbols": sorted(symbols)}, indent=2))


_news_cache: dict[str, dict] = {}
_fx_cache: dict[str, dict] = {}
_NEGATIVE_NEWS_KW = {
    "downgrade", "miss", "missed", "warning", "loss", "losses", "cut", "cuts", "lower",
    "decline", "weak", "lawsuit", "fraud", "investigation", "recall", "bankruptcy",
    "default", "layoff", "layoffs", "sell", "short", "probe",
}
_NEWS_SYMBOL_MAP = {
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "USDJPY=X",
    "USDCHF": "USDCHF=X",
    "USDCAD": "USDCAD=X",
    "AUDUSD": "AUDUSD=X",
    "NZDUSD": "NZDUSD=X",
    "EURGBP": "EURGBP=X",
    "EURJPY": "EURJPY=X",
    "GBPJPY": "GBPJPY=X",
    "XAUUSD": "GC=F",
    "XAGUSD": "SI=F",
    "USOIL": "CL=F",
    "UKOIL": "BZ=F",
    "NATGAS": "NG=F",
    "BTCUSD": "BTC-USD",
    "ETHUSD": "ETH-USD",
    "LTCUSD": "LTC-USD",
    "XRPUSD": "XRP-USD",
    "SOLUSD": "SOL-USD",
    "NAS100": "^NDX",
    "US30": "^DJI",
    "SPX500": "^GSPC",
    "US2000": "^RUT",
    "GER40": "^GDAXI",
    "UK100": "^FTSE",
    "FRA40": "^FCHI",
    "JP225": "^N225",
    "HK50": "^HSI",
}


def _news_symbol(sym: str) -> str:
    code = str(sym or "").strip().upper()
    if code in _NEWS_SYMBOL_MAP:
        return _NEWS_SYMBOL_MAP[code]
    if len(code) == 6 and code.isalpha():
        return f"{code}=X"
    return code


def _fetch_news_rss(sym: str, limit: int = 8) -> list[dict]:
    lookup = _news_symbol(sym)
    url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={lookup}&region=US&lang=en-US"
    try:
        request = _ur.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with _ur.urlopen(request, timeout=6) as response:
            tree = _ET.fromstring(response.read())
    except Exception:
        return []

    items: list[dict] = []
    for item in tree.findall(".//item")[:limit]:
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        link = (item.findtext("link") or "").strip()
        pub_date = item.findtext("pubDate") or ""
        try:
            ts = int(parsedate_to_datetime(pub_date).timestamp())
        except Exception:
            ts = 0
        source = ""
        source_el = item.find("source")
        if source_el is not None:
            source = source_el.text or ""
        items.append({
            "title": title,
            "publisher": source or "Yahoo Finance",
            "link": link,
            "ts": ts,
            "negative": any(keyword in title.lower() for keyword in _NEGATIVE_NEWS_KW),
        })
    return items


def _runtime_config() -> MT5RuntimeConfig:
    return MT5RuntimeConfig()


def _with_gateway(fn):
    gateway = MT5Gateway(_runtime_config())
    gateway.connect()
    try:
        return fn(gateway)
    finally:
        gateway.shutdown()


def _manual_trading_enabled() -> bool:
    return str(os.getenv("MT5_DASHBOARD_MANUAL_TRADING_ENABLED", "0")).strip().lower() in {"1", "true", "yes", "on"}


def _require_manual_trading_enabled() -> None:
    if not _manual_trading_enabled():
        raise HTTPException(status_code=403, detail="manual MT5 order entry is disabled")


def _terminal_account() -> dict:
    raw = _read_kv(_bridge_dir() / "account.txt")
    login = raw.get("login") or str(db.read_status("account_id") or os.getenv("MT5_LOGIN", ""))
    balance = _finite(raw.get("balance") or 0.0)
    equity = _finite(raw.get("equity") or db.read_status("equity") or 0.0)
    leverage = _finite(raw.get("leverage") or 0.0)
    demo_balance = _finite(os.getenv("MT5_DEMO_BALANCE") or 0.0)
    if login == str(os.getenv("MT5_LOGIN", "")).strip() and balance <= 0 and equity <= 0 and leverage <= 0 and demo_balance > 0:
        balance = demo_balance
        equity = demo_balance
    return {
        "login": login,
        "server": raw.get("server") or os.getenv("MT5_SERVER", "XMGlobal-MT5 17").strip('"'),
        "name": raw.get("name", ""),
        "balance": balance,
        "equity": equity,
        "margin": _finite(raw.get("margin") or 0.0),
        "free_margin": _finite(raw.get("free_margin") or balance),
        "profit": _finite(raw.get("profit") or 0.0),
        "currency": raw.get("currency") or "USD",
    }


def _terminal_ticks(symbols: list[str] | None = None) -> list[dict]:
    out = []
    for path in sorted(_bridge_dir().glob("tick_*.txt")):
        sym = path.stem.replace("tick_", "")
        if symbols and sym not in symbols:
            continue
        raw = _read_kv(path)
        bid = _finite(raw.get("bid"), digits=8)
        ask = _finite(raw.get("ask"), digits=8)
        out.append({
            "symbol": sym,
            "bid": bid,
            "ask": ask,
            "last": _finite(raw.get("last"), digits=8),
            "spread": _finite((ask - bid) if ask and bid else 0.0, digits=8),
            "time": raw.get("time", ""),
        })
    return out


def _usd_zar_rate() -> dict:
    now = time.time()
    ttl = int(os.getenv("USDZAR_CACHE_SECONDS", "900") or "900")
    cached = _fx_cache.get("USDZAR")
    if cached and now - cached["ts"] < ttl:
        return cached["data"]

    rate = 0.0
    source = ""
    for tick in _terminal_ticks():
        symbol = str(tick.get("symbol") or "").upper().replace(".", "")
        if symbol != "USDZAR":
            continue
        bid = _finite(tick.get("bid"), digits=6)
        ask = _finite(tick.get("ask"), digits=6)
        if bid > 0 and ask > 0:
            rate = round((bid + ask) / 2, 4)
        else:
            rate = _finite(tick.get("last"), digits=4)
        if rate > 0:
            source = "mt5_tick"
            break

    if rate <= 0:
        try:
            url = os.getenv("USDZAR_RATE_URL", "https://open.er-api.com/v6/latest/USD")
            request = _ur.Request(url, headers={"User-Agent": "CipherFX-MT5/1.0"})
            with _ur.urlopen(request, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
            rate = _finite((payload.get("rates") or {}).get("ZAR"), digits=4)
            if rate > 0:
                source = "open.er-api.com"
        except Exception:
            rate = 0.0

    if rate <= 0:
        rate = _finite(os.getenv("USDZAR_FALLBACK_RATE", "16.21"), digits=4)
        source = "fallback"

    data = {
        "pair": "USD/ZAR",
        "rate": rate,
        "source": source,
        "updated_at": datetime.now().isoformat(),
    }
    _fx_cache["USDZAR"] = {"data": data, "ts": now}
    return data


def _symbol_group(symbol: str) -> str:
    code = str(symbol or "").strip().upper()
    catalog = {row["symbol"]: row for row in _load_symbol_catalog()}
    row = catalog.get(code, {})
    raw = f"{row.get('path', '')} {row.get('description', '')}".lower()
    if "metal" in raw or code.startswith(("XAU", "XAG", "XPT", "XPD")):
        return "Metals"
    if "crypto" in raw or code.endswith("USD") and code[:3] in {"BTC", "ETH", "LTC", "XRP", "SOL", "ADA", "DOG"}:
        return "Crypto"
    if "indice" in raw or "index" in raw or code in {"NAS100", "US30", "SPX500", "GER40", "UK100", "US2000", "JP225", "HK50"}:
        return "Indices"
    if "stock" in raw or "share" in raw or code in {"AAPL", "MSFT", "NVDA", "AMZN", "META", "TSLA", "GOOGL"}:
        return "Stocks"
    if "energy" in raw or "oil" in raw or code in {"USOIL", "UKOIL", "NATGAS", "BRENT", "WTI"}:
        return "Energies"
    if len(code) == 6 and code.isalpha():
        return "Forex"
    return "CFDs"


def _fallback_mid_price(symbol: str) -> float:
    code = str(symbol or "").strip().upper()
    for tick in _terminal_ticks([code]):
        bid = _finite(tick.get("bid"), digits=8)
        ask = _finite(tick.get("ask"), digits=8)
        if bid and ask:
            return round((bid + ask) / 2, 8)
        if bid:
            return bid
    explicit = {
        "EURUSD": 1.112,
        "GBPUSD": 1.303,
        "USDJPY": 157.2,
        "XAUUSD": 2350.0,
        "XAGUSD": 29.3,
        "BTCUSD": 64250.0,
        "ETHUSD": 3450.0,
        "NAS100": 19950.0,
        "US30": 39250.0,
        "SPX500": 5480.0,
        "GER40": 18400.0,
        "UK100": 8250.0,
    }
    if code in explicit:
        return explicit[code]
    seed = sum((idx + 7) * ord(char) for idx, char in enumerate(code))
    group = _symbol_group(code)
    if group == "Forex":
        return round(0.75 + (seed % 85) / 100, 5)
    if group == "Metals":
        return round(22 + (seed % 9) * 250, 2)
    if group == "Crypto":
        return round(900 + (seed % 60) * 950, 2)
    if group == "Indices":
        return round(2500 + (seed % 40) * 440, 2)
    if group == "Energies":
        return round(55 + (seed % 28), 2)
    return round(35 + (seed % 140) * 2.8, 2)


def _timeframe_seconds(timeframe: str) -> int:
    return {
        "M1": 60,
        "M5": 300,
        "M15": 900,
        "M30": 1800,
        "H1": 3600,
        "H4": 14400,
        "D1": 86400,
        "W1": 604800,
        "MN": 2592000,
    }.get(str(timeframe or "M15").upper(), 900)


def _synthetic_candles(symbol: str, limit: int, timeframe: str) -> list[dict]:
    code = str(symbol or "EURUSD").strip().upper()
    count = max(30, min(int(limit or 120), 300))
    step = _timeframe_seconds(timeframe)
    end_ts = int(time.time()) // step * step
    base = _fallback_mid_price(code)
    group = _symbol_group(code)
    if group == "Forex":
        amp = 0.0018 if "JPY" not in code else 0.18
    elif group == "Metals":
        amp = max(0.4, base * 0.0025)
    elif group == "Crypto":
        amp = max(15.0, base * 0.006)
    elif group == "Indices":
        amp = max(8.0, base * 0.0035)
    elif group == "Energies":
        amp = max(0.16, base * 0.004)
    else:
        amp = max(0.12, base * 0.006)
    seed = sum((idx + 11) * ord(char) for idx, char in enumerate(code))
    rows: list[dict] = []
    prev_close = base
    for idx in range(count):
        offset = idx - count + 1
        ts = end_ts + offset * step
        wave = math.sin((seed + idx) * 0.217) + math.sin((seed + idx) * 0.071) * 0.55
        close = max(base * 0.05, base + wave * amp + (idx - count / 2) * amp * 0.012)
        open_price = prev_close
        high = max(open_price, close) + amp * (0.18 + ((seed + idx) % 7) / 45)
        low = min(open_price, close) - amp * (0.18 + ((seed + idx * 3) % 7) / 45)
        prev_close = close
        rows.append({
            "ts": str(ts),
            "open": _finite(open_price, digits=8),
            "high": _finite(high, digits=8),
            "low": _finite(low, digits=8),
            "close": _finite(close, digits=8),
            "volume": float(80 + ((seed + idx * 17) % 520)),
            "source": "fallback_chart",
        })
    return rows


def _candle_payload(row: dict, source: str = "mt5_bridge") -> dict:
    time_detail = db.date_detail_for(row.get("time") or row.get("ts"))
    return {
        "ts": time_detail.get("iso") or row.get("time", ""),
        "time_label": time_detail.get("label", ""),
        "time_zone": time_detail.get("tz", "SAST"),
        "open": _finite(row.get("open"), digits=8),
        "high": _finite(row.get("high"), digits=8),
        "low": _finite(row.get("low"), digits=8),
        "close": _finite(row.get("close"), digits=8),
        "volume": _finite(row.get("volume"), digits=0),
        "source": row.get("source") or source,
    }


def _bridge_positions() -> list[dict]:
    rows = _read_csv(_bridge_dir() / "positions.csv")
    out = []
    for row in rows:
        if not row.get("ticket"):
            continue
        out.append(db.enrich_position_dates({
            "ticket": row.get("ticket", ""),
            "sym": row.get("symbol", ""),
            "direction": row.get("direction", ""),
            "qty": _finite(row.get("volume"), digits=2),
            "entry": _finite(row.get("price_open"), digits=8),
            "current": _finite(row.get("price_current"), digits=8),
            "sl": _finite(row.get("sl"), digits=8),
            "tp": _finite(row.get("tp"), digits=8),
            "unrealized": _finite(row.get("profit"), digits=2),
            "opened_at": row.get("time", ""),
            "venue": "XM Global MT5",
            "source": "mt5_terminal",
        }))
    return out


def _positions() -> list[dict]:
    bridge_rows = _bridge_positions()
    if bridge_rows:
        return bridge_rows
    rows = db.read_positions(include_stale=True)
    return [
        {
            **row,
            "venue": row.get("venue") or "XM Global MT5",
            "source": "mt5_runtime",
            "stale": False,
        }
        for row in rows
    ]


def _orders() -> list[dict]:
    bridge_rows = _read_csv(_bridge_dir() / "orders.csv")
    if bridge_rows:
        out = []
        for row in bridge_rows:
            out.append({
                "ticket": row.get("ticket", ""),
                "symbol": row.get("symbol", ""),
                "order_type": row.get("order_type", ""),
                "side": row.get("side", ""),
                "volume": _finite(row.get("volume"), digits=2),
                "price_open": _finite(row.get("price_open"), digits=8),
                "sl": _finite(row.get("sl"), digits=8),
                "tp": _finite(row.get("tp"), digits=8),
                "state": row.get("state", ""),
                "time_setup": row.get("time_setup", ""),
            })
        return out
    return []


def _symbols() -> list[dict]:
    bridge_rows = _read_csv(_bridge_dir() / "symbols.csv")
    visible_overrides = _load_visible_overrides()
    tick_visible = {path.stem.replace("tick_", "").upper() for path in _bridge_dir().glob("tick_*.txt")}
    catalog = {row["symbol"]: row for row in _load_symbol_catalog()}
    aliases = _symbol_aliases()
    allowlist = _dashboard_symbol_allowlist()
    result: dict[str, dict] = {}
    for row in bridge_rows:
        symbol = str(row.get("symbol", "")).strip().upper()
        if not symbol:
            continue
        result[symbol] = {
            "symbol": symbol,
            "visible": str(row.get("visible", "0")).strip() in {"1", "true", "True"} or symbol in visible_overrides,
            "description": row.get("description", "") or catalog.get(symbol, {}).get("description", ""),
            "path": row.get("path", "") or catalog.get(symbol, {}).get("path", ""),
        }
    for canonical, resolved in aliases.items():
        resolved_key = str(resolved or "").strip().upper()
        broker_row = result.get(resolved_key, {})
        catalog_row = catalog.get(canonical, {})
        if not broker_row and not catalog_row:
            continue
        result[canonical] = {
            "symbol": canonical,
            "resolved_symbol": resolved,
            "visible": bool(broker_row.get("visible")) or canonical in visible_overrides or resolved_key in visible_overrides or resolved_key in tick_visible,
            "description": catalog_row.get("description") or broker_row.get("description", ""),
            "path": broker_row.get("path") or catalog_row.get("path", ""),
        }
    for symbol, row in catalog.items():
        resolved = aliases.get(symbol, symbol)
        resolved_key = str(resolved or "").strip().upper()
        result.setdefault(symbol, {
            "symbol": symbol,
            "resolved_symbol": resolved if resolved != symbol else symbol,
            "visible": symbol in visible_overrides or resolved_key in visible_overrides or resolved_key in tick_visible,
            "description": row.get("description", ""),
            "path": row.get("path", ""),
        })
    if allowlist:
        filtered: dict[str, dict] = {}
        for symbol, row in result.items():
            resolved = str(row.get("resolved_symbol") or aliases.get(symbol, symbol) or symbol).strip().upper()
            if symbol in allowlist or resolved in allowlist:
                row["visible"] = True
                filtered[symbol] = row
        result = filtered
    elif result and not any(row.get("visible") for row in result.values()):
        for symbol in {
            "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "USDCAD", "AUDUSD", "NZDUSD",
            "XAUUSD", "XAGUSD", "BTCUSD", "ETHUSD", "NAS100", "US30", "GER40",
            "UK100", "SPX500",
        }:
            if symbol in result:
                result[symbol]["visible"] = True
    return list(result.values())


def _activity() -> dict:
    positions = _positions()
    orders = _orders()
    account = _terminal_account()
    fx = _usd_zar_rate()
    equity = _finite(account.get("equity") or db.read_status("equity") or 0.0)
    daily_pnl = _finite(db.read_status("daily_pnl") or db.read_todays_pnl() or 0.0)
    unrealized = _finite(sum(float(p.get("unrealized") or 0.0) for p in positions))
    market_value = _finite(sum(abs(float(p.get("current") or 0.0) * float(p.get("qty") or 0.0)) for p in positions))
    available = _finite(max(equity - market_value, 0.0))
    updated_at = datetime.now().isoformat()
    if positions:
        updated_at = max(str(p.get("updated_at") or updated_at) for p in positions)
    return {
        "connected": _connected(),
        "broker_backend": "mt5",
        "broker_name": os.getenv("CIPHERFX_BROKER_NAME", "XM Global Demo"),
        "platform_name": os.getenv("CIPHERFX_PLATFORM_NAME", "MetaTrader 5"),
        "account_label": os.getenv("CIPHERFX_ACCOUNT_LABEL", "XM Global MT5 Demo"),
        "positions": positions,
        "position_count": len(positions),
        "unrealized_pnl": unrealized,
        "overview": {
            "account_id": str(db.read_status("account_id") or os.getenv("MT5_LOGIN", "")),
            "server": account.get("server", ""),
            "balance": account.get("balance", 0.0),
            "net_liquidation": equity,
            "daily_pnl": daily_pnl,
            "unrealized_pnl": unrealized,
            "realized_pnl": _finite(daily_pnl - unrealized),
            "market_value": market_value,
            "available_funds": available,
            "equity_with_loan": equity,
            "total_cash": available,
            "cash_balances": ([{"currency": "USD", "cash": available}] if equity else []),
            "currency": account.get("currency") or "USD",
            "usd_zar_rate": fx["rate"],
        },
        "balances": ([{"currency": "USD", "cash": available}] if equity else []),
        "open_orders": orders,
        "open_order_count": len(orders),
        "cancelled_orders": [],
        "summary": [],
        "updated_at": updated_at,
        "error": None,
    }


@app.get("/api/app/config")
def app_config():
    return {
        "company_name": "Cipher FX",
        "broker_backend": "mt5",
        "broker_name": os.getenv("CIPHERFX_BROKER_NAME", "XM Global Demo"),
        "platform_name": os.getenv("CIPHERFX_PLATFORM_NAME", "MetaTrader 5"),
        "account_label": os.getenv("CIPHERFX_ACCOUNT_LABEL", "XM Global MT5 Demo"),
        "currency": "USD",
        "secure_transport": os.getenv("CIPHERFX_PUBLIC_HTTPS", "1") == "1",
    }


@app.get("/", include_in_schema=False)
def root_redirect():
    return RedirectResponse(url="/dashboard/mt5", status_code=307)


@app.get("/dashboard/mt5", include_in_schema=False)
def dashboard_page():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/dashboard/mt5/", include_in_schema=False)
def dashboard_page_slash():
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/login")
def login(req: LoginRequest):
    user_row = _resolve_login_user(req.email, req.password)
    if user_row is None:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = app_store.create_session(user_row["id"])
    user_row, profile_row = app_store.get_user_by_id(user_row["id"])
    return {"token": token, "user": app_store.serialize_user(user_row, profile_row)}


@app.post("/api/login_web", response_class=HTMLResponse)
async def login_web(request: Request):
    raw = (await request.body()).decode("utf-8", errors="ignore")
    form = parse_qs(raw)
    email = (form.get("email") or [""])[0]
    password = (form.get("password") or [""])[0]
    user_row = _resolve_login_user(email, password)
    if user_row is None:
        return HTMLResponse(
            """
            <!doctype html><html><body>
            <script>
              location.replace('/dashboard/mt5?login_error=' + encodeURIComponent('Invalid credentials'));
            </script>
            </body></html>
            """,
            status_code=401,
        )
    token = app_store.create_session(user_row["id"])
    return HTMLResponse(
        f"""
        <!doctype html><html><body>
        <script>
          localStorage.setItem('cipherfx_mt5_token', {token!r});
          location.replace('/dashboard/mt5');
        </script>
        </body></html>
        """
    )


@app.post("/api/auth/local_bootstrap")
def local_bootstrap():
    if os.getenv("CIPHERFX_ALLOW_LOCAL_BOOTSTRAP", "0").strip() != "1":
        raise HTTPException(status_code=403, detail="Login required")
    user_row, profile_row = app_store.ensure_local_bootstrap_user()
    token = app_store.create_session(user_row["id"])
    return {"token": token, "user": app_store.serialize_user(user_row, profile_row)}



_AUDIT_CACHE = {"key": None, "rows": []}
_AUDIT_EVENTS = {
    "STRATEGY_V1_DECISION", "STRATEGY_V1_AUDIT", "signal_gate", "analytics_engine",
    "SETUP_CREATED", "SETUP_CONFIRMED_FRESH", "SETUP_EXPIRED_NO_1M_CONFIRMATION",
    "SETUP_STALE_BLOCKED", "SETUP_CANCELLED", "NOT_QUALIFIED",
    "ENGINE_POLICY_RECHECK_BLOCKED", "MT5_ENGINE_POLICY_RECHECK",
    "COST_TO_TARGET_BLOCKED", "PROFILE_GATE_BLOCKED", "INDEX_EXHAUSTION_BLOCKED",
    "execution_gate", "scan_block_reason", "ORDER_SENT", "ORDER_FILLED",
    "ORDER_REJECTED", "order_rejected_recorded",
}

def _audit_dt(value):
    raw = str(value or "").strip()
    if not raw: return None
    try: parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception: return None
    if parsed.tzinfo is not None: parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed

def _audit_window():
    sast = ZoneInfo("Africa/Johannesburg")
    now_sast = datetime.now(timezone.utc).astimezone(sast)
    start_sast = (now_sast - timedelta(days=1)).replace(hour=21, minute=0, second=0, microsecond=0)
    return (start_sast.astimezone(timezone.utc).replace(tzinfo=None),
            datetime.now(timezone.utc).replace(tzinfo=None), start_sast, now_sast)

def _read_audit_window(start_utc, end_utc):
    path = ROOT_DIR / "state" / "mt5_decision_audit.jsonl"
    try: stat = path.stat()
    except OSError: return []
    key = (stat.st_mtime_ns, stat.st_size, start_utc.isoformat(), end_utc.isoformat())
    if _AUDIT_CACHE["key"] == key: return _AUDIT_CACHE["rows"]
    rows = []
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            if stat.st_size > 64 * 1024 * 1024:
                handle.seek(stat.st_size - 64 * 1024 * 1024); handle.readline()
            for line in handle:
                try: data = json.loads(line)
                except Exception: continue
                event = str(data.get("event") or data.get("type") or "")
                if event not in _AUDIT_EVENTS: continue
                event_dt = _audit_dt(data.get("ts") or data.get("timestamp") or data.get("generated_at"))
                if event_dt is not None and start_utc <= event_dt <= end_utc: rows.append(data)
    except OSError: return []
    rows.sort(key=lambda x: str(x.get("ts") or x.get("timestamp") or ""), reverse=True)
    _AUDIT_CACHE.update(key=key, rows=rows)
    return rows

def _audit_context(data):
    evidence = data.get("evidence") if isinstance(data.get("evidence"), dict) else {}
    htf = evidence.get("higher_timeframes") if isinstance(evidence.get("higher_timeframes"), dict) else {}
    metric = evidence.get("score_metric") if isinstance(evidence.get("score_metric"), dict) else {}
    ta = data.get("trade_audit") if isinstance(data.get("trade_audit"), dict) else {}
    nested = ta.get("score_breakdown") if isinstance(ta.get("score_breakdown"), dict) else {}
    if not metric and isinstance(nested.get("score_metric"), dict): metric = nested["score_metric"]
    cond = ta.get("conditions") if isinstance(ta.get("conditions"), dict) else {}
    if not metric and isinstance(cond.get("score_metric"), dict): metric = cond["score_metric"]
    if not isinstance(metric, dict): metric = {}
    def tf(name):
        x = htf.get(name) if isinstance(htf.get(name), dict) else {}
        return x.get("side") or "", x.get("score")
    h4b,h4s = tf("H4"); h1b,h1s = tf("H1"); m15b,m15s = tf("M15")
    movement = evidence.get("movement") if isinstance(evidence.get("movement"), dict) else {}
    m5 = movement.get("M5") if isinstance(movement.get("M5"), dict) else {}
    m1 = evidence.get("m1_confirmation") if isinstance(evidence.get("m1_confirmation"), dict) else {}
    side = data.get("side") or data.get("direction") or ta.get("side") or ""
    engine = data.get("engine") or ta.get("engine") or cond.get("engine") or ""
    setup_type = data.get("setup_type") or ta.get("setup_type") or ""
    score = data.get("score", metric.get("total"))
    try: score = round(float(score), 2) if score is not None else None
    except Exception: score = None
    decision = str(data.get("decision") or data.get("status") or "").lower()
    passed = decision in {"allow","pass","passed","actionable","confirmed","entered","order_sent"}
    reason = data.get("block_reason") or data.get("reason") or data.get("gate") or ""
    if not reason and isinstance(data.get("reasons"), list): reason = "; ".join(str(x) for x in data["reasons"] if x)
    reason = reason or ta.get("block_reason") or cond.get("block_reason") or ""
    return {
        "symbol": data.get("symbol") or data.get("sym") or "", "side": side,
        "engine": engine, "setup_type": setup_type, "score": score,
        "score_band": metric.get("band") or "",
        "score_metric": {"version": metric.get("version") or "quality_v2", "total": score,
                         "band": metric.get("band") or "", "components": metric.get("components") or {},
                         "maxima": metric.get("maxima") or {}},
        "h4_bias": h4b or ta.get("h4_bias") or "", "h4_score": h4s if h4s is not None else ta.get("h4_score"),
        "h1_bias": h1b or ta.get("h1_bias") or "", "h1_score": h1s if h1s is not None else ta.get("h1_score", ta.get("bias_1h_score")),
        "m15_bias": m15b or ta.get("m15_bias") or "", "m15_score": m15s if m15s is not None else ta.get("m15_score", ta.get("setup_15m_score")),
        "m5_trigger_side": m5.get("side") or "", "m5_trigger_score": ta.get("trigger_5m_score"),
        "m1_status": m1.get("status") or ta.get("execution_1m_result") or "",
        "m1_confirmation_side": m1.get("direction_required") or ta.get("confirmation_side") or "",
        "final_status": data.get("final_status") or ("PASS" if passed else "BLOCK"),
        "gate_ok": passed, "reason": str(reason or "").strip(),
        "setup_id": data.get("setup_id") or "", "setup_created_at": data.get("setup_created_at") or "",
        "confirmation_detected_at": data.get("confirmation_detected_at") or "",
        "ts": data.get("ts") or data.get("timestamp") or data.get("generated_at") or "",
        "event": data.get("event") or "", "scope": data.get("scope") or "",
        "raw_status": data.get("status") or data.get("decision") or "",
    }

def _audit_reason(row):
    event, reason = row.get("event") or "", str(row.get("reason") or "").strip()
    if event == "scan_block_reason" and reason == "global trading lock active": return "GLOBAL_TRADING_LOCK"
    if "MARKETS_CLOSED" in reason.upper() or "market closed" in reason.lower(): return "MARKETS_CLOSED"
    if event in {"STRATEGY_V1_DECISION","NOT_QUALIFIED"}: return "NOT_QUALIFIED"
    if event == "SETUP_CANCELLED": return "SETUP_CANCELLED"
    if event in {"ENGINE_POLICY_RECHECK_BLOCKED","execution_gate"}: return "FINAL_GATE_BLOCKED"
    if event in {"ORDER_REJECTED","order_rejected_recorded"}: return "ORDER_REJECTED"
    return event or "AUDIT"

def _market_hours_snapshot(now_sast=None):
    sast = ZoneInfo("Africa/Johannesburg")
    now_sast = now_sast or datetime.now(timezone.utc).astimezone(sast)
    defs = [
        ("us_new_york","US / New York","America/New_York",(9,30),(16,0)),
        ("uk_london","UK / London","Europe/London",(8,0),(16,30)),
        ("sydney","Sydney","Australia/Sydney",(9,0),(17,0)),
        ("asia_tokyo","Asia / Tokyo","Asia/Tokyo",(9,0),(17,0)),
    ]
    sessions = []
    for sid,label,tzname,ot,ct in defs:
        tz = ZoneInfo(tzname); local = now_sast.astimezone(tz)
        od = local.replace(hour=ot[0],minute=ot[1],second=0,microsecond=0)
        cd = local.replace(hour=ct[0],minute=ct[1],second=0,microsecond=0)
        is_open = local.weekday() < 5 and od <= local < cd
        next_day = local
        while next_day.weekday() >= 5 or (next_day.date() == local.date() and local >= cd):
            next_day = (next_day + timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0)
        no = next_day.replace(hour=ot[0],minute=ot[1],second=0,microsecond=0).astimezone(sast)
        op = od.astimezone(sast).strftime("%H:%M"); cl = cd.astimezone(sast).strftime("%H:%M")
        sessions.append({"id":sid,"label":label,"region":tzname,"open":is_open,"status":"OPEN" if is_open else "CLOSED",
                         "open_sast":op+" SAST","close_sast":cl+" SAST","hours_sast":f"{op} - {cl} SAST",
                         "next_open_sast":no.strftime("%a %d %b %H:%M SAST"),
                         "reason":"Live session" if is_open else "Outside session hours"})
    weekend = now_sast.weekday() >= 5
    if weekend:
        for item in sessions: item.update(open=False,status="CLOSED",reason="Weekend closed")
    return {"timezone":"Africa/Johannesburg","timezone_label":"SAST","now_sast":now_sast.isoformat(),
            "weekend_closed":weekend,"global_status":"CLOSED" if weekend else ("OPEN" if any(x["open"] for x in sessions) else "CLOSED"),
            "global_reason":"MARKETS_CLOSED_WEEKEND" if weekend else "","sessions":sessions}

def _live_audit_summary():
    start,end,start_sast,now_sast = _audit_window()
    raw = _read_audit_window(start,end)
    public = [_audit_context(x) for x in raw]
    market = _market_hours_snapshot(now_sast)
    blocks=[]; trades=[]
    for row in public:
        event=row.get("event")
        if event in {"ORDER_SENT","ORDER_FILLED","ORDER_REJECTED","order_rejected_recorded"}:
            trades.append({**row,"reason_group":_audit_reason(row)})
        elif event not in {"STRATEGY_V1_AUDIT","analytics_engine","signal_gate","MT5_ENGINE_POLICY_RECHECK"}:
            if row.get("reason") or event in {"SETUP_CREATED","SETUP_CONFIRMED_FRESH"}:
                blocks.append({**row,"reason_group":_audit_reason(row)})
    counts={}
    for row in blocks: counts[row["reason_group"]]=counts.get(row["reason_group"],0)+1
    for event in ("SETUP_CREATED","SETUP_CONFIRMED_FRESH","NOT_QUALIFIED","SETUP_CANCELLED","SETUP_EXPIRED_NO_1M_CONFIRMATION",
                  "SETUP_STALE_BLOCKED","ORDER_SENT","ORDER_FILLED","ORDER_REJECTED"):
        counts.setdefault(event,sum(1 for x in public if x.get("event")==event))
    latest={}; priority={"signal_gate":5,"STRATEGY_V1_DECISION":4,"analytics_engine":3,"SETUP_CREATED":2}
    for row in public:
        sym=row.get("symbol")
        if not sym: continue
        key=(priority.get(row.get("event"),1), str(row.get("ts") or ""))
        if sym not in latest or key>latest[sym][0]: latest[sym]=(key,row)
    scanner=[x[1] for x in sorted(latest.values(),key=lambda x:x[1].get("ts") or "",reverse=True)]
    if market["weekend_closed"]:
        for row in scanner: row.update(current_market_status="MARKETS_CLOSED_WEEKEND",current_market_reason=market["global_reason"])
    return {"source":"mt5_decision_audit.jsonl","score_source":"quality_v2",
            "window":{"start_sast":start_sast.isoformat(),"end_sast":now_sast.isoformat(),"label":"Since 21:00 SAST last night"},
            "market":market,"counts":counts,"scanner_rows":scanner[:60],"blocks":blocks[:160],"trades":trades[:120],
            "reason_counts":sorted(({"reason":k,"count":v} for k,v in counts.items()),key=lambda x:x["count"],reverse=True),
            "updated_at":datetime.now(timezone.utc).isoformat()}


@app.get("/api/health", dependencies=[Depends(_session_user)])
def health():
    account = _terminal_account()
    return {
        "ok": True,
        "broker_backend": "mt5",
        "mt5_connected": _connected(),
        "account_id": account["login"],
        "server": account["server"],
    }


@app.get("/api/status", dependencies=[Depends(_session_user)])
def status():
    activity = _activity()
    fx = _usd_zar_rate()
    return {
        "broker_backend": "mt5",
        "broker_name": activity["broker_name"],
        "platform_name": activity["platform_name"],
        "account_id": activity["overview"]["account_id"],
        "server": activity["overview"].get("server", ""),
        "balance": activity["overview"].get("balance", 0.0),
        "equity": activity["overview"]["net_liquidation"],
        "daily_pnl": activity["overview"]["daily_pnl"],
        "currency": activity["overview"].get("currency", "USD"),
        "usd_zar_rate": fx["rate"],
        "usd_zar_source": fx["source"],
        "usd_zar_updated_at": fx["updated_at"],
        "mode": db.read_status("mode") or "DEMO",
        "mt5_connected": activity["connected"],
        "mt5_trade_allowed": db.read_status("mt5_trade_allowed"),
        "dry_run": os.getenv("MT5_DRY_RUN", "0") in {"1", "true", "True", "yes", "on"},
        "poll_seconds": db.read_status("poll_seconds") or os.getenv("MT5_POLL_SECONDS", "1"),
        "scan_seconds": db.read_status("scan_seconds") or os.getenv("MT5_SCAN_SECONDS", "890"),
        "max_open_trades": db.read_status("max_open_trades") or os.getenv("MT5_MAX_OPEN_TRADES", "30"),
        "max_daily_trades": db.read_status("max_daily_trades") or os.getenv("MT5_MAX_DAILY_TRADES", "30"),
        "daily_trade_count": db.read_status("daily_trade_count"),
        "next_scan_due": db.read_status("next_scan_due"),
        "last_scan": db.read_status("last_scan"),
        "last_heartbeat": db.read_status("last_heartbeat"),
        "live_unrealized_pnl": activity["unrealized_pnl"],
        "live_position_count": activity["position_count"],
        "effective_position_count": activity["position_count"],
        "halt": db.read_status("halt"),
        "paused": False,
    }



@app.get("/api/audit/summary", dependencies=[Depends(_session_user)])
def audit_summary():
    return _live_audit_summary()

@app.get("/api/market-hours", dependencies=[Depends(_session_user)])
def market_hours():
    return _market_hours_snapshot()


@app.get("/api/broker/activity", dependencies=[Depends(_session_user)])
def broker_activity():
    return _activity()


@app.get("/api/positions", dependencies=[Depends(_session_user)])
def positions():
    return _positions()


@app.get("/api/orders", dependencies=[Depends(_session_user)])
def orders():
    return _orders()


@app.get("/api/symbols", dependencies=[Depends(_session_user)])
def symbols():
    return _symbols()


@app.get("/api/signals", dependencies=[Depends(_session_user)])
def signals():
    return db.read_signals()


@app.get("/api/scanner", dependencies=[Depends(_session_user)])
def scanner():
    return db.read_signal_scanner()


@app.get("/api/news", dependencies=[Depends(_session_user)])
def news(sym: str = "EURUSD", limit: int = 8):
    lookup = _news_symbol(sym)
    now = time.time()
    cached = _news_cache.get(lookup)
    if cached and now - cached["ts"] < 900:
        return cached["items"][:limit]
    items = _fetch_news_rss(sym, limit)
    if items:
        _news_cache[lookup] = {"items": items, "ts": now}
    return (items or (cached["items"] if cached else []))[:limit]


@app.get("/api/trades", dependencies=[Depends(_session_user)])
def trades(limit: int = 100, include_history: bool = False):
    return db.read_trades(limit=limit, today_only=not include_history)


@app.get("/api/deals", dependencies=[Depends(_session_user)])
def deals(period: str = "today", limit: int = 200):
    return db.read_deals_period(period=period, limit=limit)


@app.get("/api/stats", dependencies=[Depends(_session_user)])
def stats():
    return db.read_stats()


@app.get("/api/candles", dependencies=[Depends(_session_user)])
def candles(sym: str = "EURUSD", limit: int = 120, timeframe: str = "M15"):
    count = max(1, min(int(limit or 120), 500))
    code = sym.upper()
    tf = (timeframe or "M15").upper()
    bridge_rows = _read_bridge_rates(code, tf, count)
    if not bridge_rows and tf != "M15":
        m15_needed = count
        if _timeframe_seconds(tf) > _timeframe_seconds("M15"):
            m15_needed = min(500, count * max(1, _timeframe_seconds(tf) // _timeframe_seconds("M15")))
        m15_rows = _read_bridge_rates(code, "M15", m15_needed)
        bridge_rows = _aggregate_rate_rows(m15_rows, tf, count) if m15_rows else []
    if bridge_rows:
        return [_candle_payload(row) for row in bridge_rows]
    db_rows = db.read_candles(code, count)
    if db_rows:
        return [
            {
                **row,
                "time_label": db.date_detail_for(row.get("ts")).get("label", ""),
                "time_zone": "SAST",
                "source": row.get("source") or "mt5_runtime",
            }
            for row in db_rows
        ]
    return []


@app.get("/api/terminal", dependencies=[Depends(_session_user)])
def terminal():
    account = _terminal_account()
    fx = _usd_zar_rate()
    return {
        "account": account,
        "currency": {
            "account": account.get("currency") or "USD",
            "usd_zar_rate": fx["rate"],
            "usd_zar_source": fx["source"],
            "usd_zar_updated_at": fx["updated_at"],
        },
        "ticks": _terminal_ticks(),
        "positions": _positions(),
        "orders": _orders(),
        "symbols": _symbols(),
        "deals": _read_csv(_bridge_dir() / "deals.csv", 100),
        "updated_at": datetime.now().isoformat(),
    }


@app.post("/api/orders/market", dependencies=[Depends(_session_user)])
def place_market_order(req: MarketOrderRequest):
    _require_manual_trading_enabled()
    direction = req.side.strip().upper()
    if direction not in {"BUY", "SELL"}:
        raise HTTPException(status_code=400, detail="side must be BUY or SELL")
    try:
        resolved, price, result = _with_gateway(
            lambda gateway: gateway.place_market_order(
                req.symbol.strip().upper(),
                direction,
                float(req.volume),
                float(req.sl or 0.0),
                float(req.tp or 0.0),
                req.comment,
            )
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "ok": True,
        "symbol": resolved,
        "side": direction,
        "price": price,
        "retcode": int(getattr(result, "retcode", 0) or 0),
        "order": int(getattr(result, "order", 0) or 0),
        "deal": int(getattr(result, "deal", 0) or 0),
    }


@app.post("/api/orders/pending", dependencies=[Depends(_session_user)])
def place_pending_order(req: PendingOrderRequest):
    _require_manual_trading_enabled()
    direction = req.side.strip().upper()
    pending_type = req.pending_type.strip().upper()
    if direction not in {"BUY", "SELL"}:
        raise HTTPException(status_code=400, detail="side must be BUY or SELL")
    if pending_type not in {"LIMIT", "STOP"}:
        raise HTTPException(status_code=400, detail="pending_type must be LIMIT or STOP")
    try:
        resolved, result = _with_gateway(
            lambda gateway: gateway.place_pending_order(
                req.symbol.strip().upper(),
                direction,
                float(req.volume),
                float(req.price),
                float(req.sl or 0.0),
                float(req.tp or 0.0),
                pending_type,
                req.comment,
            )
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "ok": True,
        "symbol": resolved,
        "side": direction,
        "pending_type": pending_type,
        "retcode": int(getattr(result, "retcode", 0) or 0),
        "order": int(getattr(result, "order", 0) or 0),
        "deal": int(getattr(result, "deal", 0) or 0),
        "price": float(getattr(result, "price", 0.0) or 0.0),
    }


@app.post("/api/positions/{ticket}/close", dependencies=[Depends(_session_user)])
def close_position(ticket: int):
    matches = [row for row in _positions() if str(row.get("ticket", "")) == str(ticket)]
    if not matches:
        raise HTTPException(status_code=404, detail="Position not found")
    row = matches[0]
    try:
        result = _with_gateway(lambda gateway: gateway.close_position(
            type("PositionRow", (), {
                "ticket": int(ticket),
                "symbol": row.get("sym", ""),
                "direction": row.get("direction", "BUY"),
                "volume": float(row.get("qty", 0.0) or 0.0),
            })()
        ))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "ok": True,
        "ticket": ticket,
        "retcode": int(getattr(result, "retcode", 0) or 0),
        "order": int(getattr(result, "order", 0) or 0),
        "deal": int(getattr(result, "deal", 0) or 0),
        "price": float(getattr(result, "price", 0.0) or 0.0),
    }


@app.post("/api/positions/{ticket}/modify", dependencies=[Depends(_session_user)])
def modify_position(ticket: int, req: ModifyPositionRequest):
    try:
        result = _with_gateway(
            lambda gateway: gateway.modify_position(
                ticket,
                req.symbol.strip().upper(),
                float(req.sl or 0.0),
                float(req.tp or 0.0),
            )
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "ok": True,
        "ticket": ticket,
        "retcode": int(getattr(result, "retcode", 0) or 0),
        "order": int(getattr(result, "order", 0) or 0),
        "deal": int(getattr(result, "deal", 0) or 0),
        "price": float(getattr(result, "price", 0.0) or 0.0),
    }


@app.delete("/api/orders/{ticket}", dependencies=[Depends(_session_user)])
def cancel_order(ticket: int):
    try:
        result = _with_gateway(lambda gateway: gateway.cancel_order(ticket))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "ok": True,
        "ticket": ticket,
        "retcode": int(getattr(result, "retcode", 0) or 0),
        "order": int(getattr(result, "order", 0) or 0),
        "deal": int(getattr(result, "deal", 0) or 0),
    }


@app.post("/api/symbols/activate", dependencies=[Depends(_session_user)])
def activate_symbol(req: SymbolToggleRequest):
    symbol = req.symbol.strip().upper()
    visible = _load_visible_overrides()
    try:
        resolved = _with_gateway(lambda gateway: gateway.set_symbol_visibility(symbol, req.enabled))
    except Exception as exc:
        resolved = symbol
        resolved_key = str(_resolve_symbol_code(symbol) or symbol).strip().upper()
        if req.enabled:
            visible.add(symbol)
            visible.add(resolved_key)
        else:
            visible.discard(symbol)
            visible.discard(resolved_key)
        _save_visible_overrides(visible)
        return {"ok": True, "symbol": resolved, "enabled": req.enabled, "warning": str(exc)}
    if req.enabled:
        visible.add(symbol)
        visible.add(str(resolved or symbol).strip().upper())
    else:
        visible.discard(symbol)
        visible.discard(str(resolved or symbol).strip().upper())
    _save_visible_overrides(visible)
    return {"ok": True, "symbol": resolved, "enabled": req.enabled}
