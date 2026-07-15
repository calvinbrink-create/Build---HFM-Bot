#!/usr/bin/env python3
"""
IBKR SCALPING BOT v4.0 — Single File, macOS
Live IBKR data · ATR Regime · Cost-Aware · Quality-Gated
Stocks · Forex · Crypto · Futures

KEY CHANGE FROM v3: uses IBKR real-time bars instead of yfinance.
yfinance is only used for the --backtest (historical daily bars).
All live signals use IBKR's own feed — no stale/cached prices.

INSTALL (one time):
  pip3 install ib_insync yfinance pandas numpy ta tabulate colorama schedule

RUN:
  python3 run_bot.py                       # LaunchAgent entrypoint / paper account
  python3 scalping_bot_v4.py --backtest    # walk-forward backtest (daily bars)
"""

import argparse, asyncio, json, logging, os, sys, time, warnings
from datetime import datetime, timedelta, timezone as _UTC
from zoneinfo import ZoneInfo

warnings.filterwarnings("ignore")

# Python 3.12+ removed auto-creation of event loops; eventkit/ib_insync
# crashes on import without one.  Create it before anything else.
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

# Always run from the bot's own directory so relative paths (logs, JSON files) work
# correctly regardless of where the script is launched from.
os.chdir(os.path.dirname(os.path.abspath(__file__)))

# ── Dashboard state store (optional — bot works fine without it) ──────────────
_ss_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard", "backend")
if _ss_path not in sys.path:
    sys.path.insert(0, _ss_path)
try:
    import state_store as _ss
    _ss.init_db()
    _HAS_DB = True
except Exception as _e:
    _HAS_DB = False
    print(f"[WARNING] Dashboard DB unavailable — trades will NOT appear on dashboard: {_e}")

try:
    import news_fetcher as _news_fetcher
    _HAS_NEWS = True
except Exception:
    _HAS_NEWS = False

import importlib.util
_REQUIRED = ["ib_insync","yfinance","pandas","numpy","ta",
             "tabulate","colorama","schedule"]
_miss = [p for p in _REQUIRED if not importlib.util.find_spec(p)]
if _miss:
    print(f"[bot] Missing: {_miss} — auto-installing...")
    import subprocess as _sp
    _sp.run([sys.executable, "-m", "pip", "install", "--quiet"] + _miss)
    importlib.invalidate_caches()
    _miss = [p for p in _REQUIRED if not importlib.util.find_spec(p)]
    if _miss:
        print(f"\n  FATAL: pip3 install {' '.join(_miss)}\n")
        sys.exit(1)

import numpy  as np
import pandas as pd
import yfinance as yf
import schedule
import news_filter
from tabulate import tabulate
from colorama import Fore, Style, init as _ci
import ta
from ib_insync import (IB, Stock, Forex, Crypto, ContFuture,
                       Contract, BarData, LimitOrder, StopOrder, util)

_ci(autoreset=True)
util.logToConsole(level=40)
TRADING_TZ = ZoneInfo("Africa/Johannesburg")
BANNER = f"""{Fore.CYAN}{Style.BRIGHT}
  IBKR SCALPING BOT v4.0
{Style.RESET_ALL}"""


def _now_trade_tz():
    return datetime.now(TRADING_TZ)


def _minutes_since_midnight(dt: datetime) -> int:
    return dt.hour * 60 + dt.minute


def _is_within_minute_window(now_mins: int, start_mins: int, end_mins: int) -> bool:
    """Inclusive minute-window check supporting windows that cross midnight."""
    if start_mins <= end_mins:
        return start_mins <= now_mins <= end_mins
    return now_mins >= start_mins or now_mins <= end_mins


def _market_window_active(now_dt: datetime, windows: list[tuple[int, int, int, int]]) -> bool:
    now_mins = _minutes_since_midnight(now_dt)
    for sh, sm, eh, em in windows:
        if _is_within_minute_window(now_mins, sh * 60 + sm, eh * 60 + em):
            return True
    return False


def _weekday_window_active(now_dt: datetime, windows: list[tuple[int, int, int, int]], weekdays: set[int]) -> bool:
    if now_dt.weekday() not in weekdays:
        return False
    return _market_window_active(now_dt, windows)


def _forex_window_active(now_dt: datetime) -> bool:
    weekday = now_dt.weekday()  # Mon=0 ... Sun=6
    now_mins = _minutes_since_midnight(now_dt)
    if weekday in {0, 1, 2, 3, 4}:
        return True
    if weekday == 5:
        return now_mins <= (2 * 60)
    if weekday == 6:
        return now_mins >= (23 * 60)
    return False


def _global_trading_enabled(now_dt: datetime | None = None) -> bool:
    """
    Global weekend shutdown in SAST.

    Trading is disabled from Saturday 02:01 SAST until Sunday 22:59 SAST.
    It resumes Sunday 23:00 SAST.
    """
    now_dt = now_dt or _now_trade_tz()
    weekday = now_dt.weekday()  # Mon=0 ... Sun=6
    now_mins = _minutes_since_midnight(now_dt)
    if weekday == 5 and now_mins >= (2 * 60 + 1):
        return False
    if weekday == 6 and now_mins < (23 * 60):
        return False
    return True


def _session_key_for_market(dt: datetime, market: str) -> tuple[str, str]:
    """
    Stable session bucket used for session VWAP resets and ORB windows.

    The returned tuple is (market, bucket_label). Bucket labels are based on
    SAST-local session boundaries rather than UTC dates.
    """
    local_dt = dt.astimezone(TRADING_TZ)
    mins = _minutes_since_midnight(local_dt)
    day = local_dt.date()

    if market in {"forex", "metal", "energy"}:
        # FX/metals/energy CFD trading day rolls at 23:00 SAST.
        if mins >= 23 * 60:
            bucket_day = day
        else:
            bucket_day = day - timedelta(days=1)
        return market, bucket_day.isoformat()

    if market in {"stock_us", "future", "index_cfd"}:
        # US session spans 15:20 -> 02:00 SAST.
        if mins >= 15 * 60 + 20:
            bucket_day = day
        elif mins <= 2 * 60:
            bucket_day = day - timedelta(days=1)
        else:
            bucket_day = day
        return market, bucket_day.isoformat()

    if market == "stock_uk":
        return market, day.isoformat()

    if market == "index_cfd_eu":
        return market, day.isoformat()

    return market, day.isoformat()


def _filter_current_session(df: pd.DataFrame, market: str, now_dt: datetime | None = None) -> pd.DataFrame:
    """Return only the bars that belong to the current SAST session bucket."""
    if df is None or df.empty:
        return df
    now_dt = now_dt or _now_trade_tz()
    target_key = _session_key_for_market(now_dt, market)

    idx = pd.DatetimeIndex(df.index)
    if idx.tz is None:
        idx_local = idx.tz_localize("UTC").tz_convert(TRADING_TZ)
    else:
        idx_local = idx.tz_convert(TRADING_TZ)
    mask = [_session_key_for_market(ts.to_pydatetime(), market) == target_key for ts in idx_local]
    filtered = df[mask]
    return filtered if not filtered.empty else df


def _orb_window_utc(market: str, now_dt: datetime | None = None) -> tuple[datetime, datetime]:
    """UTC ORB window aligned to the current market session."""
    now_dt = now_dt or _now_trade_tz()
    market_key, bucket_day_text = _session_key_for_market(now_dt, market)
    bucket_day = datetime.fromisoformat(bucket_day_text).date()

    if market == "stock_us":
        start_local = datetime(bucket_day.year, bucket_day.month, bucket_day.day, 15, 20, tzinfo=TRADING_TZ)
        end_local = datetime(bucket_day.year, bucket_day.month, bucket_day.day, 15, 50, tzinfo=TRADING_TZ)
    elif market in {"forex", "metal", "energy"}:
        # Use the FX/CFD session roll / Sydney-open half hour.
        start_local = datetime(bucket_day.year, bucket_day.month, bucket_day.day, 23, 0, tzinfo=TRADING_TZ)
        end_local = datetime(bucket_day.year, bucket_day.month, bucket_day.day, 23, 30, tzinfo=TRADING_TZ)
    else:
        start_local = datetime(bucket_day.year, bucket_day.month, bucket_day.day, 9, 50, tzinfo=TRADING_TZ)
        end_local = datetime(bucket_day.year, bucket_day.month, bucket_day.day, 10, 20, tzinfo=TRADING_TZ)
    return start_local.astimezone(_UTC.utc), end_local.astimezone(_UTC.utc)


def _trade_date_from_value(ts_value=None, fallback=None):
    for raw in (ts_value, fallback):
        if raw in (None, ""):
            continue
        text = str(raw).strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except Exception:
            dt = None
        if dt is None:
            for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y%m%d %H:%M:%S", "%Y%m%d-%H:%M:%S"):
                try:
                    dt = datetime.strptime(text, fmt)
                    break
                except Exception:
                    continue
        if dt is None:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TRADING_TZ)
        else:
            dt = dt.astimezone(TRADING_TZ)
        return dt.date().isoformat()
    return _now_trade_tz().date().isoformat()


def _trade_week_bounds(today_value=None):
    today = datetime.fromisoformat(today_value).date() if today_value else _now_trade_tz().date()
    week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=6)
    return week_start.isoformat(), week_end.isoformat()

# Suppress ib_insync's own wrapper logger — our _on_error callback classifies
# every IBKR error code; the wrapper logging the same errors independently
# creates duplicate lines in scalper.log and inflates the error count.
logging.getLogger("ib_insync.wrapper").setLevel(logging.CRITICAL)
logging.getLogger("ib_insync").setLevel(logging.CRITICAL)

# ── WhatsApp notifications (CallMeBot) ────────────────────────────────────────
import urllib.request, urllib.parse

_WA_PHONE  = "+27726203084"
_WA_APIKEY = "3688317"

def notify(msg: str):
    """Send a WhatsApp message via CallMeBot."""
    try:
        params = urllib.parse.urlencode({"phone": _WA_PHONE, "text": msg, "apikey": _WA_APIKEY})
        url = f"https://api.callmebot.com/whatsapp.php?{params}"
        with urllib.request.urlopen(url, timeout=10) as resp:
            log.info(f"WhatsApp sent (HTTP {resp.status}): {msg[:60]}")
    except Exception as exc:
        log.warning(f"WhatsApp notify FAILED: {exc} — message was: {msg[:60]}")

# ── VIX cache — fetched from yfinance, cached 30 minutes ─────────────────────
_VIX_CACHE: dict = {"level": 20.0, "ts": 0.0}

def _get_vix_level() -> float:
    """Fetch VIX from yfinance, cached 30 minutes. Returns 20.0 on error."""
    import time
    now = time.time()
    if now - _VIX_CACHE["ts"] < 1800:
        return _VIX_CACHE["level"]
    try:
        _vix_df = yf.Ticker("^VIX").history(period="1d", interval="5m")
        if _vix_df is not None and not _vix_df.empty:
            _VIX_CACHE["level"] = float(_vix_df["Close"].iloc[-1])
            _VIX_CACHE["ts"] = now
    except Exception:
        pass
    return _VIX_CACHE["level"]

# ── Sector ETF map + returns cache ───────────────────────────────────────────
_SECTOR_MAP: dict = {
    "NVDA": "XLK", "AMD": "XLK", "AAPL": "XLK", "MSFT": "XLK",
    "PLTR": "XLK", "SMCI": "XLK", "SOUN": "XLK",
    "JPM":  "XLF", "BAC": "XLF", "GS": "XLF", "V": "XLF", "MA": "XLF",
    "XOM":  "XLE", "CVX": "XLE",
    "AMGN": "XLV", "LLY": "XLV", "PFE": "XLV", "MRNA": "XLV", "BMY": "XLV",
    "AMZN": "XLY", "SBUX": "XLY", "MCD": "XLY",
    "NFLX": "XLC", "META": "XLC",
    "CAT":  "XLI",
    "TSLA": "XLY",
}
_SECTOR_RETURNS: dict = {}

def _fetch_sector_returns() -> dict:
    """Fetch intraday returns for sector ETFs via yfinance. Cached in _SECTOR_RETURNS."""
    global _SECTOR_RETURNS
    etfs = list(set(_SECTOR_MAP.values()))
    try:
        import yfinance as _yf
        _data = _yf.download(etfs, period="1d", interval="15m",
                              group_by="ticker", auto_adjust=True, progress=False, threads=True)
        for etf in etfs:
            try:
                df_e = _data[etf] if len(etfs) > 1 else _data
                if df_e is not None and not df_e.empty:
                    o = float(df_e["Open"].iloc[0])
                    c = float(df_e["Close"].iloc[-1])
                    _SECTOR_RETURNS[etf] = (c - o) / o if o > 0 else 0.0
            except Exception:
                _SECTOR_RETURNS[etf] = 0.0
    except Exception:
        pass
    return _SECTOR_RETURNS

CFG = {
    "host":         "127.0.0.1",
    "paper_port":   int(os.getenv("SCALPBOT_PAPER_PORT", "7497")),   # TWS/IB Gateway paper
    "live_port":    int(os.getenv("SCALPBOT_LIVE_PORT", "7496")),    # TWS/IB Gateway live
    "client_id":    20,

    # bar sizes per market type
    "ibkr_bar_count":       100,           # bars to fetch (applies to all markets)
    "ibkr_bar_size":          "15 mins",   # default fallback (futures etc)
    "ibkr_bar_size_stock":    "15 mins",  # 15-min: wider ATR, stops survive intrabar noise, matches IBKR latency
    "ibkr_bar_size_stock_uk": "15 mins",  # UK stocks: 15-min rhythm matches LSE
    "ibkr_bar_size_forex":    "15 mins",  # forex: bigger ATR → TP reachable in 3-4 bars
    "ibkr_bar_size_crypto":   "15 mins",  # crypto: 15min avoids 5min noise; still 24/7
    "ibkr_bar_size_execution": "1 min",   # confirmation-only timeframe
    "ibkr_what_to_show":    "TRADES",      # for stocks/futures
    "ibkr_cfd_show":        "MIDPOINT",    # CFDs are OTC — IBKR requires MIDPOINT
    "ibkr_fx_show":         "MIDPOINT",    # for forex
    "ibkr_crypto_show":     "AGGTRADES",

    "backtest_days":    365,        # yfinance daily bars for --backtest only

    "capital_cap_usd":    5000.0,  # strategy sizing cap; dashboard equity remains real IBKR NetLiquidation

    # ── GLOBAL DEFAULTS ──────────────────────────────────────────────────────
    "use_cfd_us":         False,  # OFF — no leverage, regular share orders only
    "risk_pct":           0.35,   # reduced while live paper results show negative expectancy
    "max_open":           1,      # one bot-managed trade max until the strategy proves itself
    "max_portfolio_heat_pct": 2,  # reduce stacked exposure while signal quality is rebuilt
    "max_daily_loss_pct": 5.0,
    "atr_stop_mult":      2.0,    # wider stop — avoids premature stop-outs
    "max_position_pct":   0.40,   # 40% of cap = $1,600 notional per trade (no leverage)

    # ── US STOCKS ─────────────────────────────────────────────────────────────
    "rr_ratio_us":        2.0,   # 5R never hits on 15-min bars — 2R matches intraday realities
    "stop_floor_pct_us":  0.003,  # 0.3% — let ATR govern the stop, not the floor
    "max_hold_mins_stock": 150,    # 2.5h — 2R targets resolve in 1-2h; holding longer adds reversal risk

    # ── UK STOCKS (LSE) ───────────────────────────────────────────────────────
    # Requires LSE Level-1 data subscription in IBKR account market-data settings.
    "rr_ratio_uk":        2.0,     # 2R — academic evidence: 5R targets dramatically lower win rate on 15-min
    "stop_floor_pct_uk":  0.003,   # 0.3% — GBP-priced stocks, slightly wider spreads
    "max_hold_mins_stock_uk": 90,  # 6 × 15-min bars — exit well before LSE close at 17:30 SAST

    # ── SHARED FALLBACK (futures, crypto fallback) ────────────────────────────
    "rr_ratio":           1.5,
    "stop_floor_pct":     0.002,
    "forex_stop_floor_pct": 0.0005,  # 5-pip floor — above ECN spread (~0.5 pip) on AUDUSD/NZDUSD
    "rr_ratio_forex":     2.0,        # 2R target for forex — wider than 1.5 to offset any spread cost
    "min_forex_qty":      2000,       # IBKR min $1.50/side commission — 2k units × 2R target easily covers $3 round-trip
    "forex_max_leverage": 10,         # IBKR forex is margined — qty_cap uses 10× so small accounts can reach min lot
    "max_hold_mins_forex":  150,
    "max_hold_mins_future":  50,
    "crypto_stop_floor_pct": 0.005,   # wider stop floor for crypto (less noise-triggered)
    "metal_stop_floor_pct": 0.0015,   # gold/silver CFDs need a small price floor without forex pip assumptions
    "energy_stop_floor_pct": 0.0030,  # oil/gas CFDs are noisier than FX but tighter than single-name stocks
    "crypto_rr":          2.0,        # crypto RR — higher than 1.5 fallback, lets runners run
    "rr_ratio_metals":    1.8,
    "rr_ratio_energies":  1.8,
    "min_crypto_qty":     0.0005,     # below this, skip the trade (avoids IBKR rejection)

    # STRICT TIME-OF-DAY FILTER (SAST hours) — no lunch/extra windows.
    # UK/EU window: Monday-Friday 10:00-19:30 SAST.
    # US window: Monday-Friday 15:20-23:00 SAST.
    # Forex/metals/energy: Sunday 23:00-Saturday 02:00 SAST.
    "trade_windows_uk_sast": [(10, 0, 19, 30)],
    "trade_windows_us_sast": [(15, 20, 23, 0)],
    "trade_windows_forex_sast": [(23, 0, 2, 0)],
    "allow_forex_crypto_anytime": True,

    # Disabled by request: strict windows only, no US lunch break block.
    "lunch_block_us_sast": None,

    # PER-MARKET DAILY TRADE CAPS — separate counters for UK vs US stocks,
    # so one market filling its quota doesn't block the other.
    "max_trades_per_day_uk": 30,
    "max_trades_per_day_us": 30,

    # TRAILING STOP — activates at 1R profit, which moves stop to breakeven.
    # Any trade reaching +1R can no longer be a net loser (stop = entry).
    "trailing_stop_enabled": True,
    "trail_trigger_r":       1.0,    # activate at 1R profit → stop moves to entry (breakeven protection)
    "trail_distance_atr":    1.0,    # trail = 1×ATR behind price (equals stop distance with atr_stop_mult=1.0)

    # PARTIAL TAKE-PROFIT — bank some profit early, let the rest ride the trail
    "partial_tp_enabled":    True,
    "partial_tp_r":          1.2,    # take partial profit at 1.2R — must fire BEFORE bracket TP at 2R
    "partial_tp_fraction":   0.4,    # close 40% at 1.2R — keep 60% running for full TP at 2R

    # PYRAMID — add to a winning position once it reaches X R profit.
    # Adds pyramid_qty_fraction of original qty and moves stop to breakeven.
    # Trigger set ABOVE partial_tp_r so pyramid fires AFTER partial TP has already
    # locked profit and the trailing stop is active — not simultaneously (which cancels out).
    "pyramid_enabled":       True,
    "pyramid_trigger_r":     1.5,    # add at 1.5R profit — after partial TP at 1.2R, before bracket TP at 2R
    "pyramid_qty_fraction":  0.5,    # add 50% of original qty

    # PER-SYMBOL COOLDOWN — separate from the global cooldown, prevents
    # the bot re-entering the SAME symbol too quickly even if the global
    # cooldown has expired (global blocks ANY trade, this blocks repeats).
    "symbol_cooldown_bars":  1,      # 1 × 14m50s scan = one 15-min candle cooldown per symbol after each trade

    # EQUITY DRAWDOWN STOP — separate from the daily loss limit. This tracks
    # the account's peak equity across the WHOLE test (not just today) and
    # halts all new trades if equity falls too far below that peak.
    "equity_drawdown_stop_enabled": True,
    "equity_drawdown_stop_pct":     15.0,  # halt if equity is 15% below peak ($1,200 on $8k = ~7 max losses)

    "usd_to_zar":         18.0,
    "gbp_usd":            1.27,   # GBP/USD rate for UK stock sizing (pence→USD conversion)
    "min_uk_shares":      50,     # IBKR UK commission ≈£6/trade; need ≥50 shares to be viable
    "daily_target_usd":   50.0,
    "daily_loss_usd":     35.0,

    "max_trades_per_day": 30,
    "trade_cooldown_min": 0,
    "commission_per_trade_usd": 1.5,  # IBKR minimum $1.50/side; round trip = $3

    "regime_chop_below":  0.003,
    "regime_vol_above":   0.010,
    "min_atr_pct":        0.002,

    "spread_pct":         0.0002,
    "slippage_atr_frac":  0.10,
    "execution_1m_enabled": True,
    "execution_1m_min_score": 55,
    "smc_confirmation_enabled": True,
    "smc_min_score": 50,
    "auto_disable_min_trades": 8,
    "auto_disable_max_avg_r": -0.1,

    "max_spread_atr_ratio": 0.35,   # bid-ask spread must be < 35% of ATR to enter
    "sector_headwind_pct":  0.005,  # block if sector ETF is ≥0.5% against trade direction

    "rsi_buy_below":      45,
    "rsi_sell_above":     55,

    # ── PER-SYMBOL STRATEGY MODES (backtest-validated June 2026) ─────────────
    # MOMENTUM mode: RSI > 55 for BUY + EMA50 gate + vsurge >= 1.2
    "momentum_symbols": {
        "NVDA","TSLA","KLAC",
    },
    # STRICT-VOL mode: baseline signal + vsurge >= 1.5
    "strict_vol_symbols": { "GOOGL","CRWD","SEDG","LUNR" },
    # EMA50 mode: baseline signal + price must be above/below EMA50
    "ema50_symbols":      { "AMD","MPWR","ZM","AI","CCI" },
    # Standard mode: META, SLV, LMT, BAC, STT, BSX

    "vsurge_momentum":    1.2,   # EMA/RSI/MACD already qualify trend; 1.2x confirms it
    "vsurge_pullback":    1.15,  # relaxed for live UK/forex scans to allow valid pullbacks through
    "vsurge_mean_rev":    1.0,   # allow mean-reversion on quieter sessions / MIDPOINT forex bars
    "vsurge_bbs":         1.0,   # remove extra volume penalty for UK/forex squeeze candidates
    "vsurge_strict":      1.5,   # 50% above avg vol for strict-vol symbols
    "mean_rev_min_score_us": 75,
    "mean_rev_min_score_uk": 68,
    "mean_rev_min_score_forex": 70,
    "bbs_min_score_us":    68,
    "bbs_min_score_uk":    68,
    "bbs_min_score_forex": 70,

    # ── VWAP REVERSION STRATEGY ─────────────────────────────────────────────
    "vwap_enabled":           False,  # DISABLED — probability engine's MEAN_REV already uses VWAP z-score with proper regime guards
    "vwap_entry_sd":          2.0,    # enter when price extends ≥2.0 SD from VWAP
    "vwap_deep_sd":           2.5,    # deeper extension bonus (scale-in level)
    "vwap_stop_sd":           3.0,    # stop loss at 3.0 SD beyond VWAP
    "vwap_target":            "vwap", # target = VWAP itself (full mean reversion)
    "vwap_vol_exhaust_mult":  2.0,    # volume bar must be ≥2x 20-bar avg (exhaustion spike)
    "vwap_cvd_diverge":       True,   # require CVD divergence at entry
    "vwap_min_bars":          12,     # minimum bars before VWAP SD is stable (~1h of 5-min bars)
    "vwap_skip_first_mins":   15,     # skip first 15 min after open (momentum dominates)
    "vwap_skip_last_mins":    10,     # skip last 10 min before close (imbalance flows)
    "vwap_min_score":         60,     # minimum score to fire

    # ── PER-SYMBOL RR — all US stocks target 5R ──────────────────────────────
    # $160 risk × 5R = $800 TP per trade at full size
    "symbol_rr": {
        "SPY":   2.0,  "QQQ":  2.0,  "TQQQ": 2.0,   # ETFs — tighter range
        "AMZN":  3.0,                                  # lower beta, cap at 3R
    },

    # BACKTEST-VALIDATED SYMBOLS — universe scans June 2026
    # Batch 1 (100 symbols): AMD, USO, AMAT, SLV, KLAC, TSLA, LMT, CRWD, BAC, GOOGL
    # Batch 2 (100 symbols): MPWR, ZM, BSX, STT (skipped SQQQ/SH/SOXL/UVXY — leveraged/inverse ETFs)
    # Batch 3 (100 symbols): CLF, BE, BLNK, SEDG, LI, STEM, BEAM, AI, CPRI, LUNR, ENPH, PCTY, XPEV, CCI
    # Also keeping META + NVDA (proven edge but <20 trades in scanner, kept on quality)
    "stocks_us": [
        "META",   # standard   — PF 11.45, WR 100% (kept: proven edge)
        "NVDA",   # momentum   — PF 1.36,  WR 67%  (kept: proven edge)
        "AMD",    # ema50      — PF 5.19,  WR 79%
        "SLV",    # standard   — PF 1.49,  WR 50%  (long-only)
        "TSLA",   # momentum   — batch 1 scan pass
        "GOOGL",  # strict_vol — batch 1 scan pass
        "KLAC",   # momentum   — batch 1 scan pass
        "LMT",    # standard   — batch 1 scan pass
        "CRWD",   # strict_vol — batch 1 scan pass
        "BAC",    # standard   — batch 1 scan pass
        "MPWR",   # ema50      — batch 2 scan pass  PF 2.37, WR 59.5%
        "ZM",     # ema50      — batch 2 scan pass  PF 1.96, WR 64.1%
        "BSX",    # standard   — batch 2 scan pass  PF 1.65, WR 47.8%
        "STT",    # standard   — batch 2 scan pass  PF 1.91, WR 57.1%
        "SM",     # standard   — batch 2 scan pass  PF 2.00, WR 51.5%  (SM Energy)
        "BE",     # standard   — batch 3 scan pass
        "SEDG",   # strict_vol — batch 3 scan pass
        "BEAM",   # standard   — batch 3 scan pass
        "AI",     # ema50      — batch 3 scan pass
        "LUNR",   # strict_vol — batch 3 scan pass
        "ENPH",   # standard   — batch 3 scan pass
        "PCTY",   # standard   — batch 3 scan pass
        "CCI",    # ema50      — batch 3 scan pass
    ],
    # Symbols that can only be bought (BUY signals only)
    "long_only_symbols": {"TQQQ","QQQ","SPY","GLD","SLV","IWM"},
    # LSE Level-1 data subscription confirmed active (GSK win 24 Jun 2026).
    # 10:00–17:30 SAST window, 15-min bars, 2R target, stop_floor_pct_uk=0.3%.
    "stocks_uk": ["VOD","BARC","HSBA","GSK","ULVR","AZN","LLOY"],
    # Sydney session (02:00–08:00 SAST): AUD and NZD pairs are most liquid.
    # Position sizes at $8k / 2% risk = ~177k AUDUSD / ~228k NZDUSD units —
    # both above IBKR's 25k IdealPro minimum, so ECN routing applies (~0.5 pip spread).
    "forex":   ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "NZDUSD"],
    "crypto":  [],  # no edge confirmed June 2026 backtest — disabled
    "futures": [],  # disabled: $5k account gives qty=0 for all micro contracts (MES=$7.5k/contract)

    # INDEX CFDs — daily mean reversion (backtest_index_v3.py, June 2026)
    # SPX: PF=1.92  WR=60.7%  RR=2.0 | NAS100: PF=2.19  WR=65.2%  RR=2.0
    # Strategy: price above SMA200 + RSI(10)<35 + BB lower band touch → BUY
    # Long-only (structural upside bias, central bank support, ETF inflows)
    "indices":              [],  # CFD approval pending
    "index_rr":             {"IBUS500": 2.0, "IBUST100": 2.0, "IBDE30": 2.0, "IBGB100": 2.0},
    "index_stop_floor_pct": 0.005,   # 0.5% floor on daily bars
    "index_atr_stop_mult":  2.5,     # wider stop — mean reversion needs room
    "max_hold_mins_index":  5760,    # 8 trading days × 12h — SL/TP exits earlier
    "ibkr_bar_size_index":  "1 day",
    # European indices — mean reversion, both directions allowed
    # Data proxies: EWG (iShares MSCI Germany) for IBDE30, EWU (iShares MSCI UK) for IBGB100
    # IBKR CFD symbols: IBDE30 = DAX (EUR), IBGB100 = FTSE100 (GBP)
    "indices_eu":           [],  # requires CFD trading enabled in IBKR account settings
    "trade_windows_eu_index_sast": [(10, 0, 19, 30)],  # UK/EU indices: strict 10:00-19:30 SAST Mon-Fri
    "trade_windows_index_us_sast": [(15, 20, 23, 0)],  # US index CFDs: strict 15:20-23:00 SAST Mon-Fri

    "log_file":      "scalper.log",
    "trade_file":    "scalper_trades.json",
    "equity_file":   "scalper_equity.json",   # tracks peak equity across the whole test
    "cooldown_file": "scalper_cooldowns.json",
    "pause_flag_file": "bot_pause.flag",      # written by health_monitor.py to halt new entries # per-symbol cooldown expiry timestamps

    "scan_sec":   890,  # 14m50s: evaluate just before the next 15-min candle close
    "heartbeat_sec": 60,
    "disconnect_debounce_sec": 60,
    "ibkr_hist_timeout_sec": 20,
    "fetch_htf_trends": False,
    "fetch_daily_trends": False,
    "max_same_sector": 2,  # max positions in the same sector at once
}

# Sector groupings — used to enforce correlation limit (max_same_sector)
SECTOR_MAP: dict[str, str] = {
    # Tech
    "AAPL": "tech", "MSFT": "tech", "NVDA": "tech", "AMD": "tech",
    "META": "tech", "GOOGL": "tech", "AMZN": "tech", "TSLA": "tech",
    "CRM": "tech", "ORCL": "tech", "INTC": "tech", "QCOM": "tech",
    # Finance
    "JPM": "finance", "GS": "finance", "BAC": "finance", "MS": "finance",
    "C": "finance", "WFC": "finance", "BLK": "finance", "AXP": "finance",
    # Energy
    "XOM": "energy", "CVX": "energy", "COP": "energy", "SLB": "energy",
    # Healthcare
    "JNJ": "health", "UNH": "health", "PFE": "health", "MRK": "health",
    "ABBV": "health", "LLY": "health",
    # Consumer
    "WMT": "consumer", "COST": "consumer", "HD": "consumer", "TGT": "consumer",
    "NKE": "consumer", "MCD": "consumer",
    # UK Finance / Banks
    "LLOY": "uk_finance", "BARC": "uk_finance", "HSBA": "uk_finance",
    "NWG": "uk_finance", "STAN": "uk_finance", "LGEN": "uk_finance",
    "AV": "uk_finance", "PHNX": "uk_finance",
    # UK Energy
    "BP": "uk_energy", "SHEL": "uk_energy", "SSE": "uk_energy",
    # UK Consumer / Retail
    "TSCO": "uk_consumer", "SBRY": "uk_consumer", "MKS": "uk_consumer",
    "NEXT": "uk_consumer", "JD": "uk_consumer", "ULVR": "uk_consumer",
    # UK Mining / Materials
    "GLEN": "uk_mining", "AAL": "uk_mining", "RIO": "uk_mining",
    "BHP": "uk_mining", "FRES": "uk_mining",
    # UK Pharma
    "AZN": "uk_pharma", "GSK": "uk_pharma",
    # UK Telecom / Tech
    "VOD": "uk_telecom", "BT": "uk_telecom",
    # Forex (each pair is its own group — no correlation limit applies)
    "AUDUSD": "forex_comm", "NZDUSD": "forex_comm", "EURUSD": "fx_eur",
    "GBPUSD": "fx_gbp", "USDJPY": "fx_jpy", "USDCAD": "fx_cad",
    "USDCHF": "fx_chf",
}

def _sector_of(sym: str) -> str:
    return SECTOR_MAP.get(sym, f"other_{sym}")

def _mklog():
    lg = logging.getLogger("ScalpBot")
    if lg.handlers:
        return lg  # already configured — don't add duplicate handlers on re-import/restart
    lg.setLevel(logging.DEBUG)
    lg.propagate = False   # prevent root logger (ib_insync's handler) from duplicating our messages
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s","%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(CFG["log_file"]); fh.setLevel(logging.DEBUG); fh.setFormatter(fmt)
    lg.addHandler(fh)
    # Only echo to console when running interactively — when nohup redirects stderr
    # to the log file (2>&1) the StreamHandler would write every line twice.
    if sys.stderr.isatty():
        ch = logging.StreamHandler(); ch.setLevel(logging.INFO); ch.setFormatter(fmt)
        lg.addHandler(ch)
    return lg

log = _mklog()


# ══════════════════════════════════════════════════════════════════════════════
#  IBKR LIVE DATA FETCHER
#  Pulls real 5-minute bars directly from IBKR — no yfinance for live signals.
# ══════════════════════════════════════════════════════════════════════════════

class IBKRData:
    """
    Fetches historical bars from IBKR for signal calculation.
    Falls back to a minimal DataFrame of zeros if the request fails,
    which will produce no signal (safe default).
    """

    def __init__(self, ib: IB):
        self.ib = ib
        self._cache: dict = {}
        self._hmds_block_until: datetime | None = None
        self._hmds_block_reason = ""
        self._hmds_skip_log: dict[str, datetime] = {}
        self._hist_req_times: list[datetime] = []
        self._last_hist_req_ts: float = 0.0

    def block_hmds(self, reason: str, minutes: int = 60):
        self._hmds_block_until = datetime.now() + timedelta(minutes=minutes)
        self._hmds_block_reason = reason

    def _hmds_blocked(self) -> bool:
        return bool(self._hmds_block_until and datetime.now() < self._hmds_block_until)

    def hmds_blocked(self) -> bool:
        return self._hmds_blocked()

    def hmds_block_reason(self) -> str:
        return self._hmds_block_reason

    def _skip_hmds(self, label: str):
        if self._hmds_blocked():
            now = datetime.now()
            last = self._hmds_skip_log.get(label)
            if not last or (now - last).total_seconds() >= 300:
                log.debug(f"{label}: HMDS historical data paused — {self._hmds_block_reason}")
                self._hmds_skip_log[label] = now
            return True
        return False

    def _before_hist_request(self, label: str) -> bool:
        """Centralized IBKR historical-data pacing guard.

        IBKR HMDS can lock or disconnect sessions when historical requests burst.
        Keep requests well below the common 60-per-10-min threshold and never
        faster than one request every 3 seconds.
        """
        if self._skip_hmds(label):
            return False
        now = datetime.now()
        cutoff = now - timedelta(minutes=10)
        self._hist_req_times = [t for t in self._hist_req_times if t > cutoff]
        if len(self._hist_req_times) >= 45:
            self.block_hmds("Local HMDS pacing guard: 45 historical requests in 10 minutes", minutes=10)
            if _HAS_DB:
                try:
                    _ss.set_status("hmds_blocked", True)
                    _ss.set_status("hmds_block_reason", self._hmds_block_reason)
                except Exception:
                    pass
            log.warning(f"{label}: local HMDS pacing guard tripped; pausing historical data")
            return False
        elapsed = time.time() - self._last_hist_req_ts
        if elapsed < 3.0:
            self.ib.sleep(3.0 - elapsed)
        self._last_hist_req_ts = time.time()
        self._hist_req_times.append(datetime.now())
        if _HAS_DB:
            try:
                _ss.set_status("hmds_req_10m", len(self._hist_req_times))
            except Exception:
                pass
        return True

    def _bars_to_df(self, bars):
        if not bars:
            log.debug("IBKR bars: reqHistoricalData returned an empty/no result")
            return None
        if len(bars) < 15:
            log.debug(f"IBKR bars: only {len(bars)} bars returned (need 15+) — "
                      f"likely thin/illiquid period (e.g. pre-market), not an error")
            return None
        df = util.df(bars)
        df = df.rename(columns={"open":"Open","high":"High","low":"Low",
                                 "close":"Close","volume":"Volume"})
        df.index = pd.to_datetime(df.index if "date" not in df.columns
                                  else df["date"], utc=True)
        df = df[["Open","High","Low","Close","Volume"]].copy()
        return df

    def fetch(self, contract, what_to_show: str = "TRADES", bar_size: str = "",
              use_rth: bool = False, duration_str: str = "1 D"):
        bar_size = bar_size or CFG["ibkr_bar_size"]
        key = f"{contract.symbol}_{contract.secType}_{bar_size}_rth{int(use_rth)}"
        now = datetime.now()
        cached = self._cache.get(key)
        # 5-minute cache: 15-min bars only form every 15 min so there is nothing
        # new to fetch more often. This keeps total IBKR historical-data requests
        # well under the 60-per-10-minute pacing limit that causes disconnects.
        if cached and (now - cached[1]).total_seconds() < 300:
            return cached[0]
        if not self._before_hist_request(f"IBKR bars {contract.symbol}"):
            return None
        try:
            bars = self.ib.reqHistoricalData(
                contract,
                endDateTime="",
                durationStr=duration_str,
                barSizeSetting=bar_size,
                whatToShow=what_to_show,
                useRTH=use_rth,
                formatDate=2,
                keepUpToDate=False,
                timeout=CFG["ibkr_hist_timeout_sec"],
            )
            df = self._bars_to_df(bars)
            self._cache[key] = (df, now)
            return df
        except Exception as e:
            log.debug(f"IBKR bars {contract.symbol}: {e}")
            return None

    def fetch_daily(self, contract, what_to_show: str = "MIDPOINT"):
        """Fetch 1 year of daily bars — used for index CFD mean reversion signal."""
        key = f"{contract.symbol}_{contract.secType}_1day"
        now = datetime.now()
        cached = self._cache.get(key)
        if cached and (now - cached[1]).total_seconds() < 600:   # 10-min cache for daily bars
            return cached[0]
        if not self._before_hist_request(f"IBKR daily bars {contract.symbol}"):
            return None
        try:
            bars = self.ib.reqHistoricalData(
                contract,
                endDateTime="",
                durationStr="2 Y",
                barSizeSetting="1 day",
                whatToShow=what_to_show,
                useRTH=True,
                formatDate=2,
                keepUpToDate=False,
                timeout=CFG["ibkr_hist_timeout_sec"],
            )
            df = self._bars_to_df(bars)
            self._cache[key] = (df, now)
            return df
        except Exception as e:
            log.debug(f"IBKR daily bars {contract.symbol}: {e}")
            return None

    def get_hourly_trend(self, data_contract, sym: str, what_to_show: str = "TRADES") -> str:
        """
        Returns 'UP', 'DOWN', or 'FLAT' based on 1-hour EMA9 vs EMA21.
        Cached for 60 minutes.

        Cold HMDS trend fetches are disabled by default. During live scans they
        multiply historical requests per instrument and trip IBKR pacing. If a
        cached trend exists, use it; otherwise return FLAT and let the 15-minute
        scan proceed from its main bars only.
        Used as a trend filter: only trade 15-min signals that align with the
        1-hour trend. Research shows this lifts win rate from ~39% to ~58%.
        """
        key = f"{sym}_1h_trend"
        now = datetime.now()
        cached = self._cache.get(key)
        if cached and (now - cached[1]).total_seconds() < 3600:
            return cached[0]
        if not CFG.get("fetch_htf_trends", False):
            self._cache[key] = ("FLAT", now)
            return "FLAT"
        if not self._before_hist_request(f"{sym} 1H trend"):
            return "FLAT"
        try:
            bars = self.ib.reqHistoricalData(
                data_contract,
                endDateTime="",
                durationStr="5 D",
                barSizeSetting="1 hour",
                whatToShow=what_to_show,
                useRTH=True,
                formatDate=2,
                keepUpToDate=False,
                timeout=CFG["ibkr_hist_timeout_sec"],
            )
            df = self._bars_to_df(bars)
            if df is None or len(df) < 10:
                self._cache[key] = ("FLAT", now)
                return "FLAT"
            ema9  = float(ta.trend.EMAIndicator(df["Close"], 9).ema_indicator().iloc[-1])
            ema21 = float(ta.trend.EMAIndicator(df["Close"], 21).ema_indicator().iloc[-1])
            trend = "UP" if ema9 > ema21 else "DOWN"
            self._cache[key] = (trend, now)
            log.debug(f"{sym} 1H trend: {trend} (EMA9={ema9:.2f} EMA21={ema21:.2f})")
            return trend
        except Exception as e:
            log.debug(f"{sym} 1H trend fetch failed: {e}")
            self._cache[key] = ("FLAT", now)
            return "FLAT"

    def get_daily_trend(self, data_contract, sym: str, what_to_show: str = "TRADES") -> str:
        """
        Returns 'UP', 'DOWN', or 'FLAT' based on daily EMA9 vs EMA21.
        Cached for 4 hours.

        Cold HMDS trend fetches are disabled by default for the same pacing
        reason as get_hourly_trend().
        Used as a 3rd timeframe trend filter alongside the 1H trend.
        """
        key = f"{sym}_1d_trend"
        now = datetime.now()
        cached = self._cache.get(key)
        if cached and (now - cached[1]).total_seconds() < 14400:
            return cached[0]
        if not CFG.get("fetch_daily_trends", False):
            self._cache[key] = ("FLAT", now)
            return "FLAT"
        if not self._before_hist_request(f"{sym} 1D trend"):
            return "FLAT"
        try:
            bars = self.ib.reqHistoricalData(
                data_contract,
                endDateTime="",
                durationStr="60 D",
                barSizeSetting="1 day",
                whatToShow=what_to_show,
                useRTH=True,
                formatDate=2,
                keepUpToDate=False,
                timeout=CFG["ibkr_hist_timeout_sec"],
            )
            df = self._bars_to_df(bars)
            if df is None or len(df) < 10:
                self._cache[key] = ("FLAT", now)
                return "FLAT"
            ema9  = float(ta.trend.EMAIndicator(df["Close"], 9).ema_indicator().iloc[-1])
            ema21 = float(ta.trend.EMAIndicator(df["Close"], 21).ema_indicator().iloc[-1])
            trend = "UP" if ema9 > ema21 else "DOWN"
            self._cache[key] = (trend, now)
            log.debug(f"{sym} 1D trend: {trend} (EMA9={ema9:.2f} EMA21={ema21:.2f})")
            return trend
        except Exception as e:
            log.debug(f"{sym} 1D trend fetch failed: {e}")
            self._cache[key] = ("FLAT", now)
            return "FLAT"

    def get_spread_pct(self, contract) -> float:
        """Disabled live quote snapshot.

        reqTickers()/snapshot top-of-book requests require live market-data
        subscriptions and were flooding IBKR with TOP/ALL subscription errors.
        Strategy scans use historical bars; if no live quote subscription is
        available, do not run a spread gate from live quotes.
        """
        return 0.0


# ══════════════════════════════════════════════════════════════════════════════
#  YFINANCE  — only used for --backtest (daily historical bars)
# ══════════════════════════════════════════════════════════════════════════════

class YFData:
    _cache: dict = {}

    @classmethod
    def daily(cls, sym, days=365):
        key = f"{sym}_1d"
        now = datetime.now()
        cached = cls._cache.get(key)
        if cached and (now - cached[1]).seconds < 60:
            return cached[0]
        try:
            df = yf.Ticker(sym).history(
                start=now - timedelta(days=days), end=now,
                interval="1d", auto_adjust=True)
            if df is None or df.empty or len(df) < 60:
                return None
            cls._cache[key] = (df, now)
            return df
        except Exception as e:
            log.debug(f"yf {sym}: {e}")
            return None


# ══════════════════════════════════════════════════════════════════════════════
#  SHARED ANALYTICS (regime, signal, sizing) — same for live and backtest
# ══════════════════════════════════════════════════════════════════════════════

# Per-market CHOP thresholds — forex/futures move much less in % terms than stocks.
# EURUSD London open ATR ~5-15 pips = 0.05-0.14%. Stocks need 0.2%+ to be meaningful.
_CHOP_THRESHOLDS = {
    "forex":     0.0003,  # 0.03% ~ 3 pips EURUSD — catches active London/NY
    "future":    0.0003,  # 0.03% ~ 1.5pts MES at 5000
    "stock_us":  0.0002,  # 0.02% — only block if SPY is basically flat
    "stock_uk":  0.0015,  # lowered: LLOY/VOD/BARC have low nominal prices (70-250p)
    "crypto":    0.003,   # 0.3%  — crypto needs bigger moves to overcome spread
    "metal":     0.0005,  # 0.05% — gold/silver active sessions without blocking normal ranges
    "energy":    0.0010,  # 0.10% — oil/gas need movement but not stock-like volatility
    "index_cfd":    0.0003,  # 0.03% — indices move tighter % than individual stocks
    "index_cfd_eu": 0.0003,  # same threshold for European index CFDs
}

def regime_of(df, market="stock_us"):
    """ATR-based regime: CHOP / TREND / VOLATILE. Uses per-market thresholds."""
    if df is None or len(df) < 15:
        return "CHOP", 0.0
    atr_s = ta.volatility.AverageTrueRange(
        df["High"], df["Low"], df["Close"], 14).average_true_range()
    atr   = float(atr_s.iloc[-1])
    price = float(df["Close"].iloc[-1])
    atr_pct = atr / price if price > 0 else 0
    chop_threshold = _CHOP_THRESHOLDS.get(market, CFG["regime_chop_below"])
    if atr_pct < chop_threshold:
        return "CHOP", atr_pct
    elif atr_pct > CFG["regime_vol_above"]:
        return "VOLATILE", atr_pct
    return "TREND", atr_pct

REGIME_COLOR = {"CHOP": Fore.YELLOW, "TREND": Fore.GREEN, "VOLATILE": Fore.RED}


def evaluate_signal(df, mode="standard"):
    """
    Returns signal dict or None. Works on any OHLCV DataFrame.
    mode: "standard"   — EMA9/21 + VWAP + MACD slope + RSI(9) reversal (ETFs + META)
          "momentum"   — RSI>55 BUY + EMA50 gate + vsurge>=1.2 (stocks: AAPL/NVDA/TSLA/etc)
          "strict_vol" — baseline + vsurge>=1.5 only (GOOGL/ORCL/IWM/USO)
          "ema50"      — baseline + price must be above/below EMA50 (MSFT/AMD)
    """
    if df is None or len(df) < 30:
        return None
    c = df["Close"]; h = df["High"]; l = df["Low"]
    v = df["Volume"] if "Volume" in df.columns else None

    ema9  = ta.trend.EMAIndicator(c, 9).ema_indicator()
    ema21 = ta.trend.EMAIndicator(c, 21).ema_indicator()
    rsi   = ta.momentum.RSIIndicator(c, 9).rsi()
    macd  = ta.trend.MACD(c, 12, 26, 9)
    atr   = ta.volatility.AverageTrueRange(h, l, c, 14).average_true_range()

    if v is not None and float(v.sum()) > 0:
        tp     = (h + l + c) / 3
        vwap   = (tp * v).rolling(20).sum() / v.rolling(20).sum()
        vol_ma = v.rolling(20).mean()
        vsurge = float(v.iloc[-1] / vol_ma.iloc[-1]) if float(vol_ma.iloc[-1]) > 0 else 1.0
    else:
        vwap   = c.rolling(20).mean()
        vsurge = 1.0

    price   = float(c.iloc[-1])
    e9      = float(ema9.iloc[-1])
    e21     = float(ema21.iloc[-1])
    rsi_v   = float(rsi.iloc[-1])
    macd_h  = macd.macd_diff()
    mh      = float(macd_h.iloc[-1])
    mh_prev = float(macd_h.iloc[-2]) if len(macd_h) >= 2 else mh
    atr_v   = float(atr.iloc[-1])
    vwap_v  = float(vwap.iloc[-1])

    trend_up        = e9 > e21
    trend_down      = e9 < e21
    macd_accel_up   = mh > mh_prev
    macd_accel_down = mh < mh_prev

    # ── Volume gate (per-mode threshold) ─────────────────────────────────────
    if mode == "momentum":
        vsurge_min = CFG.get("vsurge_momentum", 1.2)
    elif mode == "strict_vol":
        vsurge_min = CFG.get("vsurge_strict", 1.5)
    else:
        vsurge_min = 0.7
    if vsurge < vsurge_min:
        return None

    # ── EMA50 gate (momentum and ema50 modes) ────────────────────────────────
    ema50_gate = mode in ("momentum", "ema50")
    if ema50_gate:
        ema50  = ta.trend.EMAIndicator(c, 50).ema_indicator()
        e50_v  = float(ema50.iloc[-1]) if len(ema50.dropna()) > 0 else price
        above_ema50 = price > e50_v
        below_ema50 = price < e50_v
    else:
        above_ema50 = True
        below_ema50 = True

    direction = None

    if mode == "momentum":
        # Ride established trends: RSI > 55 confirms momentum is already running
        if trend_up and rsi_v > 55 and macd_accel_up and price > vwap_v and above_ema50:
            direction = "BUY"
        elif trend_down and rsi_v < 45 and macd_accel_down and price < vwap_v and below_ema50:
            direction = "SELL"
    else:
        # Standard / strict_vol / ema50 — reversal/pullback entry
        if trend_up and price > vwap_v and mh > 0 and macd_accel_up and rsi_v < 65:
            direction = "BUY"
        # Pullback BUY: RSI oversold in uptrend + MACD recovering but must be ≥ 0
        # (mh >= 0 prevents entering long while MACD is still negative — avoids catching
        #  a dead-cat bounce in a downtrend that just looks temporarily oversold)
        elif trend_up and rsi_v < CFG["rsi_buy_below"] and macd_accel_up and mh >= 0 and above_ema50:
            direction = "BUY"
        elif trend_down and price < vwap_v and mh < 0 and macd_accel_down and rsi_v > 35:
            direction = "SELL"
        # Pullback SELL: RSI overbought in downtrend + MACD must be ≤ 0
        elif trend_down and rsi_v > CFG["rsi_sell_above"] and macd_accel_down and mh <= 0 and below_ema50:
            direction = "SELL"

    if direction is None:
        return None

    return {"price": price, "atr": atr_v, "rsi": rsi_v, "macd_h": mh,
            "vwap": vwap_v, "vsurge": vsurge, "direction": direction, "mode": mode}


def evaluate_signal_index(df, long_only=True):
    """
    Mean reversion signal for index CFDs on daily bars.
    Requires 200+ bars (SMA200 filter).
    LONG:  above SMA200 + RSI(10) < 35 + Bollinger Band lower touch (%B < 0.25)
    SHORT: below SMA200 + RSI(10) > 65 + upper band touch (%B > 0.75)
    """
    if df is None or len(df) < 210:
        return None
    c = df["Close"]; h = df["High"]; l = df["Low"]

    sma200  = c.rolling(200).mean()
    rsi     = ta.momentum.RSIIndicator(c, 10).rsi()
    atr_ind = ta.volatility.AverageTrueRange(h, l, c, 14).average_true_range()
    bb      = ta.volatility.BollingerBands(c, window=20, window_dev=2)
    pct_b   = bb.bollinger_pband()

    price    = float(c.iloc[-1])
    sma200_v = float(sma200.iloc[-1]) if not pd.isna(sma200.iloc[-1]) else price
    rsi_v    = float(rsi.iloc[-1])    if not pd.isna(rsi.iloc[-1])    else 50.0
    atr_v    = float(atr_ind.iloc[-1]) if not pd.isna(atr_ind.iloc[-1]) else price * 0.01
    pct_b_v  = float(pct_b.iloc[-1])  if not pd.isna(pct_b.iloc[-1])  else 0.5

    direction = None
    if price > sma200_v and rsi_v < 35 and pct_b_v < 0.25:
        direction = "BUY"
    elif price < sma200_v and not long_only and rsi_v > 65 and pct_b_v > 0.75:
        direction = "SELL"

    if direction is None:
        return None
    return {"price": price, "atr": atr_v, "rsi": rsi_v, "macd_h": 0.0,
            "vsurge": 1.0, "direction": direction}


def _sc(condition, pts):
    """Return pts if condition is True, else 0. Used to build probability scores."""
    return pts if condition else 0


def _tod_score_adj(market: str, now_dt: datetime | None = None) -> int:
    """Time-of-day score adjustment aligned to the active SAST market window."""
    now_dt = now_dt or _now_trade_tz()
    mins = _minutes_since_midnight(now_dt)
    if not _global_trading_enabled(now_dt):
        return -12
    if market == "stock_uk":
        if 9 * 60 + 50 <= mins < 12 * 60:
            return 6
        if 18 * 60 <= mins <= 19 * 60 + 30:
            return 4
        return 0
    if market in {"stock_us", "future", "index_cfd"}:
        if 17 * 60 + 30 <= mins < 20 * 60:
            return -8
        if 15 * 60 + 20 <= mins < 17 * 60:
            return 10
        if 0 <= mins <= 2 * 60:
            return 5
        return 0
    if market == "forex":
        if 23 * 60 <= mins or mins < 2 * 60:
            return 4
        if 9 * 60 + 50 <= mins < 15 * 60 + 20:
            return 8
        if 15 * 60 + 20 <= mins <= 22 * 60 + 59:
            return 7
        return 2
    return 0


def _compute_vpin(h_arr, l_arr, c_arr, v_arr, window: int = 20) -> float:
    """
    Simplified VPIN using bulk volume classification (OHLCV).
    Buy volume fraction = (close - low) / (high - low) per bar.
    VPIN = rolling mean of |buy_frac - 0.5| * 2 over `window` bars → [0, 1].
    Values > 0.7 indicate elevated informed-trading toxicity.
    """
    import numpy as np
    n = len(h_arr)
    if n < window:
        return 0.0
    buy_frac = []
    for i in range(n):
        rng = h_arr[i] - l_arr[i]
        if rng > 0:
            buy_frac.append((c_arr[i] - l_arr[i]) / rng)
        else:
            buy_frac.append(0.5)
    arr = np.array(buy_frac[-window:])
    return float(np.mean(np.abs(arr - 0.5)) * 2)


def _score_band(score: float) -> str:
    try:
        val = int(round(float(score)))
    except Exception:
        return "0-59"
    if val < 60:
        return "0-59"
    if val < 70:
        return "60-69"
    if val < 80:
        return "70-79"
    if val < 90:
        return "80-89"
    return "90-100"


def _market_session_name(market: str, now_dt: datetime | None = None) -> str:
    now_sast = now_dt or _now_trade_tz()
    mins = _minutes_since_midnight(now_sast)
    if not _global_trading_enabled(now_sast):
        return "WEEKEND_CLOSED"
    if market == "stock_uk":
        if 9 * 60 + 50 <= mins < 12 * 60:
            return "LONDON_OPEN"
        if 12 * 60 <= mins <= 19 * 60 + 30:
            return "LONDON"
        return "OFF_HOURS"
    if market in ("stock_us", "future", "index_cfd", "index_cfd_eu"):
        if 17 * 60 + 30 <= mins < 20 * 60:
            return "US_LUNCH"
        if 15 * 60 + 20 <= mins < 17 * 60:
            return "NEW_YORK_OPEN"
        if 17 * 60 <= mins or mins <= 2 * 60:
            return "NEW_YORK"
        return "OFF_HOURS"
    if market in {"forex", "metal", "energy"}:
        if 23 * 60 <= mins or mins < 2 * 60:
            return "SYDNEY_FX"
        if 9 * 60 + 50 <= mins < 15 * 60 + 20:
            return "LONDON_FX"
        if 15 * 60 + 20 <= mins <= 22 * 60 + 59:
            return "NEW_YORK_FX"
        return "ASIA_FX"
    return "GENERAL"


def _session_score_for_market(market: str, now_dt: datetime | None = None) -> int:
    session = _market_session_name(market, now_dt=now_dt)
    if session in {"LONDON_OPEN", "NEW_YORK_OPEN"}:
        return 90
    if session in {"LONDON", "NEW_YORK", "LONDON_FX", "NEW_YORK_FX"}:
        return 85
    if session == "SYDNEY_FX":
        return 70
    if session == "US_LUNCH":
        return 35
    if session == "ASIA_FX":
        return 60
    if session == "WEEKEND_CLOSED":
        return 0
    return 45


def _risk_reward_score(rr_ratio: float) -> int:
    if rr_ratio >= 2.5:
        return 95
    if rr_ratio >= 2.0:
        return 85
    if rr_ratio >= 1.5:
        return 70
    return 50


def evaluate_execution_1m(df_1m, direction: str):
    if df_1m is None or len(df_1m) < 20:
        return 0, "unavailable"
    c = df_1m["Close"]
    v = df_1m["Volume"] if "Volume" in df_1m.columns else None
    ema5 = ta.trend.EMAIndicator(c, 5).ema_indicator()
    ema13 = ta.trend.EMAIndicator(c, 13).ema_indicator()
    rsi = ta.momentum.RSIIndicator(c, 7).rsi()
    macd = ta.trend.MACD(c, 6, 13, 5).macd_diff()
    score = 0
    is_buy = direction == "BUY"
    if (ema5.iloc[-1] > ema13.iloc[-1]) if is_buy else (ema5.iloc[-1] < ema13.iloc[-1]):
        score += 30
    if (c.iloc[-1] > c.iloc[-2]) if is_buy else (c.iloc[-1] < c.iloc[-2]):
        score += 20
    if (macd.iloc[-1] > macd.iloc[-2]) if is_buy else (macd.iloc[-1] < macd.iloc[-2]):
        score += 20
    rsi_v = float(rsi.dropna().iloc[-1]) if len(rsi.dropna()) else 50.0
    if (50 <= rsi_v <= 72) if is_buy else (28 <= rsi_v <= 50):
        score += 20
    if v is not None and len(v) >= 5:
        vol_ma = float(v.tail(5).mean() or 0)
        if vol_ma > 0 and float(v.iloc[-1]) >= vol_ma:
            score += 10
    result = "confirmed" if score >= CFG.get("execution_1m_min_score", 55) else "not_confirmed"
    return int(score), result


def evaluate_smc_confirmation(df_1m, direction: str):
    if df_1m is None or len(df_1m) < 12:
        return 0, False, "unavailable"
    highs = df_1m["High"]
    lows = df_1m["Low"]
    closes = df_1m["Close"]
    recent_high = float(highs.iloc[-6:-1].max())
    recent_low = float(lows.iloc[-6:-1].min())
    last_close = float(closes.iloc[-1])
    prev_close = float(closes.iloc[-2])
    score = 0
    if direction == "BUY":
        if last_close > recent_high:
            score += 45
        if float(lows.iloc[-1]) > recent_low:
            score += 25
        if last_close > prev_close:
            score += 15
        if float(lows.tail(3).min()) >= float(lows.tail(6).min()):
            score += 15
    else:
        if last_close < recent_low:
            score += 45
        if float(highs.iloc[-1]) < recent_high:
            score += 25
        if last_close < prev_close:
            score += 15
        if float(highs.tail(3).max()) <= float(highs.tail(6).max()):
            score += 15
    confirmed = score >= CFG.get("smc_min_score", 50)
    return int(score), confirmed, "confirmed" if confirmed else "not_confirmed"


def _score_details_from_signal(sig: dict, market: str, htf_trend: str = "FLAT", now_dt: datetime | None = None) -> dict:
    score = float(sig.get("score", 0) or 0)
    strategy = sig.get("strategy", sig.get("mode", "standard"))
    rr = CFG.get("rr_ratio", 1.5)
    if market == "stock_uk":
        rr = CFG.get("rr_ratio_uk", rr)
    elif market == "stock_us":
        rr = CFG.get("rr_ratio_us", rr)
    elif market == "forex":
        rr = CFG.get("rr_ratio_forex", rr)
    elif market == "metal":
        rr = CFG.get("rr_ratio_metals", rr)
    elif market == "energy":
        rr = CFG.get("rr_ratio_energies", rr)
    elif market == "crypto":
        rr = CFG.get("crypto_rr", rr)
    explicit_h1_score = sig.get("bias_1h_score")
    if explicit_h1_score is None:
        explicit_h1_score = sig.get("h1_score")
    explicit_m15_score = sig.get("setup_15m_score")
    if explicit_m15_score is None:
        explicit_m15_score = sig.get("m15_score")
    bias_1h_score = float(explicit_h1_score) if explicit_h1_score is not None else 90 if htf_trend in {"UP", "DOWN"} and (
        (htf_trend == "UP" and sig.get("direction") == "BUY") or
        (htf_trend == "DOWN" and sig.get("direction") == "SELL")
    ) else 55 if htf_trend == "FLAT" else 25
    conds = sig.get("conditions", {}) or {}
    setup_hits = 0
    setup_total = 0
    trigger_hits = 0
    trigger_total = 0
    for label, met in conds.items():
        if label in {"strategy", "score", "1M execution", "SMC", "ORB"}:
            continue
        val = bool(met)
        if label in {"EMA 9/21", "EMA50", "VWAP", "VWAP z≥1.2", "VWAP z≥2.0", "RSI extreme",
                     "RSI 50-72", "RSI dip", "ADX < 22", "BB Squeeze", "Squeeze low"}:
            setup_total += 1
            setup_hits += int(val)
        else:
            trigger_total += 1
            trigger_hits += int(val)
    breakdown = {
        "bias_1h": bias_1h_score,
        "setup_15m": float(explicit_m15_score) if explicit_m15_score is not None else int(round((setup_hits / setup_total) * 100)) if setup_total else 0,
        "trigger_5m": int(round((trigger_hits / trigger_total) * 100)) if trigger_total else 0,
        "execution_1m": int(sig.get("execution_1m_score", 0) or 0),
        "smc": int(sig.get("smc_score", 0) or 0),
        "risk_reward": _risk_reward_score(rr),
        "session": _session_score_for_market(market, now_dt=now_dt),
        "strategy": strategy,
    }
    return {
        "score_band": _score_band(score),
        "score_breakdown": breakdown,
        "bias_1h_score": breakdown["bias_1h"],
        "setup_15m_score": breakdown["setup_15m"],
        "trigger_5m_score": breakdown["trigger_5m"],
        "execution_1m_score": breakdown["execution_1m"],
        "smc_score": breakdown["smc"],
        "risk_reward_score": breakdown["risk_reward"],
        "session_score": breakdown["session"],
        "timeframe_setup": "15m+5m",
        "comparison_bucket": "5m+1m" if (sig.get("execution_1m_result") or "").startswith("confirmed") else "5m_direct",
        "forward_phase": "forward_paper",
        "session_name": _market_session_name(market, now_dt=now_dt),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# VWAP REVERSION STRATEGY — pure VWAP + Volume, no EMA/RSI
# ═══════════════════════════════════════════════════════════════════════════════

def _cumulative_vwap(h, l, c, v):
    """
    Compute session VWAP and volume-weighted standard deviation bands.
    Uses the proper formula: Var = Σ(P²·V)/Σ(V) - VWAP², SD = √Var.
    Returns (vwap_array, sd_array) as numpy arrays.
    """
    import numpy as np
    tp = (h + l + c) / 3.0
    cum_tpv = np.cumsum(tp * v)
    cum_v   = np.cumsum(v)
    cum_v[cum_v == 0] = 1e-10
    vwap = cum_tpv / cum_v
    cum_tp2v = np.cumsum(tp**2 * v)
    variance = cum_tp2v / cum_v - vwap**2
    variance = np.clip(variance, 0, None)
    sd = np.sqrt(variance)
    return vwap, sd


def _bulk_cvd(h, l, c, v):
    """
    Cumulative Volume Delta using bulk volume classification.
    Buy fraction = (close - low) / (high - low) per bar.
    CVD = cumulative sum of (buy_vol - sell_vol).
    Returns CVD array.
    """
    import numpy as np
    rng = h - l
    rng[rng == 0] = 1e-10
    buy_frac = (c - l) / rng
    buy_vol  = buy_frac * v
    sell_vol = (1 - buy_frac) * v
    delta    = buy_vol - sell_vol
    return np.cumsum(delta)


def evaluate_vwap(df, market: str = "stock_us", now_dt: datetime | None = None):
    """
    Pure VWAP + Volume reversion strategy.

    Logic:
      1. Compute session VWAP with proper volume-weighted SD bands
      2. When price extends ≥2.0 SD above VWAP → SELL (fade back to VWAP)
      3. When price extends ≥2.0 SD below VWAP → BUY (fade back to VWAP)
      4. Require volume exhaustion: current bar volume ≥2x 20-bar average
      5. Require CVD divergence: price at new extreme but CVD flattening/diverging
      6. Score 0-100 based on extension depth, volume spike, CVD signal

    No EMA, no RSI — strictly VWAP and volume from IBKR.
    Returns signal dict or None.
    """
    import numpy as np

    if not CFG.get("vwap_enabled", True):
        return None

    if df is None or len(df) < 5:
        return None

    # ── Session filter: VWAP resets at the true local market session boundary ─
    df_today = _filter_current_session(df, market, now_dt=now_dt)

    if len(df_today) < CFG.get("vwap_min_bars", 12):
        return None

    c = df_today["Close"].values.astype(float)
    h = df_today["High"].values.astype(float)
    l = df_today["Low"].values.astype(float)
    v = df_today["Volume"].values.astype(float) if "Volume" in df_today.columns else None

    if v is None or np.sum(v) == 0:
        return None

    # ── VWAP + SD bands (today's session only) ───────────────────────────────
    vwap_arr, sd_arr = _cumulative_vwap(h, l, c, v)

    price    = c[-1]
    vwap_now = vwap_arr[-1]
    sd_now   = sd_arr[-1]

    if sd_now <= 0:
        return None

    # How many SDs is price from VWAP?
    vwap_z = (price - vwap_now) / sd_now

    entry_sd = CFG.get("vwap_entry_sd", 2.0)
    deep_sd  = CFG.get("vwap_deep_sd", 2.5)

    # Must be extended ≥ entry_sd from VWAP
    if abs(vwap_z) < entry_sd:
        return None

    # Direction: price above VWAP → sell to fade, price below → buy to fade
    if vwap_z >= entry_sd:
        direction = "SELL"
    elif vwap_z <= -entry_sd:
        direction = "BUY"
    else:
        return None

    # ── Volume exhaustion ─────────────────────────────────────────────────────
    vol_window = min(20, len(v) - 1)
    vol_ma     = np.mean(v[-vol_window-1:-1]) if vol_window > 0 else v[-1]
    vol_ratio  = v[-1] / vol_ma if vol_ma > 0 else 0.0
    exhaust_mult = CFG.get("vwap_vol_exhaust_mult", 2.0)

    # Volume must be elevated (exhaustion spike or sustained participation)
    vol_exhaustion = vol_ratio >= exhaust_mult
    # Also accept if recent 3-bar average is elevated (sustained push)
    vol_sustained  = np.mean(v[-3:]) / vol_ma >= 1.5 if vol_ma > 0 else False

    if not vol_exhaustion and not vol_sustained:
        return None

    # ── CVD divergence ────────────────────────────────────────────────────────
    cvd = _bulk_cvd(h, l, c, v)
    cvd_now   = cvd[-1]
    cvd_prev3 = cvd[-4] if len(cvd) >= 4 else cvd[0]
    cvd_prev5 = cvd[-6] if len(cvd) >= 6 else cvd[0]

    cvd_diverge = False
    if direction == "SELL":
        # Price at high but CVD not making new high → buying exhaustion
        price_rising = price >= np.max(c[-5:]) * 0.999
        cvd_flat     = cvd_now <= cvd_prev3 * 1.02
        cvd_diverge  = price_rising and cvd_flat
    else:
        # Price at low but CVD not making new low → selling exhaustion
        price_falling = price <= np.min(c[-5:]) * 1.001
        cvd_flat      = cvd_now >= cvd_prev3 * 0.98
        cvd_diverge   = price_falling and cvd_flat

    # CVD divergence is preferred but not hard-required if extension is deep (≥2.5 SD)
    require_cvd = CFG.get("vwap_cvd_diverge", True)
    if require_cvd and not cvd_diverge and abs(vwap_z) < deep_sd:
        return None

    # ── Score (0-100) ─────────────────────────────────────────────────────────
    score = 0

    # Extension depth (0-35 points)
    if abs(vwap_z) >= 3.0:
        score += 35
    elif abs(vwap_z) >= deep_sd:
        score += 25
    elif abs(vwap_z) >= entry_sd:
        score += 15

    # Volume spike strength (0-25 points)
    if vol_ratio >= 3.0:
        score += 25
    elif vol_ratio >= exhaust_mult:
        score += 20
    elif vol_sustained:
        score += 12

    # CVD divergence (0-25 points)
    if cvd_diverge:
        score += 25

    # Price stall: last bar range smaller than previous bar (exhaustion candle)
    bar_range_now  = h[-1] - l[-1]
    bar_range_prev = h[-2] - l[-2] if len(h) >= 2 else bar_range_now
    if bar_range_now < bar_range_prev * 0.7:
        score += 10  # narrowing range = momentum dying

    # Consecutive bars on same side of VWAP (overextension)
    same_side = 0
    for i in range(len(c)-1, max(0, len(c)-10), -1):
        if direction == "SELL" and c[i] > vwap_arr[i]:
            same_side += 1
        elif direction == "BUY" and c[i] < vwap_arr[i]:
            same_side += 1
        else:
            break
    if same_side >= 7:
        score += 5

    score = min(100, score)

    min_score = CFG.get("vwap_min_score", 60)
    if score < min_score:
        return None

    # ── ATR for stop/target sizing ────────────────────────────────────────────
    atr14 = float(ta.volatility.AverageTrueRange(
        df_today["High"], df_today["Low"], df_today["Close"], 14
    ).average_true_range().iloc[-1])

    # TP = VWAP itself (full reversion)
    vwap_dist = abs(price - vwap_now)

    # Stop = distance from entry to the 3.0 SD band
    # e.g. entry at 2.2 SD, stop at 3.0 SD → stop_dist = (3.0 - 2.2) * sd
    stop_sd   = CFG.get("vwap_stop_sd", 3.0)
    stop_dist = (stop_sd - abs(vwap_z)) * sd_now

    # Floor: stop must be at least 1× ATR (protects against tiny SD values)
    stop_dist = max(stop_dist, atr14)

    # RR gate: only take the trade if TP distance is at least 1.5× stop distance
    # Avoids trades where we're close to VWAP but far from the stop band
    if vwap_dist < stop_dist * 1.5:
        return None

    _conds = {
        "strategy":      "VWAP_REV",
        "score":         int(score),
        "VWAP z-score":  True,
        f"|z| ≥ {entry_sd}": bool(abs(vwap_z) >= entry_sd),
        f"|z| ≥ {deep_sd}":  bool(abs(vwap_z) >= deep_sd),
        "Vol exhaust":   bool(vol_exhaustion),
        "Vol sustained": bool(vol_sustained),
        "CVD diverge":   bool(cvd_diverge),
        "Price stall":   bool(bar_range_now < bar_range_prev * 0.7),
    }

    return {
        "price": float(price), "atr": float(atr14),
        "rsi": 0.0, "macd_h": 0.0,
        "vwap": float(vwap_now), "vwap_z": round(float(vwap_z), 2),
        "vwap_sd": round(float(sd_now), 6),
        "vsurge": round(float(vol_ratio), 2),
        "stoch_k": 0.0, "stoch_d": 0.0,
        "direction": direction,
        "strategy": "VWAP_REV", "regime": "VWAP", "score": score,
        "adx": 0.0, "atr_ratio": 0.0,
        "mode": "vwap_reversion",
        "vwap_target": float(vwap_now),
        "vwap_stop_dist": round(float(stop_dist), 6),
        "conditions": _conds,
    }


_regime_history = {}  # {sym: previous_regime} — for 2-bar regime switch delay

def evaluate_probability(df, htf_trend: str = "FLAT", orb_direction=None, sym: str = "", market: str = "stock_us", now_dt: datetime | None = None):
    """
    Multi-strategy adaptive probability engine for US/UK stocks.

    Instead of a single fixed strategy the bot now:
      1. Detects the market REGIME for this symbol (TREND / RANGE / VOLATILE / QUIET)
         using ADX and a short-vs-long ATR ratio.
      2. Scores 3 candidate strategies against the current bar:
           MOMENTUM  — ride an established trend (ADX≥22, RSI>50, MACD positive)
           PULLBACK  — buy a dip within a trend (ADX≥22, RSI<50, MACD recovering)
           MEAN_REV  — fade extremes in a ranging market (ADX<22, RSI<38 or >62,
                       VWAP z-score stretched ≥1.2 SD)
      3. Each strategy produces a 0–100 confidence score from weighted indicator
         components. Only the strategy with the highest score that clears its
         minimum threshold (60–65 depending on strategy) fires.
      4. The score is returned in the signal dict so position_size() can scale
         size accordingly (60-74 → 0.5×, 75-84 → 0.75×, 85-100 → 1.0×).

    Returns a signal dict (same shape as evaluate_signal()) plus extra keys:
      strategy: "MOMENTUM" | "PULLBACK" | "MEAN_REV"
      regime:   "TREND"    | "RANGE"    | "VOLATILE" | "QUIET"
      score:    0-100 composite probability score
    Returns None if no strategy meets its minimum threshold.
    """
    if df is None or len(df) < 50:
        return None

    c = df["Close"]
    h = df["High"]
    l = df["Low"]
    v = df["Volume"] if "Volume" in df.columns else None

    if htf_trend == "FLAT":
        try:
            htf_df = df[["Open", "High", "Low", "Close"]].copy()
            htf_df.index = pd.DatetimeIndex(htf_df.index)
            if htf_df.index.tz is None:
                htf_df.index = htf_df.index.tz_localize("UTC")
            htf_1h = htf_df.resample("1h").agg({
                "Open": "first",
                "High": "max",
                "Low": "min",
                "Close": "last",
            }).dropna()
            if len(htf_1h) >= 21:
                htf_ema9 = ta.trend.EMAIndicator(htf_1h["Close"], 9).ema_indicator().dropna()
                htf_ema21 = ta.trend.EMAIndicator(htf_1h["Close"], 21).ema_indicator().dropna()
                if len(htf_ema9) > 0 and len(htf_ema21) > 0:
                    if float(htf_ema9.iloc[-1]) > float(htf_ema21.iloc[-1]):
                        htf_trend = "UP"
                    elif float(htf_ema9.iloc[-1]) < float(htf_ema21.iloc[-1]):
                        htf_trend = "DOWN"
            if htf_trend == "FLAT":
                # Early-session fallback: approximate 1h EMA trend from 15m bars so UK/forex
                # scans are not left trendless until enough hourly bars accumulate.
                q_ema36 = ta.trend.EMAIndicator(c, 36).ema_indicator().dropna()
                q_ema84 = ta.trend.EMAIndicator(c, 84).ema_indicator().dropna()
                if len(q_ema36) > 0 and len(q_ema84) > 0:
                    if float(q_ema36.iloc[-1]) > float(q_ema84.iloc[-1]):
                        htf_trend = "UP"
                    elif float(q_ema36.iloc[-1]) < float(q_ema84.iloc[-1]):
                        htf_trend = "DOWN"
        except Exception:
            pass

    # ── Core indicators ────────────────────────────────────────────────────────
    ema9  = ta.trend.EMAIndicator(c,  9).ema_indicator()
    ema21 = ta.trend.EMAIndicator(c, 21).ema_indicator()
    ema50 = ta.trend.EMAIndicator(c, 50).ema_indicator()
    rsi_s = ta.momentum.RSIIndicator(c, 9).rsi()
    macd  = ta.trend.MACD(c, 12, 26, 9)
    atr14 = ta.volatility.AverageTrueRange(h, l, c, 14).average_true_range()
    atr5  = ta.volatility.AverageTrueRange(h, l, c,  5).average_true_range()
    atr20 = ta.volatility.AverageTrueRange(h, l, c, 20).average_true_range()
    adx_i  = ta.trend.ADXIndicator(h, l, c, 14)
    bb     = ta.volatility.BollingerBands(c, window=20, window_dev=2)
    stoch_i = ta.momentum.StochasticOscillator(h, l, c, window=14, smooth_window=3)
    kc_mid_s = ta.trend.EMAIndicator(c, 20).ema_indicator()

    # ── Extract scalar values ──────────────────────────────────────────────────
    price   = float(c.iloc[-1])
    e9      = float(ema9.iloc[-1])
    e21     = float(ema21.iloc[-1])
    e50_s   = ema50.dropna()
    e50     = float(e50_s.iloc[-1]) if len(e50_s) > 0 else price
    rsi_v   = float(rsi_s.iloc[-1])
    mh_s    = macd.macd_diff()
    mh      = float(mh_s.iloc[-1])
    mh_prev = float(mh_s.iloc[-2]) if len(mh_s) >= 2 else mh
    atr_v   = float(atr14.iloc[-1])
    _adx_s  = adx_i.adx().dropna()
    adx_v   = float(_adx_s.iloc[-1]) if len(_adx_s) > 0 else 20.0
    adx_prev = float(_adx_s.iloc[-3]) if len(_adx_s) >= 3 else adx_v
    adx_rising = adx_v > adx_prev
    atr_ratio = (float(atr5.iloc[-1]) / float(atr20.iloc[-1])
                 if float(atr20.iloc[-1]) > 0 else 1.0)
    bb_upper = float(bb.bollinger_hband().iloc[-1])
    bb_lower = float(bb.bollinger_lband().iloc[-1])
    stoch_k  = float(stoch_i.stoch().dropna().iloc[-1])      if len(stoch_i.stoch().dropna())        > 0 else 50.0
    stoch_d  = float(stoch_i.stoch_signal().dropna().iloc[-1]) if len(stoch_i.stoch_signal().dropna()) > 0 else 50.0
    # Keltner Channel midline + width (for BB squeeze detection)
    kc_mid_v = float(kc_mid_s.iloc[-1])
    kc_upper_v = kc_mid_v + 1.5 * atr_v
    kc_lower_v = kc_mid_v - 1.5 * atr_v
    # BB width squeeze: current BB width vs 20-period low
    try:
        bb_width_s   = bb.bollinger_wband().fillna(0)
        bb_width_now = float(bb_width_s.iloc[-1])
        bb_width_lo  = float(bb_width_s.rolling(20).min().iloc[-1])
        bb_squeeze   = (bb_upper < kc_upper_v) and (bb_lower > kc_lower_v)
        squeeze_tight = bb_width_now <= bb_width_lo * 1.15 if bb_width_lo > 0 else False
    except Exception:
        bb_squeeze = False
        squeeze_tight = False

    session_df = _filter_current_session(df, market, now_dt=now_dt)
    _has_vol = (v is not None and float(v.sum()) > 0)
    if _has_vol:
        session_len = len(session_df)
        if session_len >= 5:
            sh = session_df["High"].values.astype(float)
            sl = session_df["Low"].values.astype(float)
            sc = session_df["Close"].values.astype(float)
            sv = session_df["Volume"].values.astype(float)
            vwap_arr, sd_arr = _cumulative_vwap(sh, sl, sc, sv)
            vwap = float(vwap_arr[-1])
            _vwap_std = float(sd_arr[-1]) if len(sd_arr) else 0.0
            vwap_z = (price - vwap) / _vwap_std if _vwap_std > 0 else 0.0
        else:
            tp       = (h + l + c) / 3
            vwap_s   = (tp * v).rolling(20).sum() / v.rolling(20).sum()
            _vwap_raw = vwap_s.iloc[-1]
            vwap     = float(_vwap_raw) if not pd.isna(_vwap_raw) else price
            vwap_dev = c - vwap_s
            _vwap_std_raw = vwap_dev.rolling(20).std().iloc[-1]
            _vwap_std = float(_vwap_std_raw) if (not pd.isna(_vwap_std_raw) and _vwap_std_raw > 0) else 0.0
            vwap_z   = (price - vwap) / _vwap_std if _vwap_std > 0 else 0.0
        vol_ma   = float(v.rolling(20).mean().iloc[-1])
        vsurge   = float(v.iloc[-1] / vol_ma) if vol_ma > 0 else 1.0
    else:
        # No real volume (e.g. IBKR MIDPOINT forex bars) — use SMA as proxy VWAP
        # and compute a proper z-score so MEAN_REV can still fire on forex.
        vwap_s   = c.rolling(20).mean()
        vwap     = float(vwap_s.iloc[-1])
        _sma_dev = c - vwap_s
        _sma_std_raw = _sma_dev.rolling(20).std().iloc[-1]
        _sma_std = float(_sma_std_raw) if (not pd.isna(_sma_std_raw) and _sma_std_raw > 0) else 0.0
        vwap_z   = (price - vwap) / _sma_std if _sma_std > 0 else 0.0
        vsurge   = 1.0

    # ── Regime ─────────────────────────────────────────────────────────────────
    if adx_v < 12:
        return None                   # QUIET — market going nowhere (lowered from 15)
    elif atr_ratio > 2.8 and adx_v < 22:
        regime = "VOLATILE"           # extreme expansion in ranging mkt — spreads widen (raised from 2.2 to reduce false blocks)
    elif adx_v >= 22 and adx_rising:
        regime = "TREND"              # rising ADX ≥ 22 = confirmed developing trend (lowered from 25)
    elif adx_v >= 22 and not adx_rising:
        regime = "RANGE"              # ADX high but falling = trend exhaustion, treat as range
    else:
        regime = "RANGE"

    if sym:
        _regime_history[sym] = regime

    if regime == "VOLATILE":
        return None                   # explosive ranging — spreads widen, entries unreliable

    # ── Shared boolean flags ───────────────────────────────────────────────────
    trend_up   = e9 > e21
    trend_down = e9 < e21
    macd_up    = mh > mh_prev
    macd_down  = mh < mh_prev
    above_e50  = price > e50
    below_e50  = price < e50
    above_vwap = price > vwap

    best_dir   = None
    best_score = 0
    best_strat = None
    rejection_notes = []

    _vsurge_mom = CFG.get("vsurge_momentum", 1.5)
    _vsurge_pb  = CFG.get("vsurge_pullback", 1.3)
    _vsurge_mr  = CFG.get("vsurge_mean_rev", 1.2)
    _vsurge_bbs = CFG.get("vsurge_bbs", 1.1)
    _fx_like = market in {"forex", "metal", "energy"}
    _mean_rev_min = (
        CFG.get("mean_rev_min_score_forex", 65) if market == "forex"
        else 72 if market in {"metal", "energy"}
        else CFG.get("mean_rev_min_score_uk", 65) if market == "stock_uk"
        else CFG.get("mean_rev_min_score_us", 65) if market == "stock_us"
        else 65
    )
    _bbs_min = (
        CFG.get("bbs_min_score_forex", 68) if market == "forex"
        else 72 if market in {"metal", "energy"}
        else CFG.get("bbs_min_score_uk", 68) if market == "stock_uk"
        else CFG.get("bbs_min_score_us", 68) if market == "stock_us"
        else 68
    )
    _mr_rsi_buy = 40 if _fx_like else 42 if market == "stock_uk" else 36
    _mr_rsi_sell = 60 if _fx_like else 60 if market == "stock_uk" else 64
    _mr_z_shallow = 0.9 if market == "forex" else 1.2 if market in {"metal", "energy"} else 1.0 if market == "stock_uk" else 1.5
    _mr_z_deep = 1.9 if market == "forex" else 1.8 if market in {"metal", "energy"} else 1.7 if market == "stock_uk" else 2.0
    _pb_min = 66 if _fx_like or market == "stock_uk" else 70
    _mom_rsi_buy_low = 54 if _fx_like else 52 if market == "stock_uk" else 50
    _mom_rsi_buy_high = 68 if _fx_like else 70 if market == "stock_uk" else 72
    _mom_rsi_sell_low = 32 if _fx_like else 30 if market == "stock_uk" else 28
    _mom_rsi_sell_high = 46 if _fx_like else 48 if market == "stock_uk" else 50
    _mom_adx_min = 22 if _fx_like else 20 if market == "stock_uk" else 22
    _bbs_adx_min = 20 if _fx_like else 18 if market == "stock_uk" else 18
    _session_name = _market_session_name(market, now_dt=now_dt)

    for direction in ("BUY", "SELL"):
        ib = (direction == "BUY")   # shorthand: is_buy
        # HTF gate: MOMENTUM and PULLBACK only trade with the 1H trend;
        # MEAN_REV and BB_SQUEEZE are exempt (they fade extremes regardless of trend)
        _htf_ok = not ((htf_trend == "UP" and not ib) or (htf_trend == "DOWN" and ib))

        # ── Strategy 1: MOMENTUM ───────────────────────────────────────────────
        # Ride an ESTABLISHED trend: RSI > 50 (momentum already running),
        # MACD positive, price on the right side of VWAP.
        # Only fires in TREND regime (ADX ≥ 25).
        if regime == "TREND" and _htf_ok:
            s = 0
            _mom_vwap_ok = abs(vwap_z) <= (1.0 if market == "forex" else 1.4 if market == "stock_uk" else 1.8)
            s += _sc(trend_up   if ib else trend_down,           20)  # EMA9/21 aligned
            s += _sc(above_vwap if ib else not above_vwap,       15)  # VWAP side
            s += _sc((mh > 0)   if ib else (mh < 0),             15)  # MACD on right side
            s += _sc(macd_up    if ib else macd_down,             10)  # MACD accelerating
            s += _sc((_mom_rsi_buy_low < rsi_v < _mom_rsi_buy_high) if ib
                      else (_mom_rsi_sell_low < rsi_v < _mom_rsi_sell_high), 15)
            s += _sc(above_e50  if ib else below_e50,            10)  # EMA50 side
            s += _sc(vsurge >= _vsurge_mom,                       10)  # volume confirms trend participation
            s += _sc(adx_v >= 30,                                  5)  # strong trend bonus
            # Stochastic confirmation: K > 50 trending, K > D for momentum
            s += _sc((stoch_k > 50 and stoch_k > stoch_d) if ib
                      else (stoch_k < 50 and stoch_k < stoch_d),  5)   # stochastic momentum confirmation
            # ORB bonus: Gao et al. first-half return predicts direction (Sharpe 1.08)
            s += _sc(orb_direction == direction,                  10)  # opening range alignment
            # RSI must be on the right side; allow MACD slightly negative (within 5% of ATR)
            _mh_tol = 0.05 * atr_v
            _mom_orb_required = (market == "forex" and orb_direction is not None)
            _mom_orb_ok = (orb_direction == direction) if _mom_orb_required else True
            _mom_session_ok = True
            if _fx_like:
                _mom_session_ok = _session_name in {"LONDON_FX", "NEW_YORK_FX", "ASIA_FX", "SYDNEY_FX"}
            elif market == "stock_uk":
                _mom_session_ok = _session_name == "LONDON_OPEN"
            _mom_vol_ok = vsurge >= (0.85 if _fx_like else 0.25 if market == "stock_uk" else _vsurge_mom)
            if ib and (rsi_v <= _mom_rsi_buy_low or rsi_v >= _mom_rsi_buy_high or mh < -_mh_tol or not _mom_vwap_ok or adx_v < _mom_adx_min or not _mom_orb_ok or not _mom_session_ok or not _mom_vol_ok or not adx_rising):
                rejection_notes.append(f"MOM BUY reject rsi={rsi_v:.1f} mh={mh:.5f} adx={adx_v:.1f} vsurge={vsurge:.2f} orb={orb_direction} session={_session_name} adx_rising={adx_rising}")
                s = 0
            if not ib and (rsi_v <= _mom_rsi_sell_low or rsi_v >= _mom_rsi_sell_high or mh > _mh_tol or not _mom_vwap_ok or adx_v < _mom_adx_min or not _mom_orb_ok or not _mom_session_ok or not _mom_vol_ok or not adx_rising):
                rejection_notes.append(f"MOM SELL reject rsi={rsi_v:.1f} mh={mh:.5f} adx={adx_v:.1f} vsurge={vsurge:.2f} orb={orb_direction} session={_session_name} adx_rising={adx_rising}")
                s = 0
            if s >= 65 and s > best_score:
                best_score = s; best_dir = direction; best_strat = "MOMENTUM"

        # ── Strategy 2: PULLBACK ───────────────────────────────────────────────
        # Buy the dip inside a trend: RSI < 50 (pulled back) + MACD turning up
        # from at or near zero. Requires trend alignment via EMA and EMA50.
        if regime == "TREND" and _htf_ok:
            s = 0
            _pb_rsi_ok = (26 < rsi_v < 48) if ib else (52 < rsi_v < 74)
            s += _sc(trend_up  if ib else trend_down,            20)  # trend direction
            s += _sc(above_e50 if ib else below_e50,             15)  # structure: above/below EMA50
            s += _sc(_pb_rsi_ok,                                 20)
            s += _sc(macd_up   if ib else macd_down,             15)  # MACD turning
            _pb_mh_tol = 0.1 * atr_v
            s += _sc((mh >= -_pb_mh_tol) if ib
                      else (mh <= _pb_mh_tol),                    10)  # MACD near/above zero
            s += _sc(vsurge >= _vsurge_pb,                        10)  # moderate vol: confirms genuine dip
            # Split stochastic — each half scores independently to avoid dual-condition block
            s += _sc((stoch_k < 45) if ib else (stoch_k > 55),    5)   # stochastic oversold
            s += _sc((stoch_k > stoch_d) if ib
                      else (stoch_k < stoch_d),                    5)   # stochastic turning
            if not _pb_rsi_ok:
                rejection_notes.append(f"PB {direction} reject rsi={rsi_v:.1f} trend_up={trend_up} trend_down={trend_down} e50={'above' if above_e50 else 'below'}")
                s = 0
            if s >= _pb_min and s > best_score:
                best_score = s; best_dir = direction; best_strat = "PULLBACK"

        # ── Strategy 3: MEAN_REV ───────────────────────────────────────────────
        # Fade extremes when ADX < 22 (market ranging). VWAP z-score replaces
        # BB-edge touch — evidence: 63% reversion from 2-SD VWAP, +0.89% mean edge.
        if regime == "RANGE":
            s = 0
            _mr_rsi_ok = (rsi_v <= _mr_rsi_buy) if ib else (rsi_v >= _mr_rsi_sell)
            _mr_z_ok = (vwap_z <= -_mr_z_shallow) if ib else (vwap_z >= _mr_z_shallow)
            _mr_z_deep_ok = (vwap_z <= -_mr_z_deep) if ib else (vwap_z >= _mr_z_deep)
            _mr_stoch_ok = (stoch_k < 18 and stoch_k <= stoch_d) if ib else (stoch_k > 82 and stoch_k >= stoch_d)
            _mr_reversal_ok = (macd_up and mh <= 0) if ib else (macd_down and mh >= 0)
            s += _sc(_mr_rsi_ok,                                  30)
            s += _sc(_mr_z_ok,                                    20)
            s += _sc(_mr_z_deep_ok,                               10)
            s += _sc(macd_up   if ib else macd_down,              20)  # reversal starting
            s += _sc(vsurge >= _vsurge_mr or not _has_vol,         10)  # slight vol confirmation; exempt no-vol (forex MIDPOINT)
            s += _sc(adx_v < 22,                                  10)  # truly range-bound
            s += _sc(_mr_stoch_ok,                                 8)
            s += _sc(_mr_reversal_ok,                             12)
            _mr_confirm_ok = _mr_reversal_ok or _mr_stoch_ok
            if not (_mr_rsi_ok and _mr_z_ok and _mr_confirm_ok):
                rejection_notes.append(f"MR {direction} reject rsi={rsi_v:.1f} z={vwap_z:.2f} stoch={stoch_k:.1f}/{stoch_d:.1f} macd_turn={macd_up if ib else macd_down}")
                s = 0
            if s >= _mean_rev_min and s > best_score:
                best_score = s; best_dir = direction; best_strat = "MEAN_REV"

        # ── Strategy 4: BB SQUEEZE ─────────────────────────────────────────────
        # TTM Squeeze: BB inside Keltner Channel (volatility contraction) +
        # momentum histogram firing. Sharpe 0.81, 68-72% accuracy with filters.
        # Fires in either TREND or RANGE regime — squeeze resolves into a move.
        if bb_squeeze:                        # squeeze_tight removed — BB-inside-KC alone is the gate
            s = 0
            _bbs_structure_ok = (above_e50 if ib else below_e50)
            _bbs_vwap_ok = abs(vwap_z) <= (1.1 if _fx_like else 1.3 if market == "stock_uk" else 1.8)
            _bbs_tight_required = market in {"forex", "metal", "energy", "stock_uk"}
            _bbs_regime_ok = regime == "TREND" if market in {"forex", "metal", "energy", "stock_uk"} else True
            _bbs_session_ok = True
            if _fx_like:
                _bbs_session_ok = _session_name in {"LONDON_FX", "NEW_YORK_FX", "ASIA_FX", "SYDNEY_FX"}
            elif market == "stock_uk":
                _bbs_session_ok = _session_name == "LONDON_OPEN"
            _bbs_orb_required = market in {"forex", "stock_uk"} and orb_direction is not None
            _bbs_orb_ok = (orb_direction == direction) if _bbs_orb_required else True
            _bbs_vol_ok = vsurge >= (0.85 if _fx_like else 0.25 if market == "stock_uk" else _vsurge_bbs)
            s += _sc(bb_squeeze,                                   25)  # BB inside KC (core squeeze)
            s += _sc(squeeze_tight,                                10)  # bonus: at multi-bar BB width low
            s += _sc(macd_up    if ib else macd_down,             20)  # momentum histogram direction
            s += _sc((mh > 0)   if ib else (mh < 0),             10)  # histogram on correct side
            s += _sc(trend_up   if ib else trend_down,            10)  # EMA direction aligns
            s += _sc(vsurge >= _vsurge_bbs or not _has_vol,        10)  # volume breakout confirms; exempt no-vol (forex MIDPOINT)
            s += _sc((stoch_k > stoch_d) if ib
                      else (stoch_k < stoch_d),                    5)   # stochastic K/D crossover
            s += _sc(orb_direction == direction,                   5)   # ORB alignment bonus
            s += _sc(_bbs_structure_ok,                            8)
            s += _sc(_bbs_vwap_ok,                                 7)
            s += _sc(adx_v >= _bbs_adx_min,                        6)
            s += _sc(_bbs_regime_ok,                               6)
            if (_bbs_tight_required and not squeeze_tight) or not _bbs_structure_ok or not _bbs_vwap_ok or adx_v < _bbs_adx_min or not _bbs_regime_ok or not _bbs_session_ok or not _bbs_orb_ok or not _bbs_vol_ok or not adx_rising:
                rejection_notes.append(f"BBS {direction} reject squeeze={bb_squeeze} tight={squeeze_tight} adx={adx_v:.1f} vsurge={vsurge:.2f} orb={orb_direction} session={_session_name}")
                s = 0
            if s >= _bbs_min and s > best_score:
                best_score = s; best_dir = direction; best_strat = "BB_SQUEEZE"

    # Time-of-day adjustment (Heston et al. periodicity — SAST hours)
    if best_dir is not None:
        best_score = max(0, min(100, best_score + _tod_score_adj(market, now_dt=now_dt)))

    if best_dir is None or best_score < 65:
        details = {
            "regime": regime,
            "htf": htf_trend,
            "adx": round(adx_v, 1),
            "rsi": round(rsi_v, 1),
            "vwap_z": round(vwap_z, 2),
            "vsurge": round(vsurge, 2),
            "session": _session_name,
            "notes": rejection_notes[:8],
        }
        return {
            "price": price, "atr": atr_v, "rsi": rsi_v, "macd_h": mh,
            "vwap": vwap, "vwap_z": round(vwap_z, 2), "vsurge": vsurge,
            "stoch_k": round(stoch_k, 1), "stoch_d": round(stoch_d, 1),
            "direction": None,
            "strategy": None, "regime": regime, "score": 0,
            "adx": round(adx_v, 1), "atr_ratio": round(atr_ratio, 2),
            "mode": "no_data",
            "gate_ok": False,
            "gate_reason": f"no setup (regime={regime} htf={htf_trend} adx={adx_v:.1f})",
            "conditions": details,
        }

    ib_win = (best_dir == "BUY")
    if best_strat == "MOMENTUM":
        _htf_ok_m = (htf_trend == "FLAT" or
                     (htf_trend == "UP" and ib_win) or
                     (htf_trend == "DOWN" and not ib_win))
        _conds = {
            "EMA 9/21":   trend_up   if ib_win else trend_down,
            "VWAP":       above_vwap if ib_win else not above_vwap,
            "MACD > 0":   (mh > 0)   if ib_win else (mh < 0),
            "MACD accel": macd_up    if ib_win else macd_down,
            "RSI 50-72":  (50 < rsi_v < 72) if ib_win else (28 < rsi_v < 50),
            "EMA50":      above_e50  if ib_win else below_e50,
            "Volume":     vsurge >= _vsurge_mom,
            "Stochastic": (stoch_k > 50 and stoch_k > stoch_d) if ib_win else (stoch_k < 50 and stoch_k < stoch_d),
            "ORB":        orb_direction == best_dir,
            "1H align":   _htf_ok_m,
        }
    elif best_strat == "PULLBACK":
        _htf_ok = (htf_trend == "FLAT" or
                   (htf_trend == "UP" and ib_win) or
                   (htf_trend == "DOWN" and not ib_win))
        _conds = {
            "EMA 9/21":   trend_up   if ib_win else trend_down,
            "EMA50":      above_e50  if ib_win else below_e50,
            "RSI dip":      (24 < rsi_v < 52) if ib_win else (48 < rsi_v < 76),
            "MACD turn":    macd_up    if ib_win else macd_down,
            "MACD near 0":  (mh >= -0.1 * atr_v) if ib_win else (mh <= 0.1 * atr_v),
            "1H align":     _htf_ok,
            "Stoch oversold": (stoch_k < 45) if ib_win else (stoch_k > 55),
            "Stoch turning":  (stoch_k > stoch_d) if ib_win else (stoch_k < stoch_d),
        }
    elif best_strat == "MEAN_REV":
        _conds = {
            "RSI extreme": (rsi_v < 38)      if ib_win else (rsi_v > 62),
            "VWAP z≥1.2":  (vwap_z <= -1.2)  if ib_win else (vwap_z >= 1.2),
            "VWAP z≥2.0":  (vwap_z <= -2.0)  if ib_win else (vwap_z >= 2.0),
            "MACD turn":   macd_up    if ib_win else macd_down,
            "Volume":      vsurge >= _vsurge_mr or not _has_vol,
            "ADX < 22":    adx_v < 22,
            "Stochastic":  (stoch_k < 20)    if ib_win else (stoch_k > 80),
        }
    elif best_strat == "BB_SQUEEZE":
        _conds = {
            "BB Squeeze":  bb_squeeze,
            "Squeeze low": squeeze_tight,
            "MACD dir":    macd_up    if ib_win else macd_down,
            "MACD side":   (mh > 0)   if ib_win else (mh < 0),
            "EMA 9/21":    trend_up   if ib_win else trend_down,
            "Volume":      vsurge >= _vsurge_bbs or not _has_vol,
            "Stochastic":  (stoch_k > stoch_d) if ib_win else (stoch_k < stoch_d),
            "ORB":         orb_direction == best_dir,
        }
    else:
        _conds = {}

    return {
        "price": price, "atr": atr_v, "rsi": rsi_v, "macd_h": mh,
        "vwap": vwap, "vwap_z": round(vwap_z, 2), "vsurge": vsurge,
        "stoch_k": round(stoch_k, 1), "stoch_d": round(stoch_d, 1),
        "direction": best_dir,
        "strategy": best_strat, "regime": regime, "score": best_score,
        "adx": round(adx_v, 1), "atr_ratio": round(atr_ratio, 2),
        "mode": best_strat.lower(),
        "conditions": {"strategy": best_strat, "score": best_score, **_conds},
    }


def execution_ok(price, atr, market="stock_us"):
    if price <= 0:
        return False, "bad price"
    atr_pct = atr / price
    min_atr = _CHOP_THRESHOLDS.get(market, CFG["min_atr_pct"])
    if atr_pct < min_atr:
        return False, f"low vol {atr_pct:.4f}"
    return True, "ok"


def _market_floor_pct(market: str, is_crypto: bool = False) -> float:
    if is_crypto:
        return CFG["crypto_stop_floor_pct"]
    if market == "forex":
        return CFG["forex_stop_floor_pct"]
    if market == "stock_uk":
        return CFG["stop_floor_pct_uk"]
    if market == "stock_us":
        return CFG["stop_floor_pct_us"]
    if market == "metal":
        return CFG.get("metal_stop_floor_pct", CFG["forex_stop_floor_pct"])
    if market == "energy":
        return CFG.get("energy_stop_floor_pct", CFG["stop_floor_pct"])
    if market in ("index_cfd", "index_cfd_eu"):
        return CFG["index_stop_floor_pct"]
    return CFG["stop_floor_pct"]


def _atr_stop_mult_for_market(market: str) -> float:
    return CFG["index_atr_stop_mult"] if market in ("index_cfd", "index_cfd_eu") else CFG["atr_stop_mult"]


def _rr_for_market(market: str, sym: str | None = None, sig_mode: str | None = None, is_crypto: bool = False) -> float:
    if is_crypto:
        return CFG.get("crypto_rr", CFG["rr_ratio"])
    if market == "forex":
        return CFG.get("rr_ratio_forex", CFG["rr_ratio"])
    if market == "metal":
        return CFG.get("rr_ratio_metals", CFG.get("rr_ratio_forex", CFG["rr_ratio"]))
    if market == "energy":
        return CFG.get("rr_ratio_energies", CFG["rr_ratio"])
    if market == "stock_uk":
        return CFG["rr_ratio_uk"]
    if market in ("index_cfd", "index_cfd_eu"):
        return CFG.get("index_rr", {}).get(sym or "", 2.0)
    if market == "stock_us":
        sym_rr = CFG.get("symbol_rr", {}).get(sym or "")
        if sym_rr is not None:
            return sym_rr
        if sig_mode == "momentum":
            return CFG.get("rr_ratio_momentum", CFG["rr_ratio_us"])
        return CFG["rr_ratio_us"]
    return CFG["rr_ratio"]


def _compute_stop_distance(price: float, atr: float, market: str = "stock_us", vwap_stop_dist: float | None = None, is_crypto: bool = False) -> float:
    floor_pct = _market_floor_pct(market, is_crypto=is_crypto)
    if vwap_stop_dist is not None and vwap_stop_dist > 0:
        return max(float(vwap_stop_dist), price * floor_pct)
    return max(atr * _atr_stop_mult_for_market(market), price * floor_pct)


def _gross_pnl_usd(entry: float, exit_px: float, qty: float, direction: str, market: str) -> float:
    gross = ((exit_px - entry) * qty if direction == "BUY" else (entry - exit_px) * qty)
    if market == "stock_uk":
        gross = gross / 100 * CFG.get("gbp_usd", 1.27)
    return gross


def in_trade_window(market):
    """
    Time-of-day gate, SAST local time.
    stock_uk: only trades inside trade_windows_uk_sast (LSE session).
    stock_us: only trades inside trade_windows_us_sast (US session).
    futures: uses the US window (CME hours roughly align with the US session).
    Forex: only trades inside the configured Sunday-night to Saturday-02:00 window.
    """
    now_sast = _now_trade_tz()
    if market == "crypto" and CFG["allow_forex_crypto_anytime"]:
        return True
    if not _global_trading_enabled(now_sast):
        return False

    weekdays = {0, 1, 2, 3, 4}
    if market in {"forex", "metal", "energy"}:
        return _forex_window_active(now_sast)
    if market in {"stock_uk", "index_cfd_eu"}:
        return _weekday_window_active(now_sast, CFG["trade_windows_uk_sast"], weekdays)
    if market in {"stock_us", "future", "index_cfd"}:
        return _weekday_window_active(now_sast, CFG["trade_windows_us_sast"], weekdays)
    return False


def is_us_lunch_block_now():
    """True during the US lunch no-entry block while the US session is open."""
    now_sast = _now_trade_tz()
    now_mins = _minutes_since_midnight(now_sast)
    lz = CFG.get("lunch_block_us_sast")
    if not lz:
        return False
    lz_start = lz[0] * 60 + lz[1]
    lz_end   = lz[2] * 60 + lz[3]
    in_lunch = _is_within_minute_window(now_mins, lz_start, lz_end - 1)
    in_us_session = _market_window_active(now_sast, CFG.get("trade_windows_us_sast", []))
    return in_lunch and in_us_session


def apply_costs(price, direction, atr):
    spread   = price * CFG["spread_pct"]
    slippage = atr   * CFG["slippage_atr_frac"]
    return (price + spread + slippage) if direction == "BUY" else (price - spread - slippage)


def position_size(price, atr, equity, regime, allow_fractional=False, is_crypto=False, is_stock=False, market="stock_us", prob_score=None, vwap_stop_dist=None):
    if price <= 0: return 0
    risk      = equity * (CFG["risk_pct"] / 100)
    stop = _compute_stop_distance(price, atr, market=market, vwap_stop_dist=vwap_stop_dist, is_crypto=is_crypto)
    if stop <= 0: return 0
    vix_mult  = news_filter.get_vix_multiplier()
    base_mult = 0.5 if regime == "VOLATILE" else 1.0
    # Scale position by probability score: high confidence → full size
    if prob_score is not None:
        if prob_score >= 85:
            prob_mult = 1.0
        elif prob_score >= 75:
            prob_mult = 0.75
        else:
            prob_mult = 0.5   # 60-74 range — minimum threshold, half size
    else:
        prob_mult = 1.0
    size_mult = base_mult * vix_mult * prob_mult
    # UK stocks are priced in pence (GBX). Convert stop and price to USD so
    # risk sizing uses the same currency as equity.
    if market == "stock_uk":
        gbp_usd = CFG.get("gbp_usd", 1.27)
        stop_usd = stop / 100 * gbp_usd   # pence → GBP → USD
        px_usd   = price / 100 * gbp_usd
        qty_risk = (risk / stop_usd) * size_mult
        qty_cap  = (equity * CFG["max_position_pct"]) / px_usd
    elif market == "forex":
        # Forex is margined — 1:1 notional cap always hits 1,300 units on a $2k account,
        # which is below min_forex_qty. Use the configured leverage factor for the cap.
        forex_lev = CFG.get("forex_max_leverage", 10)
        qty_risk  = (risk / stop) * size_mult
        qty_cap   = (equity * CFG["max_position_pct"] * forex_lev) / price
    else:
        qty_risk  = (risk / stop) * size_mult
        # For US stock CFDs, IBKR gives 5:1 leverage — the notional cap must
        # account for this or expensive stocks (KLAC $780, META $630) get capped
        # at 4-5 shares, producing only $1-2 P&L per trade.
        cfd_lev   = 5 if (market == "stock_us" and CFG.get("use_cfd_us")) else 1
        qty_cap   = (equity * CFG["max_position_pct"] * cfd_lev) / price
    qty       = min(qty_risk, qty_cap)
    if allow_fractional and is_crypto:
        qty = round(qty, 6) if qty > 0 else 0
        if qty < CFG["min_crypto_qty"]:
            return 0
        return qty
    if is_stock:
        qty = max(0, int(qty))
        if market == "stock_uk":
            min_shares = CFG.get("min_uk_shares", 50)
            if 0 < qty < min_shares:
                return 0  # IBKR UK commission ~£6/trade — below min is unviable
        return qty
    qty = max(0, int(qty))  # futures + forex → whole integer units
    if market == "forex":
        min_lot = CFG.get("min_forex_qty", 5000)
        if 0 < qty < min_lot:
            return 0  # too small — commission would exceed profit
    return qty


# ══════════════════════════════════════════════════════════════════════════════
#  CONTRACTS
# ══════════════════════════════════════════════════════════════════════════════

class Contracts:
    def __init__(self, ib: IB):
        self.ib     = ib
        self._cache = {}

    def _q(self, c):
        key = str(c)
        if key in self._cache:
            return self._cache[key]
        try:
            result = self.ib.qualifyContracts(c)
            # CRITICAL FIX: qualifyContracts() returns the IBKR-resolved
            # contract list. Previously this code checked c.conId on the
            # ORIGINAL object (ib_insync mutates it in place so it often
            # "looks" qualified) but never explicitly used result[0] as the
            # contract to actually trade. Always use the resolved object
            # IBKR gave back, not the original — this is the most likely
            # explanation for trades the bot believed succeeded but that
            # never actually appeared at IBKR.
            if not result:
                log.warning(f"Contract qualification failed (no result): {c}")
                return None
            resolved = result[0]
            if not resolved.conId:
                log.warning(f"Contract qualification failed (no conId): {c}")
                return None
            self._cache[key] = resolved
            return resolved
        except Exception as e:
            log.warning(f"Contract qualification error for {c}: {e}")
            return None

    def stock_us(self, s):
        if CFG.get("use_cfd_us"):
            return self._q(Contract(secType="CFD", symbol=s, currency="USD", exchange="SMART"))
        return self._q(Stock(s, "SMART", "USD"))

    def index_cfd(self, s):
        return self._q(Contract(secType="CFD", symbol=s, currency="USD", exchange="SMART"))

    def index_cfd_eu(self, s):
        _CCY = {"IBDE30": "EUR", "IBGB100": "GBP"}
        return self._q(Contract(secType="CFD", symbol=s, currency=_CCY.get(s, "EUR"), exchange="SMART"))

    def stock_uk(self, s):
        # UK/LSE stocks on IBKR generally need SMART routing with
        # primaryExchange set to LSE, rather than exchange="LSE" directly —
        # using exchange="LSE" caused "Error 200: No security definition
        # has been found" for valid symbols like BP.
        return self._q(Stock(s, "SMART", "GBP", primaryExchange="LSE"))
    def forex(self, p):       return self._q(Forex(pair=p))
    def crypto(self, s):      return self._q(Crypto(s, "PAXOS", "USD"))
    def future(self, s, e):   return self._q(ContFuture(s, e))


# ══════════════════════════════════════════════════════════════════════════════
#  ORDERS
# ══════════════════════════════════════════════════════════════════════════════

class Orders:
    def __init__(self, ib: IB, dry_run: bool, account_id: str = ""):
        self.ib        = ib
        self.dry_run   = dry_run
        self.account_id = account_id
        self._log      = self._load()
        self._open_bars = {}
        self._open_trades = {}   # sym -> {dir, entry, initial_stop, risk, trailing_active, stop_order, current_stop}
        self._pnl_sub  = None
        self._day            = _now_trade_tz().date()
        self._trades_today   = 0
        self._trades_today_uk = 0
        self._trades_today_us = 0
        self._last_trade_ts  = None
        self._day_locked     = False
        # per-symbol cooldown: sym -> bars remaining before re-entry allowed (in-session)
        self._symbol_cooldowns = {}
        # per-symbol cooldown expiry timestamps — persists across restarts
        self._cooldown_expiry = self._load_cooldowns()
        # direction block: sym -> "BUY"|"SELL" — blocked for rest of session after stop-loss
        self._stop_loss_direction_block: dict = {}
        # equity drawdown tracking (persists across restarts via equity_file)
        self._peak_equity     = self._load_peak_equity()
        self._drawdown_locked = False
        # rejection backoff: sym -> {error_code: consecutive_count}
        self._rejection_counts: dict = {}
        # session P&L baseline — subtracted from IBKR's raw daily P&L so each
        # session starts at $0 regardless of prior-day carry-over
        self._pnl_baseline = 0.0
        # rebuild open-trade display metadata + today's trade count from log
        self._restore_session_state()

    def _roll_day(self):
        today = _now_trade_tz().date()
        if today != self._day:
            self._day          = today
            self._trades_today = 0
            self._trades_today_uk = 0
            self._trades_today_us = 0
            self._last_trade_ts= None
            self._day_locked   = False
            log.info(f"New trading day {today} - counters reset.")

    def _load(self):
        try:
            with open(CFG["trade_file"]) as f:
                records = json.load(f)
        except Exception:
            return []
        changed = False
        for rec in records:
            trade_date = _trade_date_from_value(rec.get("exit_ts"), fallback=rec.get("ts"))
            if rec.get("trade_date") != trade_date:
                rec["trade_date"] = trade_date
                changed = True
        if changed:
            try:
                with open(CFG["trade_file"], "w") as f:
                    json.dump(records, f, indent=2, default=str)
            except Exception:
                pass
        return records

    def _save(self):
        with open(CFG["trade_file"], "w") as f:
            json.dump(self._log, f, indent=2, default=str)

    def _restore_session_state(self):
        """Rebuild in-memory state from the trade log after a restart so that
        open-position metadata (entry/SL/TP) and today's trade count survive
        a process restart without losing display accuracy."""
        today_str = _now_trade_tz().date().isoformat()
        futures_syms = {s for s, _ in CFG.get("futures", [])}
        trades_today = 0
        for rec in self._log:
            trade_date = rec.get("trade_date") or _trade_date_from_value(rec.get("exit_ts"), fallback=rec.get("ts"))
            rec["trade_date"] = trade_date
            if trade_date != today_str:
                continue
            if rec.get("outcome") in ("rejected",) or rec.get("status") == "rejected":
                continue
            trades_today += 1
            if rec.get("outcome") == "open" and rec.get("status") == "live":
                # Never restore open trades from scalper_trades.json. The file
                # is history only; live/open truth must come from IBKR.
                continue
        self._trades_today = trades_today

        # Cross-check JSON-restored positions against what IBKR actually holds.
        # Phantom positions (bot was restarted after manual cancel / position closed
        # outside the bot) must be discarded or they will be removed by tick_bars()
        # on the first cycle, leaving _open_trades empty and triggering re-entry.
        if self._open_trades:
            self.ib.sleep(2.5)
            actual_ibkr = set(self.positions())
            # Fetch recent executions once so we can look up fill prices for each
            # discarded position — avoids repeated API calls per symbol.
            try:
                recent_fills = self.ib.reqExecutions()
            except Exception:
                recent_fills = []
            # Build a map: sym -> most recent exit fill price
            fill_px_map: dict = {}
            for fill in recent_fills:
                s = getattr(fill.contract, "symbol", None)
                if not s:
                    continue
                side   = getattr(fill.execution, "side", "")
                avg_px = getattr(fill.execution, "avgPrice", None) or getattr(fill.execution, "price", None)
                if avg_px:
                    fill_px_map.setdefault(s, []).append((side, float(avg_px)))

            discarded = []
            for sym in list(self._open_trades.keys()):
                if sym not in actual_ibkr:
                    tr = self._open_trades.pop(sym, {})
                    self._open_bars.pop(sym, None)
                    discarded.append((sym, tr))
            if discarded:
                discarded_syms = {sym for sym, _ in discarded}
                for rec in self._log:
                    if rec.get("sym") not in discarded_syms or rec.get("outcome") != "open":
                        continue
                    sym = rec["sym"]
                    tr  = next((t for s, t in discarded if s == sym), {})
                    entry = float(rec.get("entry") or 0)
                    qty   = float(rec.get("qty")   or 0)
                    direction = rec.get("dir", "BUY")
                    # Try to get actual fill price from execution report
                    fills = fill_px_map.get(sym, [])
                    # A closing fill for a BUY position is "SLD", for SELL is "BOT"
                    close_side = "SLD" if direction == "BUY" else "BOT"
                    exit_fill = next((px for side, px in reversed(fills) if side == close_side), None)
                    if exit_fill:
                        market = tr.get("market", rec.get("market", "stock_us"))
                        pnl = _gross_pnl_usd(entry, exit_fill, qty, direction, market)
                        pnl -= CFG["commission_per_trade_usd"] * 2
                        rec["outcome"]    = "win" if pnl > 0 else "loss"
                        rec["exit_price"] = exit_fill
                        rec["pnl_usd"]    = round(pnl, 4)
                        log.info(f"Recovered closed trade {sym} from executions: "
                                 f"{rec['outcome']} ${pnl:+.2f} exit={exit_fill}")
                    else:
                        # No fill found — mark as closed_unknown so it still shows
                        # in history rather than vanishing silently.
                        sl_px = tr.get("initial_stop", float(rec.get("sl") or 0))
                        tp_px = tr.get("initial_tp",   float(rec.get("tp") or 0))
                        # Best guess: if between SL and TP, assume SL (conservative)
                        market = tr.get("market", rec.get("market", "stock_us"))
                        pnl = _gross_pnl_usd(entry, sl_px, qty, direction, market) if sl_px else 0
                        pnl -= CFG["commission_per_trade_usd"] * 2
                        rec["outcome"]    = "win" if pnl > 0 else "loss"
                        rec["exit_price"] = sl_px or 0
                        rec["pnl_usd"]    = round(pnl, 4)
                        rec["estimated"]  = True
                        log.warning(f"Phantom position {sym} closed while bot was down — "
                                    f"estimated P&L ${pnl:+.2f} (fill unavailable)")
                    rec["exit_ts"] = str(_now_trade_tz())
                    rec["trade_date"] = _trade_date_from_value(rec.get("exit_ts"), fallback=rec.get("ts"))
                    # Persist the close to the dashboard DB so it shows in Closed Trades
                    if _HAS_DB:
                        try:
                            _ss.close_trade(
                                trade_id=rec.get("id"),
                                exit_px=rec.get("exit_price", 0),
                                realized=rec.get("pnl_usd", 0),
                                outcome=rec.get("outcome", "loss"),
                                closed_at=rec["exit_ts"],
                                sym=sym,
                                market=tr.get("market", "stock_us"),
                                direction=direction,
                                qty=qty,
                                entry=entry,
                                sl=tr.get("initial_stop", 0),
                                tp=tr.get("initial_tp", 0),
                                atr=tr.get("atr", 0),
                                sig_mode=rec.get("sig_mode", "recovered"),
                                opened_at=rec.get("ts"),
                                trade_date=rec.get("trade_date"),
                            )
                            _ss.remove_position(sym)
                        except Exception as _dbe:
                            log.debug(f"[startup] close_trade for {sym}: {_dbe}")
                    # Block same direction for session — treat phantom losses same as live stop-outs
                    if rec.get("outcome") == "loss":
                        blocked_dir = rec.get("dir")
                        if blocked_dir:
                            self._stop_loss_direction_block[sym] = blocked_dir
                self._save()
                log.warning(f"Recovered {len(discarded)} position(s) closed during restart: "
                             f"{[s for s, _ in discarded]}")

        if trades_today:
            log.info(f"Session restored: {trades_today} trades today; "
                     "local JSON open trades ignored by design.")

        # Sync the entire JSON log → SQLite so the dashboard always has real history.
        # close_trade() is idempotent (INSERT OR IGNORE) so re-running is safe.
        if _HAS_DB:
            synced = 0
            for rec in self._log:
                if rec.get("outcome") not in ("win", "loss"):
                    continue
                trade_id = rec.get("id")
                if not trade_id:
                    continue
                try:
                    _ss.close_trade(
                        trade_id,
                        exit_px=float(rec.get("exit_price") or 0),
                        realized=float(rec.get("pnl_usd") or 0),
                        outcome=rec["outcome"],
                        closed_at=rec.get("exit_ts") or rec.get("ts"),
                        sym=rec.get("sym"),
                        market=rec.get("market", "stock_us"),
                        direction=rec.get("dir", "BUY"),
                        qty=float(rec.get("qty") or 0),
                        entry=float(rec.get("entry") or 0),
                        sl=float(rec.get("sl") or 0),
                        tp=float(rec.get("tp") or 0),
                        atr=float(rec.get("atr") or 0),
                        sig_mode=rec.get("sig_mode", "standard"),
                        opened_at=rec.get("ts"),
                        trade_date=rec.get("trade_date") or _trade_date_from_value(rec.get("exit_ts"), fallback=rec.get("ts")),
                    )
                    synced += 1
                except Exception as _dbe:
                    log.error(f"[DB] startup sync failed for trade {rec.get('id')}: {_dbe}")
            if synced:
                log.info(f"[dashboard] Synced {synced} closed trade(s) from log to DB.")

        # Do not adopt orphaned/current broker positions into bot state. The
        # dashboard may show broker positions separately, but they must not be
        # reinserted as bot-open trades from stale local files or snapshots.
        try:
            self.ib.reqPositions()   # force fresh snapshot — never use stale cache
            self.ib.sleep(3)
            broker_count = sum(1 for _p in self.ib.positions() if abs(float(_p.position)) >= 0.001)
            if broker_count:
                log.warning(f"[startup] Ignored {broker_count} broker position(s) for bot-open state; "
                            "not inserting stale/adopted open trades.")
        except Exception as _ae:
            log.warning(f"[startup] broker-position check error: {_ae}")

    def account(self):
        real = self.real_account_equity()
        if real is None:
            return CFG["capital_cap_usd"]
        return min(real, CFG["capital_cap_usd"])

    def real_account_equity(self):
        try:
            for x in self.ib.accountSummary():
                if x.tag == "NetLiquidation":
                    return float(x.value)
        except Exception:
            pass
        return None

    def _load_peak_equity(self):
        try:
            with open(CFG["equity_file"]) as f:
                data = json.load(f)
                peak = float(data.get("peak_equity", 0.0))
        except Exception:
            peak = 0.0
        cap = CFG.get("capital_cap_usd", 0.0)
        if cap > 0 and peak > cap:
            log.warning(f"Peak equity ${peak:,.0f} exceeds capital_cap_usd ${cap:,.0f} — resetting peak to cap and rewriting file")
            peak = cap
            # Rewrite immediately so the stale value doesn't re-appear on next restart
            try:
                with open(CFG["equity_file"], "w") as f:
                    json.dump({"peak_equity": peak, "updated": str(datetime.now())}, f, indent=2)
            except Exception:
                pass
        return peak

    def _save_peak_equity(self):
        try:
            with open(CFG["equity_file"], "w") as f:
                json.dump({"peak_equity": self._peak_equity,
                           "updated": str(datetime.now())}, f, indent=2)
        except Exception:
            pass

    def _load_cooldowns(self):
        """Load per-symbol cooldown expiry timestamps; discard any that have already expired."""
        try:
            with open(CFG["cooldown_file"]) as f:
                data = json.load(f)
            now = datetime.now()
            return {sym: datetime.fromisoformat(exp)
                    for sym, exp in data.items()
                    if datetime.fromisoformat(exp) > now}
        except Exception:
            return {}

    def _save_cooldowns(self):
        try:
            data = {sym: exp.isoformat() for sym, exp in self._cooldown_expiry.items()}
            with open(CFG["cooldown_file"], "w") as f:
                json.dump(data, f)
        except Exception:
            pass

    def check_drawdown(self, current_equity):
        """
        Tracks peak equity across the whole test (not reset daily).
        Halts new entries if equity falls equity_drawdown_stop_pct below peak.
        Returns (ok_to_trade, reason).
        """
        if not CFG["equity_drawdown_stop_enabled"]:
            return True, "ok"
        if current_equity > self._peak_equity:
            self._peak_equity = current_equity
            self._save_peak_equity()
        if self._peak_equity <= 0:
            return True, "ok"
        dd_pct = (self._peak_equity - current_equity) / self._peak_equity * 100
        if dd_pct >= CFG["equity_drawdown_stop_pct"]:
            if not self._drawdown_locked:
                self._drawdown_locked = True
                log.warning(f"EQUITY DRAWDOWN STOP: {dd_pct:.1f}% below peak "
                           f"(peak=${self._peak_equity:,.2f}, now=${current_equity:,.2f})")
            return False, f"drawdown {dd_pct:.1f}% (limit {CFG['equity_drawdown_stop_pct']}%)"
        self._drawdown_locked = False
        return True, "ok"

    def portfolio_heat_pct(self, equity):
        """Total open risk as % of equity across all active positions."""
        if not self._open_trades or equity <= 0:
            return 0.0
        total_risk = sum(
            self.trade_risk_usd(t)
            for t in self._open_trades.values()
            if self.is_bot_managed_trade(t)
        )
        return (total_risk / equity) * 100

    @staticmethod
    def trade_risk_usd(trade_meta: dict) -> float:
        risk_per_unit = abs(float(trade_meta.get("risk_per_unit") or 0))
        qty = float(trade_meta.get("remaining_qty", trade_meta.get("qty", 0)) or 0)
        risk = risk_per_unit * qty
        if trade_meta.get("market") == "stock_uk":
            risk = risk / 100 * CFG.get("gbp_usd", 1.27)
        return risk

    @staticmethod
    def is_bot_managed_trade(trade_meta: dict) -> bool:
        """True only for positions with bot bracket metadata attached."""
        if not trade_meta:
            return False
        stop = float(trade_meta.get("current_stop") or trade_meta.get("initial_stop") or 0)
        target = float(trade_meta.get("initial_tp") or 0)
        risk = float(trade_meta.get("risk_per_unit") or 0)
        has_live_stop_ref = bool(trade_meta.get("stop_order_id") or trade_meta.get("stop_contract"))
        return has_live_stop_ref and stop > 0 and target > 0 and risk > 0

    def managed_open_count(self):
        return sum(1 for t in self._open_trades.values() if self.is_bot_managed_trade(t))

    def managed_open_direction_count(self, direction):
        return sum(
            1 for t in self._open_trades.values()
            if t.get("dir") == direction and self.is_bot_managed_trade(t)
        )

    def daily_pnl(self):
        today = _now_trade_tz().date().isoformat()
        pnl = 0.0
        for rec in self._log:
            if rec.get("outcome") not in ("win", "loss"):
                continue
            trade_date = rec.get("trade_date") or _trade_date_from_value(rec.get("exit_ts"), fallback=rec.get("ts"))
            if trade_date != today:
                continue
            pnl += float(rec.get("pnl_usd") or 0.0)
        return round(pnl, 4)

    def reset_session_pnl(self):
        """Zero out P&L and unlock trading for a fresh session."""
        self._pnl_baseline = 0.0
        self._trades_today      = 0
        self._trades_today_uk   = 0
        self._trades_today_us   = 0
        self._last_trade_ts     = None
        self._day_locked        = False
        self._day               = _now_trade_tz().date()
        log.info("Session reset — date-based daily P&L recalculated, trading unlocked.")

    def positions(self):
        try:
            result = []
            for p in self.ib.positions():
                c = p.contract
                # Forex positions come back with secType='CASH', symbol='NZD', currency='USD'
                # Reconstruct the full pair so keys match _open_trades (e.g. 'NZDUSD')
                if getattr(c, 'secType', '') == 'CASH':
                    result.append(c.symbol + c.currency)
                else:
                    result.append(c.symbol)
            return result
        except Exception:
            return []

    def can_trade(self, market=None):
        self._roll_day()
        if self._day_locked:
            return False, "day locked"
        pnl_usd = self.daily_pnl()
        if pnl_usd >= CFG["daily_target_usd"]:
            self._day_locked = True
            log.info(f"DAILY TARGET HIT: +${pnl_usd:,.2f}")
            notify(f"🎯 DAILY TARGET HIT\nP&L: ${pnl_usd:+.2f}  Bot locked for today.")
            return False, f"target hit (+${pnl_usd:,.2f})"
        if pnl_usd <= -CFG["daily_loss_usd"]:
            self._day_locked = True
            log.warning(f"DAILY LOSS LIMIT: -${abs(pnl_usd):,.2f}")
            notify(f"🛑 DAILY LOSS LIMIT HIT\nP&L: ${pnl_usd:+.2f}  Bot locked for today.")
            return False, f"loss limit (-${abs(pnl_usd):,.2f})"
        if self._trades_today >= CFG["max_trades_per_day"]:
            return False, f"max {CFG['max_trades_per_day']} trades/day"
        # per-market caps — only apply to stock_uk / stock_us; forex, crypto,
        # and futures are unaffected by these and only count against the
        # global max_trades_per_day cap above.
        if market == "stock_uk" and self._trades_today_uk >= CFG["max_trades_per_day_uk"]:
            return False, f"max {CFG['max_trades_per_day_uk']} UK trades/day"
        if market == "stock_us" and self._trades_today_us >= CFG["max_trades_per_day_us"]:
            return False, f"max {CFG['max_trades_per_day_us']} US trades/day"
        if self._last_trade_ts:
            mins = (datetime.now() - self._last_trade_ts).total_seconds() / 60
            if mins < CFG["trade_cooldown_min"]:
                return False, f"cooldown {CFG['trade_cooldown_min'] - mins:.0f}m"
        ok_dd, reason_dd = self.check_drawdown(self.account())
        if not ok_dd:
            return False, reason_dd
        return True, "ok"

    def can_trade_symbol(self, sym, direction=None):
        """
        Per-symbol cooldown, separate from the global cooldown above.
        Global cooldown blocks ANY new trade for N minutes after the last one.
        This blocks re-entering the SAME symbol for N scan cycles, even if
        the global cooldown has already expired and other symbols are free
        to trade — prevents the bot churning the same name repeatedly.
        Cooldown expiry timestamps persist across restarts via scalper_cooldowns.json.
        After a stop-loss both the cooldown (30 min) and direction block expire together.
        """
        # Check persistent timestamp-based cooldown first so we can clear direction block
        # at the same time the cooldown expires (both are set together on stop-loss).
        if sym in self._cooldown_expiry:
            expiry = self._cooldown_expiry[sym]
            if datetime.now() < expiry:
                remaining_sec = int((expiry - datetime.now()).total_seconds())
                # Direction block still active while cooldown hasn't expired
                if direction and self._stop_loss_direction_block.get(sym) == direction:
                    return False, f"{sym} {direction} blocked {remaining_sec}s (stop-loss cooldown)"
                return False, f"{sym} cooldown {remaining_sec}s remaining"
            else:
                # Cooldown expired — clear both cooldown and direction block together
                del self._cooldown_expiry[sym]
                self._stop_loss_direction_block.pop(sym, None)
                self._save_cooldowns()
        elif direction and self._stop_loss_direction_block.get(sym) == direction:
            # Direction block without an active timestamp cooldown — clear it (stale)
            self._stop_loss_direction_block.pop(sym, None)
        # In-session bar-based cooldown (in case only bar count was set, no expiry)
        remaining = self._symbol_cooldowns.get(sym, 0)
        if remaining > 0:
            return False, f"{sym} cooldown {remaining} bars"
        return True, "ok"

    def tick_symbol_cooldowns(self):
        """Call once per scan cycle to count down all active symbol cooldowns."""
        for sym in list(self._symbol_cooldowns.keys()):
            self._symbol_cooldowns[sym] -= 1
            if self._symbol_cooldowns[sym] <= 0:
                del self._symbol_cooldowns[sym]

    def status_line(self):
        pnl   = self.daily_pnl()
        state = "LOCKED" if self._day_locked else "ACTIVE"
        sc    = Fore.RED if self._day_locked else Fore.GREEN
        pc    = Fore.GREEN if pnl >= 0 else Fore.RED
        wins  = sum(1 for t in self._log
                    if t.get("outcome")=="win"
                    and str(t.get("exit_ts",""))[:10]==str(datetime.now().date()))
        losses= sum(1 for t in self._log
                    if t.get("outcome")=="loss"
                    and str(t.get("exit_ts",""))[:10]==str(datetime.now().date()))
        return (f"{Fore.CYAN}  {'─'*78}{Style.RESET_ALL}\n"
                f"  {sc}[{state}]{Style.RESET_ALL}  "
                f"Trades: {Style.BRIGHT}{self._trades_today}{Style.RESET_ALL}/{CFG['max_trades_per_day']}  ·  "
                f"{Fore.GREEN}{wins}W{Style.RESET_ALL}/{Fore.RED}{losses}L{Style.RESET_ALL}  ·  "
                f"P&L: {pc}${pnl:+,.2f}{Style.RESET_ALL}  "
                f"(target +${CFG['daily_target_usd']:,.2f}  /  stop -${CFG['daily_loss_usd']:,.2f})")

    def _bump_trade_counters(self, market):
        self._trades_today += 1
        if market == "stock_uk":
            self._trades_today_uk += 1
        elif market == "stock_us":
            self._trades_today_us += 1
        self._last_trade_ts = datetime.now()

    @staticmethod
    def _round_px(price, market):
        if market == "forex":
            return round(price, 5)
        if market == "future":
            return round(price * 4) / 4   # nearest 0.25 tick (MES, MNQ)
        if market == "crypto":
            return round(price, 0)         # BTC/ETH/SOL on PAXOS: whole dollar ticks
        if market == "stock_uk":
            # LSE minimum price variation: 0.5p under 500p, 1p at 500p+
            tick = 1.0 if price >= 500 else 0.5
            return round(round(price / tick) * tick, 2)
        return round(price, 2)             # stocks: $0.01 tick

    def bracket(self, contract, direction, price, atr, qty, sym, is_crypto=False, market="stock_us", sig_mode="standard", strategy=None, score=0, vwap_target=None, vwap_stop_dist=None, signal_meta=None):
        if qty <= 0: return False
        # Hard guard — check positions AND open orders before placing.
        # Open orders catch pending bracket entries that haven't filled yet.
        self.ib.sleep(0.5)
        live_positions = self.positions()
        open_order_syms = set()
        try:
            for o in self.ib.openOrders():
                c = getattr(o, 'contract', None)
                if c:
                    s = (c.symbol + c.currency) if getattr(c, 'secType', '') == 'CASH' else c.symbol
                    open_order_syms.add(s)
        except Exception:
            pass
        if sym in live_positions or sym in self._open_trades or sym in open_order_syms:
            log.warning(f"Skipping {sym} — already have open position or pending order")
            return False
        floor_pct = _market_floor_pct(market, is_crypto=is_crypto)
        rr = _rr_for_market(market, sym=sym, sig_mode=sig_mode, is_crypto=is_crypto)
        ep = self._round_px(price * 1.001 if direction == "BUY" else price * 0.999, market)

        if vwap_target is not None and vwap_stop_dist is not None:
            # VWAP reversion: TP = VWAP, SL = 3.0 SD band
            tp     = self._round_px(vwap_target, market)
            stop_d = _compute_stop_distance(price, atr, market=market, vwap_stop_dist=vwap_stop_dist, is_crypto=is_crypto)
            sl     = self._round_px(ep - stop_d if direction == "BUY" else ep + stop_d, market)
            # Hard cap: max loss on this trade = 1.5% of capital
            max_loss   = CFG.get("capital_cap_usd", 4000) * 0.015
            max_stop_d = max_loss / max(qty, 1)
            if stop_d > max_stop_d:
                stop_d = max_stop_d
                sl = self._round_px(ep - stop_d if direction == "BUY" else ep + stop_d, market)
        else:
            stop_d = _compute_stop_distance(price, atr, market=market, is_crypto=is_crypto)
            tp_d   = stop_d * rr
            sl = self._round_px(ep - stop_d if direction == "BUY" else ep + stop_d, market)
            tp = self._round_px(ep + tp_d   if direction == "BUY" else ep - tp_d,   market)
        opened_ts = _now_trade_tz()
        trade_id = f"{sym}_{opened_ts.strftime('%Y%m%d%H%M%S%f')}"
        strat_label = strategy or sig_mode
        meta = signal_meta or {}
        risk_per_unit = abs(ep - sl)
        est_slippage = atr * CFG.get("slippage_atr_frac", 0.0)
        rec = {"id": trade_id, "ts": str(opened_ts), "trade_date": opened_ts.date().isoformat(), "sym": sym, "dir": direction,
               "market": market,
               "qty": qty, "entry": ep, "sl": sl, "tp": tp, "atr": round(atr,4),
               "dry": self.dry_run, "status": "pending",
               "commission_usd": CFG["commission_per_trade_usd"],
               "outcome": "open", "exit_price": None, "exit_ts": None,
               "pnl_usd": None, "partial_taken": False,
               "strategy": strat_label, "score": score,
               "score_band": meta.get("score_band", _score_band(score)),
               "score_breakdown": meta.get("score_breakdown", {}),
               "bias_1h_score": meta.get("bias_1h_score"),
               "setup_15m_score": meta.get("setup_15m_score"),
               "trigger_5m_score": meta.get("trigger_5m_score"),
               "execution_1m_score": meta.get("execution_1m_score"),
               "smc_score": meta.get("smc_score"),
               "risk_reward_score": meta.get("risk_reward_score"),
               "session_score": meta.get("session_score"),
               "timeframe_setup": meta.get("timeframe_setup"),
               "execution_1m_result": meta.get("execution_1m_result"),
               "smc_confirmation": meta.get("smc_confirmation"),
               "spread_signal": meta.get("spread_pct", 0.0),
               "spread_execution": meta.get("spread_pct", 0.0),
               "slippage": est_slippage,
               "result_r": None,
               "result_ccy": None,
               "exit_reason": None,
               "comparison_bucket": meta.get("comparison_bucket"),
               "forward_phase": meta.get("forward_phase")}

        # track for trailing stop / partial-TP / outcome management
        self._open_trades[sym] = {
            "id": trade_id,
            "dir": direction, "entry": ep, "initial_stop": sl, "initial_tp": tp,
            "risk_per_unit": risk_per_unit, "atr": atr,
            "trailing_active": False, "current_stop": sl,
            "stop_order_id": None, "qty": qty, "remaining_qty": qty,
            "is_crypto": is_crypto, "market": market, "partial_taken": False,
            "pyramid_done": False,
        }

        # start this symbol's per-symbol cooldown so the bot doesn't
        # immediately re-enter the same name next cycle
        cooldown_bars = CFG["symbol_cooldown_bars"]
        self._symbol_cooldowns[sym] = cooldown_bars
        cooldown_secs = cooldown_bars * CFG.get("scan_sec", 15)
        self._cooldown_expiry[sym] = datetime.now() + timedelta(seconds=cooldown_secs)
        self._save_cooldowns()

        if self.dry_run:
            rec["status"] = "dry_run"
            log.info(f"[DRY] {direction} {qty}x {sym} @ {ep}  SL={sl}  TP={tp}  "
                     f"[{strat_label} score={score}]  (#{self._trades_today+1})")
            self._log.append(rec); self._save()
            self._bump_trade_counters(market)
            return True
        try:
            orders = self.ib.bracketOrder(direction, qty, ep, tp, sl)
            # CONFIRMED ROOT CAUSE (verified against ib_insync's own source/
            # docs): the base Order class defaults tif to an EMPTY STRING,
            # not a valid value. bracketOrder()'s constituent orders do not
            # reliably override this for every order in the group. IBKR's
            # API rejects an empty/missing time-in-force with
            # "Error 10052: Invalid time in force" — this is exactly what
            # hit COIN and ETH repeatedly. My earlier fix removed an
            # explicit GTC override assuming the library handled this; that
            # assumption was wrong, as the same error recurred afterward.
            # Setting tif="DAY" explicitly is the correct, standard fix —
            # DAY is universally valid for regular-session bracket orders
            # and is what most retail trading software uses by default.
            for o in orders:
                if not o.tif:
                    o.tif = "DAY"

            # Be explicit rather than trusting assumed library defaults —
            # we've been burned by wrong assumptions about what ib_insync
            # sets automatically more than once. bracketOrder() normally
            # sets transmit=True only on the LAST order in the group (so
            # the whole bracket transmits together); confirm that's
            # actually the case rather than assuming it silently.
            for i, o in enumerate(orders):
                is_last = (i == len(orders) - 1)
                if o.transmit != is_last:
                    log.debug(f"transmit flag on order {i} was {o.transmit}, "
                              f"expected {is_last} for bracket group — correcting")
                    o.transmit = is_last
                # explicitly stamp the verified account onto every order in
                # the bracket — previously this relied on IBKR's implicit
                # default-account routing, which is exactly the kind of
                # silent assumption that's bitten us before today.
                if self.account_id:
                    o.account = self.account_id

            placed_trades = []
            for o in orders:
                t = self.ib.placeOrder(contract, o)
                placed_trades.append(t)
                log.info(f"Placed order: orderId={o.orderId} type={o.orderType} "
                         f"action={o.action} qty={o.totalQuantity} "
                         f"account={o.account or '(default)'} transmit={o.transmit}")

            # give IBKR more time — bracket orders with a limit parent can
            # take a few seconds to move from PendingSubmit to Submitted on
            # a paper account; 1.5s was too aggressive and a real factor in
            # us seeing 'Cancelled' children before the parent even settled.
            for _ in range(6):
                self.ib.sleep(0.5)
                statuses = [t.orderStatus.status for t in placed_trades]
                if all(s not in ("PendingSubmit",) for s in statuses):
                    break

            statuses = [t.orderStatus.status for t in placed_trades]
            rejected = [s for s in statuses if s in ("Cancelled", "Inactive", "ApiCancelled")]

            if rejected or not placed_trades:
                rec["status"] = "rejected"
                rec["outcome"] = "rejected"
                rec["exit_reason"] = f"order_status:{'/'.join(statuses)}"
                self._log.append(rec); self._save()
                if _HAS_DB:
                    try:
                        _ss.insert_trade(trade_id, sym, market, direction, qty, ep, sl, tp,
                                         round(atr, 4), sig_mode, opened_ts,
                                         status="rejected", strategy=strat_label, score=score,
                                         score_breakdown=rec.get("score_breakdown"),
                                         bias_1h_score=rec.get("bias_1h_score"),
                                         setup_15m_score=rec.get("setup_15m_score"),
                                         trigger_5m_score=rec.get("trigger_5m_score"),
                                         execution_1m_score=rec.get("execution_1m_score"),
                                         smc_score=rec.get("smc_score"),
                                         risk_reward_score=rec.get("risk_reward_score"),
                                         session_score=rec.get("session_score"),
                                         timeframe_setup=rec.get("timeframe_setup"),
                                         execution_1m_result=rec.get("execution_1m_result"),
                                         smc_confirmation=rec.get("smc_confirmation"),
                                         spread_signal=rec.get("spread_signal"),
                                         spread_execution=rec.get("spread_execution"),
                                         slippage=rec.get("slippage"),
                                         commission=rec.get("commission_usd"),
                                         result_r=0.0, result_ccy=0.0,
                                         exit_reason=rec.get("exit_reason"),
                                         comparison_bucket=rec.get("comparison_bucket"),
                                         forward_phase=rec.get("forward_phase"),
                                         rejected_reason=rec.get("exit_reason"))
                    except Exception as _dbe:
                        log.error(f"[DB] insert rejected trade failed for {sym}: {_dbe}")
                log.error(f"REJECTED {direction} {qty}x {sym} — order statuses: {statuses}")
                self._open_trades.pop(sym, None)
                # Backoff: 20-bar cooldown on rejection so we don't hammer the same
                # symbol every scan cycle (e.g. IBKR "closing-only" status, Error 201)
                self._symbol_cooldowns[sym] = max(self._symbol_cooldowns.get(sym, 0), 20)
                return False

            # the stop order is typically the 3rd order in a standard bracket
            # (parent, takeProfit, stopLoss) — store its order id for later modification
            if len(placed_trades) >= 3:
                self._open_trades[sym]["stop_order_id"] = placed_trades[2].order.orderId
                self._open_trades[sym]["stop_contract"] = contract
            rec["status"] = "live"
            self._log.append(rec); self._save()
            if _HAS_DB:
                try:
                    _ss.insert_trade(trade_id, sym, market, direction, qty, ep, sl, tp,
                                     round(atr, 4), sig_mode, opened_ts,
                                     status="live", strategy=strat_label, score=score,
                                     score_breakdown=rec.get("score_breakdown"),
                                     bias_1h_score=rec.get("bias_1h_score"),
                                     setup_15m_score=rec.get("setup_15m_score"),
                                     trigger_5m_score=rec.get("trigger_5m_score"),
                                     execution_1m_score=rec.get("execution_1m_score"),
                                     smc_score=rec.get("smc_score"),
                                     risk_reward_score=rec.get("risk_reward_score"),
                                     session_score=rec.get("session_score"),
                                     timeframe_setup=rec.get("timeframe_setup"),
                                     execution_1m_result=rec.get("execution_1m_result"),
                                     smc_confirmation=rec.get("smc_confirmation"),
                                     spread_signal=rec.get("spread_signal"),
                                     spread_execution=rec.get("spread_execution"),
                                     slippage=rec.get("slippage"),
                                     commission=rec.get("commission_usd"),
                                     result_r=rec.get("result_r"),
                                     result_ccy=rec.get("result_ccy"),
                                     exit_reason=rec.get("exit_reason"),
                                     comparison_bucket=rec.get("comparison_bucket"),
                                     forward_phase=rec.get("forward_phase"))
                except Exception as _dbe:
                    log.error(f"[DB] insert_trade failed for {sym}: {_dbe}")
            log.info(f"FILLED {direction} {qty}x {sym} @ {ep}  SL={sl}  TP={tp}  "
                     f"[{strat_label} score={score}]  (#{self._trades_today+1})  [statuses: {statuses}]")
            notify(
                f"🟢 TRADE OPEN\n"
                f"{direction} {sym} x{qty}\n"
                f"Entry: {ep}  SL: {sl}  TP: {tp}\n"
                f"Strategy: {strat_label}  Score: {score}\n"
                f"Market: {market.upper()}  Risk: ${round(abs(ep-sl)*qty,2)}"
            )
            self._open_bars[sym] = 0
            self._bump_trade_counters(market)
            return True
        except Exception as e:
            log.error(f"Order fail {sym}: {e}")
            self._open_trades.pop(sym, None)
            self._symbol_cooldowns.pop(sym, None)
            return False

    def manage_partial_take_profit(self, current_prices):
        """
        Once a trade reaches partial_tp_r profit, close partial_tp_fraction
        of the position and let the trailing stop manage the remainder.
        Dry-run: logs what would have happened. Live: places a reduce-only
        market order for the partial quantity.
        """
        if not CFG["partial_tp_enabled"]:
            return
        for sym, tr in list(self._open_trades.items()):
            if tr.get("partial_taken"):
                continue
            price = current_prices.get(sym)
            if price is None:
                continue
            risk = tr["risk_per_unit"]
            if risk <= 0:
                continue
            profit_r = ((price - tr["entry"]) / risk if tr["dir"] == "BUY"
                       else (tr["entry"] - price) / risk)
            if profit_r < CFG["partial_tp_r"]:
                continue

            partial_qty = tr["qty"] * CFG["partial_tp_fraction"]
            if not tr.get("is_crypto"):
                partial_qty = max(0, int(partial_qty))
                if partial_qty <= 0:
                    continue  # too small to split a whole-share position
            else:
                partial_qty = round(partial_qty, 6)

            tr["partial_taken"]  = True
            tr["remaining_qty"]  = tr["qty"] - partial_qty

            if self.dry_run:
                log.info(f"[DRY] Partial TP {sym}: closing {partial_qty} of {tr['qty']} "
                         f"at {profit_r:.2f}R (price {price})")
            else:
                try:
                    from ib_insync import MarketOrder
                    exit_dir = "SELL" if tr["dir"] == "BUY" else "BUY"
                    contract = tr.get("stop_contract")
                    if contract:
                        order = MarketOrder(exit_dir, partial_qty)
                        self.ib.placeOrder(contract, order)
                        log.info(f"Partial TP {sym}: closed {partial_qty} of {tr['qty']} "
                                 f"at {profit_r:.2f}R (price {price})")
                except Exception as e:
                    log.error(f"Partial TP order failed {sym}: {e}")

    def manage_pyramid(self, current_prices):
        """
        Once a trade reaches pyramid_trigger_r profit, add pyramid_qty_fraction
        of the original qty to the position and move the stop to breakeven.
        Only one pyramid add per trade.
        """
        if not CFG.get("pyramid_enabled"):
            return
        for sym, tr in list(self._open_trades.items()):
            if tr.get("pyramid_done"):
                continue
            price = current_prices.get(sym)
            if price is None:
                continue
            risk = tr["risk_per_unit"]
            if risk <= 0:
                continue
            profit_r = ((price - tr["entry"]) / risk if tr["dir"] == "BUY"
                        else (tr["entry"] - price) / risk)
            if profit_r < CFG["pyramid_trigger_r"]:
                continue

            add_qty = tr["qty"] * CFG["pyramid_qty_fraction"]
            if not tr.get("is_crypto"):
                add_qty = max(0, int(add_qty))
                if add_qty <= 0:
                    tr["pyramid_done"] = True
                    continue
            else:
                add_qty = round(add_qty, 6)

            tr["pyramid_done"] = True
            be = self._round_px(tr["entry"], tr.get("market", "stock_us"))

            if self.dry_run:
                log.info(f"[DRY] Pyramid {sym}: +{add_qty} at {profit_r:.2f}R "
                         f"(price {price}), stop -> BE {be}")
            else:
                try:
                    from ib_insync import MarketOrder, StopOrder
                    contract = tr.get("stop_contract")
                    if not contract:
                        continue
                    add_order = MarketOrder(tr["dir"], add_qty)
                    add_order.tif = "DAY"
                    if self.account_id:
                        add_order.account = self.account_id
                    self.ib.placeOrder(contract, add_order)

                    total_qty = tr.get("remaining_qty", tr["qty"]) + add_qty
                    oid = tr.get("stop_order_id")
                    if oid:
                        new_stop = StopOrder(
                            "SELL" if tr["dir"] == "BUY" else "BUY",
                            total_qty, be)
                        new_stop.orderId = oid
                        new_stop.tif = "DAY"
                        if self.account_id:
                            new_stop.account = self.account_id
                        self.ib.placeOrder(contract, new_stop)
                        tr["current_stop"] = be

                    tr["remaining_qty"] = total_qty
                    log.info(f"Pyramid {sym}: added {add_qty} at {profit_r:.2f}R "
                             f"(price {price}), stop -> BE {be}")
                    notify(
                        f"📈 PYRAMID ADD\n"
                        f"{tr['dir']} {sym} +{add_qty}\n"
                        f"At {profit_r:.2f}R profit  price {price}\n"
                        f"Stop moved to BE: {be}"
                    )
                except Exception as e:
                    log.error(f"Pyramid failed {sym}: {e}")

    def check_open_trade_outcomes(self, positions):
        """
        Compares the bot's open-trade tracker against IBKR's actual open
        positions. When a symbol we were tracking is no longer in IBKR's
        position list, the trade has closed (hit stop, target, or trailing
        stop) — record the outcome using the actual IBKR fill price.
        """
        closed_syms = [s for s in self._open_trades if s not in positions]
        if not closed_syms:
            return

        # Fetch recent executions once so we can look up actual exit prices.
        fill_map: dict = {}  # sym -> most recent closing fill avg price
        try:
            fills = self.ib.reqExecutions()
            for fill in fills:
                s = getattr(fill.contract, "symbol", None)
                if not s:
                    continue
                # For forex, rebuild the pair key
                if getattr(fill.contract, "secType", "") == "CASH":
                    s = s + getattr(fill.contract, "currency", "")
                side = getattr(fill.execution, "side", "")
                avg  = getattr(fill.execution, "avgPrice", None) or getattr(fill.execution, "price", None)
                if avg:
                    fill_map.setdefault(s, []).append((side, float(avg)))
        except Exception as _fe:
            log.debug(f"check_open_trade_outcomes: reqExecutions failed: {_fe}")

        for sym in closed_syms:
            tr = self._open_trades.get(sym)
            if not tr:
                continue
            # find the matching open log entry by id
            for rec in reversed(self._log):
                if rec.get("id") == tr.get("id") and rec.get("outcome") == "open":
                    entry = tr["entry"]
                    qty   = tr["qty"]
                    # Prefer actual IBKR closing fill price; fall back to stop estimate
                    exit_px = None
                    if sym in fill_map:
                        # The closing fill has the opposite side to the entry direction
                        closing_side = "SLD" if tr["dir"] == "BUY" else "BOT"
                        closes = [px for side, px in fill_map[sym] if closing_side in side.upper()]
                        if closes:
                            exit_px = closes[-1]
                    if exit_px is None:
                        exit_px = tr.get("current_stop") or tr.get("initial_stop") or entry
                    market = tr.get("market", "stock_us")
                    pnl = _gross_pnl_usd(entry, exit_px, qty, tr["dir"], market)
                    round_trip_commission = CFG["commission_per_trade_usd"] * 2
                    pnl -= round_trip_commission  # round trip
                    risk_per_unit = abs(float(tr.get("risk_per_unit") or 0))
                    gross_pnl_r_units = ((exit_px - entry) * qty if tr["dir"] == "BUY"
                                         else (entry - exit_px) * qty)
                    result_r = (gross_pnl_r_units / (risk_per_unit * qty)) if (risk_per_unit > 0 and qty > 0) else 0.0
                    if tr["dir"] == "BUY":
                        if exit_px >= float(tr.get("initial_tp") or exit_px):
                            exit_reason = "target_hit"
                        elif exit_px <= float(tr.get("current_stop") or tr.get("initial_stop") or exit_px):
                            exit_reason = "stop_or_trail_hit"
                        else:
                            exit_reason = "manual_or_unknown"
                    else:
                        if exit_px <= float(tr.get("initial_tp") or exit_px):
                            exit_reason = "target_hit"
                        elif exit_px >= float(tr.get("current_stop") or tr.get("initial_stop") or exit_px):
                            exit_reason = "stop_or_trail_hit"
                        else:
                            exit_reason = "manual_or_unknown"
                    rec["outcome"]    = "win" if pnl > 0 else "loss"
                    rec["exit_price"] = exit_px
                    rec["exit_ts"]    = str(_now_trade_tz())
                    rec["trade_date"] = _trade_date_from_value(rec["exit_ts"], fallback=rec.get("ts"))
                    rec["pnl_usd"]    = round(pnl, 4)
                    rec["result_r"]   = round(result_r, 4)
                    rec["result_ccy"] = round(pnl, 4)
                    rec["exit_reason"] = exit_reason
                    if _HAS_DB:
                        try:
                            _ss.close_trade(
                                tr.get("id"), exit_px, round(pnl, 4),
                                rec["outcome"], datetime.now(),
                                sym=sym, market=tr.get("market"),
                                direction=tr.get("dir"), qty=tr.get("qty"),
                                entry=entry, sl=tr.get("initial_stop"),
                                tp=tr.get("initial_tp"), atr=tr.get("atr"),
                                sig_mode=rec.get("sig_mode"),
                                opened_at=rec.get("ts"),
                                trade_date=rec.get("trade_date"),
                                status="closed",
                                result_r=rec.get("result_r"),
                                result_ccy=rec.get("result_ccy"),
                                exit_reason=rec.get("exit_reason"),
                                spread_execution=rec.get("spread_execution"),
                                slippage=rec.get("slippage"),
                                commission=round_trip_commission,
                            )
                        except Exception as _dbe:
                            log.error(f"[DB] close_trade failed for {sym}: {_dbe}")
                    log.info(f"Trade closed {sym}: {rec['outcome']} "
                             f"(${pnl:+.2f}, exit={exit_px})")
                    emoji = "✅" if pnl > 0 else "❌"
                    notify(
                        f"{emoji} TRADE CLOSED\n"
                        f"{sym} {tr['dir']}  {rec['outcome'].upper()}\n"
                        f"P&L: ${pnl:+.2f}  Exit: {exit_px}\n"
                        f"Daily P&L: ${self.daily_pnl():+.2f}"
                    )
                    break
            self._save()
            # Determine outcome for cooldown logic: find the rec we just wrote
            _outcome = "unknown"
            for _r in reversed(self._log):
                if _r.get("id") == self._open_trades.get(sym, {}).get("id") or \
                   (_r.get("sym") == sym and _r.get("outcome") in ("win", "loss")):
                    _outcome = _r.get("outcome", "unknown")
                    break
            self.cleanup_closed_trade(sym, outcome=_outcome)

    def manage_trailing_stops(self, current_prices: dict):
        """
        For each open trade, check if profit has crossed trail_trigger_r.
        Once triggered, trail the stop behind price by trail_distance_atr * ATR.
        current_prices: dict of sym -> latest price (from the scan cycle).
        Live orders: actually moves the stop-loss order in IBKR.
        Dry-run: just logs what would have happened (no order to move).
        """
        if not CFG["trailing_stop_enabled"]:
            return
        for sym, tr in list(self._open_trades.items()):
            price = current_prices.get(sym)
            if price is None:
                continue
            risk = tr["risk_per_unit"]
            if risk <= 0:
                continue
            if tr["dir"] == "BUY":
                profit_r = (price - tr["entry"]) / risk
            else:
                profit_r = (tr["entry"] - price) / risk

            if not tr["trailing_active"] and profit_r >= CFG["trail_trigger_r"]:
                tr["trailing_active"] = True
                log.info(f"Trailing stop activated for {sym} at {profit_r:.2f}R profit")

            if tr["trailing_active"]:
                trail_dist = tr["atr"] * CFG["trail_distance_atr"]
                be = self._round_px(tr["entry"], tr.get("market", "stock_us"))
                if tr["dir"] == "BUY":
                    new_stop = self._round_px(max(price - trail_dist, be), tr.get("market", "stock_us"))
                    moved = new_stop > tr["current_stop"]
                else:
                    new_stop = self._round_px(min(price + trail_dist, be), tr.get("market", "stock_us"))
                    moved = new_stop < tr["current_stop"]

                if moved:
                    old_stop = tr["current_stop"]
                    tr["current_stop"] = new_stop
                    if self.dry_run:
                        log.info(f"[DRY] Trail {sym}: stop {old_stop} -> {new_stop} "
                                 f"(price {price}, {profit_r:.2f}R)")
                    else:
                        oid = tr.get("stop_order_id")
                        contract = tr.get("stop_contract")
                        if oid and contract:
                            try:
                                from ib_insync import StopOrder
                                stop_qty = tr.get("remaining_qty", tr["qty"])
                                new_order = StopOrder(
                                    "SELL" if tr["dir"] == "BUY" else "BUY",
                                    stop_qty, new_stop)
                                new_order.orderId = oid
                                self.ib.placeOrder(contract, new_order)
                                log.info(f"Trailed {sym}: stop {old_stop} -> {new_stop} "
                                         f"(price {price}, {profit_r:.2f}R, qty={stop_qty})")
                            except Exception as e:
                                log.debug(f"Trail update failed {sym}: {e}")

    def close_profitable_positions(self):
        """Each cycle: close any open position that is at breakeven or better."""
        if self.dry_run:
            return
        try:
            from ib_insync import MarketOrder, Forex
            portfolio = self.ib.portfolio()
            for item in portfolio:
                if item.unrealizedPNL <= 0:
                    continue
                c = item.contract
                qty = abs(item.position)
                if qty <= 0:
                    continue
                # For forex (CASH) contracts, the portfolio item has no exchange set and
                # symbol='NZD' not 'NZDUSD'. Rebuild a proper Forex contract so
                # qualifyContracts can resolve it (exchange=IDEALPRO).
                if getattr(c, 'secType', '') == 'CASH':
                    sym = c.symbol + c.currency  # full pair e.g. 'NZDUSD' (matches _open_trades key)
                    ibkr_sym = c.symbol           # base currency e.g. 'NZD' (matches openTrades symbol)
                    raw = Forex(sym)
                else:
                    sym = c.symbol
                    ibkr_sym = c.symbol
                    raw = c
                qualified = self.ib.qualifyContracts(raw)
                if not qualified:
                    log.error(f"Auto-close: could not qualify contract for {sym}")
                    continue
                contract = qualified[0]
                # cancel bracket child orders first so they don't re-open the position
                open_orders = [t for t in self.ib.openTrades()
                               if t.contract.symbol == ibkr_sym]
                for t in open_orders:
                    self.ib.cancelOrder(t.order)
                self.ib.sleep(0.5)
                close_dir = "SELL" if item.position > 0 else "BUY"
                order = MarketOrder(close_dir, qty)
                order.outsideRth = True  # allow after-hours execution
                self.ib.placeOrder(contract, order)
                log.info(f"Auto-close profitable: {close_dir} {qty}x {sym} "
                         f"unrealizedPNL=${item.unrealizedPNL:+.2f}")
        except Exception as e:
            log.error(f"close_profitable_positions: {e}")

    def close_all_positions(self):
        """Close every open position in the IBKR account at market — called on bot shutdown."""
        if self.dry_run:
            return
        try:
            from ib_insync import MarketOrder, Forex
            portfolio = self.ib.portfolio()
            closed = []
            for item in portfolio:
                if item.position == 0:
                    continue
                c = item.contract
                qty = abs(item.position)
                if getattr(c, 'secType', '') == 'CASH':
                    sym = c.symbol + c.currency
                    raw = Forex(sym)
                else:
                    sym = c.symbol
                    raw = c
                qualified = self.ib.qualifyContracts(raw)
                if not qualified:
                    log.error(f"Close-all: could not qualify {sym}")
                    continue
                contract = qualified[0]
                # cancel any pending bracket child orders first
                for t in self.ib.openTrades():
                    if t.contract.symbol == c.symbol:
                        self.ib.cancelOrder(t.order)
                self.ib.sleep(0.5)
                close_dir = "SELL" if item.position > 0 else "BUY"
                order = MarketOrder(close_dir, qty)
                order.outsideRth = True
                if self.account_id:
                    order.account = self.account_id
                self.ib.placeOrder(contract, order)
                closed.append(f"{sym} {close_dir} {qty}")
                log.info(f"Close-all: {close_dir} {qty}x {sym} @ market (unrealPNL=${item.unrealizedPNL:+.2f})")
            if closed:
                notify(f"🔴 BOT SHUTDOWN — CLOSED ALL\n" + "\n".join(closed))
            else:
                log.info("Close-all: no open positions to close.")
        except Exception as e:
            log.error(f"close_all_positions: {e}")

    def register_api_rejection(self, sym, code):
        counts = self._rejection_counts.setdefault(sym, {})
        counts[code] = counts.get(code, 0) + 1
        if counts[code] >= 3:
            cooldown_bars = 2  # ~30 min at 14m50s scan cadence
            self._symbol_cooldowns[sym] = max(self._symbol_cooldowns.get(sym, 0), cooldown_bars)
            log.warning(f"Rejection backoff: {sym} code={code} hit {counts[code]}x — "
                        f"cooling down {cooldown_bars} bars (~30min)")
            counts[code] = 0

    def cleanup_closed_trade(self, sym, reason="closed", outcome="unknown"):
        """Call when a position is no longer open, to stop tracking it."""
        was_open = sym in self._open_trades
        tr = self._open_trades.get(sym, {})
        direction = tr.get("dir")
        self._open_trades.pop(sym, None)
        if was_open:
            is_loss = outcome in ("loss",) or reason in ("stop_loss", "stop")
            if is_loss:
                if direction:
                    self._stop_loss_direction_block[sym] = direction
                    log.info(f"{sym}: {direction} direction blocked for 30 min after stop-loss")
                # 30-min cooldown after stop — 2h was too aggressive on a 5.5h session
                cooldown_secs = 1800  # 30 minutes
                cooldown = cooldown_secs // max(CFG.get("scan_sec", 15), 1)
            else:
                cooldown = CFG.get("symbol_cooldown_bars", 60)
                cooldown_secs = cooldown * CFG.get("scan_sec", 15)
            self._symbol_cooldowns[sym] = max(
                self._symbol_cooldowns.get(sym, 0), cooldown)
            expiry = datetime.now() + timedelta(seconds=cooldown_secs)
            self._cooldown_expiry[sym] = expiry
            self._save_cooldowns()
            log.info(f"Closed {sym} ({reason}/{outcome}) — re-entry cooldown {cooldown_secs//60}min")

    def tick_bars(self, positions):
        scan_sec = CFG.get("scan_sec", 15)
        for sym in list(self._open_bars.keys()):
            if sym not in positions:
                del self._open_bars[sym]
                self.cleanup_closed_trade(sym)
                continue
            self._open_bars[sym] += 1
            tr = self._open_trades.get(sym, {})
            # Broker-adopted/manual positions have no bot bracket metadata. They
            # block duplicate entries and stay visible on the dashboard, but the
            # bot must not auto-flatten them on a max-hold timer.
            if not (tr.get("initial_stop") or tr.get("current_stop")) or not tr.get("initial_tp"):
                continue
            mkt = tr.get("market", "stock_us")
            if mkt == "forex":
                max_mins = CFG.get("max_hold_mins_forex", 150)
            elif mkt == "future":
                max_mins = CFG.get("max_hold_mins_future", 50)
            elif mkt == "stock_uk":
                max_mins = CFG.get("max_hold_mins_stock_uk", 90)
            elif mkt in ("index_cfd", "index_cfd_eu"):
                max_mins = CFG.get("max_hold_mins_index", 5760)
            else:
                max_mins = CFG.get("max_hold_mins_stock", 120)
            elapsed_mins = self._open_bars[sym] * scan_sec / 60
            if elapsed_mins >= max_mins:
                if tr.get("force_close_sent"):
                    # retry every 4 minutes if odd-lot order stalls
                    last_bar  = tr.get("force_close_bar", self._open_bars[sym])
                    mins_since = (self._open_bars[sym] - last_bar) * scan_sec / 60
                    if mins_since < 4:
                        continue
                    log.warning(f"Force-close retry: {sym} still open {elapsed_mins:.0f}min")
                    tr["force_close_sent"] = False
                log.warning(f"Force-close: {sym} held {self._open_bars[sym]} bars — submitting market close")
                if self.dry_run:
                    tr["force_close_sent"] = True
                    log.info(f"[DRY] Force-close market order: {sym}")
                else:
                    contract = tr.get("stop_contract")
                    # If stop_contract is missing (e.g. position restored from log),
                    # rebuild the contract from the known market type.
                    if contract is None:
                        from ib_insync import Forex, ContFuture, Stock
                        mkt = tr.get("market", "")
                        if mkt == "forex":
                            result = self.ib.qualifyContracts(Forex(sym))
                            contract = result[0] if result else None
                        elif mkt == "future":
                            extra = next((e for s, e in CFG.get("futures", []) if s == sym), "CME")
                            result = self.ib.qualifyContracts(ContFuture(sym, extra))
                            contract = result[0] if result else None
                        elif mkt in ("stock_us", "stock_uk", ""):
                            exch = "LSE" if mkt == "stock_uk" else "SMART"
                            curr = "GBP" if mkt == "stock_uk" else "USD"
                            try:
                                result = self.ib.qualifyContracts(Stock(sym, exch, curr))
                                contract = result[0] if result else None
                            except Exception:
                                contract = None
                    if contract:
                        try:
                            from ib_insync import MarketOrder
                            # cancel bracket children (SL/TP) before market close
                            # so they don't conflict with the new flat order
                            ibkr_sym = contract.symbol  # 'AUD' for AUDUSD, etc.
                            for t in self.ib.openTrades():
                                if t.contract.symbol == ibkr_sym:
                                    self.ib.cancelOrder(t.order)
                            self.ib.sleep(0.5)
                            close_dir = "SELL" if tr.get("dir") == "BUY" else "BUY"
                            qty = int(tr.get("remaining_qty") or tr.get("qty", 0))
                            if qty > 0:
                                order = MarketOrder(close_dir, qty)
                                order.outsideRth = True
                                self.ib.placeOrder(contract, order)
                                tr["force_close_sent"] = True
                                tr["force_close_bar"] = self._open_bars[sym]
                                log.warning(f"Force-close submitted: {close_dir} {qty}x {sym} at market")
                            else:
                                log.error(f"Force-close: qty=0 for {sym}, cannot close")
                        except Exception as e:
                            log.error(f"Force-close order failed {sym}: {e}")
                    else:
                        log.error(f"Force-close: no contract stored for {sym}, cannot close")


# ══════════════════════════════════════════════════════════════════════════════
#  DISPLAY
# ══════════════════════════════════════════════════════════════════════════════

_W = 80   # terminal width

def _c(v, dp=4):
    try:
        f = float(v)
        col = Fore.GREEN if f > 0 else Fore.RED if f < 0 else Fore.WHITE
        return f"{col}{f:+,.{dp}f}{Style.RESET_ALL}"
    except Exception:
        return str(v)

def _pbar(used, total, width=20):
    if total <= 0: pct = 0
    else: pct = min(abs(used) / abs(total), 1.0)
    filled = int(pct * width)
    col = Fore.GREEN if pct < 0.5 else Fore.YELLOW if pct < 0.85 else Fore.RED
    return f"{col}{'█'*filled}{Style.DIM}{'░'*(width-filled)}{Style.RESET_ALL}"

def _infer_market(sym):
    """Infer market from symbol for legacy log entries that lack a market field."""
    futures_syms = {s for s, _ in CFG.get("futures", [])}
    uk_syms      = set(CFG.get("stocks_uk", []) + ["VOD","BARC","HSBA","GSK","ULVR","AZN","LLOY"])
    if len(sym) == 6 and sym.isalpha():
        return "forex"
    if sym in futures_syms:
        return "future"
    if sym in uk_syms:
        return "stock_uk"
    return "stock_us"

def _market_stats():
    """
    Returns per-market W/L/P&L for today and the past 7 days.
    Structure: {market: {today: {w,l,pnl}, week: {w,l,pnl}}}
    """
    try:
        with open("scalper_trades.json") as f:
            trades = json.load(f)
    except Exception:
        return {}

    today = _now_trade_tz().date().isoformat()
    week_start, week_end = _trade_week_bounds(today)
    markets  = ["stock_us", "stock_uk", "forex", "future"]
    out = {m: {"today": {"w":0,"l":0,"pnl":0.0},
               "week":  {"w":0,"l":0,"pnl":0.0}} for m in markets}

    for t in trades:
        if t.get("outcome") not in ("win", "loss"):
            continue
        trade_date = t.get("trade_date") or _trade_date_from_value(t.get("exit_ts"), fallback=t.get("ts"))
        if not (week_start <= trade_date <= week_end):
            continue
        mkt = t.get("market") or _infer_market(t.get("sym",""))
        if mkt not in out:
            continue
        pnl = t.get("pnl_usd") or 0.0
        w   = 1 if t["outcome"] == "win" else 0
        l   = 1 if t["outcome"] == "loss" else 0
        out[mkt]["week"]["w"]   += w
        out[mkt]["week"]["l"]   += l
        out[mkt]["week"]["pnl"] += pnl
        if trade_date == today:
            out[mkt]["today"]["w"]   += w
            out[mkt]["today"]["l"]   += l
            out[mkt]["today"]["pnl"] += pnl

    return out

def _session_info():
    """Returns compact session status strings for SAST (UTC+2)."""
    now = _now_trade_tz()
    mins = _minutes_since_midnight(now)
    sessions = [
        ("Forex",  23*60,      2*60),       # Sunday 23:00 → Saturday 02:00 gate enforced separately
        ("London", 9*60+50,    19*60+30),
        ("NY",     15*60+20,   2*60),
    ]
    parts = []
    if not _global_trading_enabled(now):
        return f"{Fore.RED}Weekend shutdown until Sun 23:00 SAST{Style.RESET_ALL}"
    for name, start, end in sessions:
        if _is_within_minute_window(mins, start, end):
            left = (end - mins) % (24 * 60)
            parts.append(f"{Fore.GREEN}{name} OPEN {left//60}h{left%60:02d}m{Style.RESET_ALL}")
        else:
            opens = (start - mins) % (24*60)
            parts.append(f"{Style.DIM}{name} opens {opens//60}h{opens%60:02d}m{Style.RESET_ALL}")
    reset_in = (22*60 - mins) % (24*60)
    parts.append(f"{Fore.YELLOW}Reset {reset_in//60}h{reset_in%60:02d}m{Style.RESET_ALL}")
    return "  ·  ".join(parts)

def show_header(acct, pnl, reg, atr_pct, mode, dry, spy_data_ok=True,
                orders=None):
    os.system("clear")
    now   = datetime.now().strftime("%a %d %b %Y  %H:%M:%S")
    mode_str = (Fore.RED + Style.BRIGHT + "LIVE" + Style.RESET_ALL if mode == "live"
                else Fore.YELLOW + "PAPER" + Style.RESET_ALL)
    dry_str = f"  {Fore.MAGENTA}[DRY-RUN]{Style.RESET_ALL}" if dry else ""
    rc  = REGIME_COLOR.get(reg, "")
    atr_str = (f"{atr_pct*100:.2f}%" if spy_data_ok
               else Fore.RED+"NO DATA"+Style.RESET_ALL)

    # ── top bar ──────────────────────────────────────────────────────────────
    print(f"{Fore.CYAN}{'═'*_W}{Style.RESET_ALL}")
    print(f"  {Fore.CYAN}{Style.BRIGHT}IBKR SCALPING BOT v4.0{Style.RESET_ALL}  ·  "
          f"{mode_str}{dry_str}  ·  "
          f"{Style.DIM}cap ${CFG['capital_cap_usd']:,.0f}  "
          f"risk {CFG['risk_pct']}%/trade  "
          f"max ${CFG['capital_cap_usd']*CFG['max_position_pct']:,.0f}/pos{Style.RESET_ALL}"
          f"{'':>4}{Style.DIM}{now}{Style.RESET_ALL}")
    print(f"{Fore.CYAN}{'═'*_W}{Style.RESET_ALL}")

    # ── sessions / regime / account ──────────────────────────────────────────
    print(f"  {Style.DIM}Sessions:{Style.RESET_ALL}  {_session_info()}")
    print(f"  {Style.DIM}Regime  :{Style.RESET_ALL}  "
          f"{rc}{Style.BRIGHT}{reg}{Style.RESET_ALL}  "
          f"{Style.DIM}(SPY atr {atr_str}){Style.RESET_ALL}")
    tgt = CFG["daily_target_usd"]
    lim = CFG["daily_loss_usd"]
    pnl_col = Fore.GREEN if pnl >= 0 else Fore.RED
    print(f"  {Style.DIM}Account :{Style.RESET_ALL}  "
          f"{Style.BRIGHT}${acct:>10,.2f}{Style.RESET_ALL}   "
          f"P&L: {pnl_col}{pnl:+,.2f}{Style.RESET_ALL}")
    print(f"  {Style.DIM}Target  :{Style.RESET_ALL}  "
          f"{_pbar(max(0,pnl), tgt)}  "
          f"{Fore.GREEN}${max(0,pnl):,.2f}{Style.RESET_ALL} / ${tgt:,.2f}")
    print(f"  {Style.DIM}Loss lim:{Style.RESET_ALL}  "
          f"{_pbar(max(0,-pnl), lim)}  "
          f"{Fore.RED}${max(0,-pnl):,.2f}{Style.RESET_ALL} / ${lim:,.2f}")

    # ── per-market breakdown ─────────────────────────────────────────────────
    stats = _market_stats()
    now_sast = _now_trade_tz()
    mins_now = _minutes_since_midnight(now_sast)

    def _mkt_status(mkt):
        if not _global_trading_enabled(now_sast):
            return f"{Fore.RED}WEEKEND SHUTDOWN{Style.RESET_ALL}"
        if mkt == "forex":
            if _forex_window_active(now_sast):
                return f"{Fore.GREEN}ACTIVE (Sun 23:00-Sat 02:00){Style.RESET_ALL}"
            return f"{Style.DIM}Forex closed until Sun 23:00 SAST{Style.RESET_ALL}"
        if mkt == "stock_uk":
            in_win = in_trade_window("stock_uk")
            if in_win:
                return f"{Fore.GREEN}ACTIVE (UK){Style.RESET_ALL}"
            opens_at = CFG["trade_windows_uk_sast"][0]
            return f"{Style.DIM}LSE · opens {opens_at[0]:02d}:{opens_at[1]:02d} SAST{Style.RESET_ALL}"
        if mkt == "stock_us":
            in_win = in_trade_window(mkt)
            if in_win and reg != "CHOP":
                return f"{Fore.GREEN}ACTIVE{Style.RESET_ALL}"
            if in_win and reg == "CHOP":
                return f"{Fore.YELLOW}OPEN · CHOP blocked{Style.RESET_ALL}"
            opens_at = CFG["trade_windows_us_sast"][0]
            return f"{Style.DIM}Pre-market · opens {opens_at[0]:02d}:{opens_at[1]:02d}{Style.RESET_ALL}"
        if mkt == "future":
            in_win = in_trade_window(mkt)
            if in_win:
                return f"{Fore.GREEN}ACTIVE{Style.RESET_ALL}"
            opens_at = CFG["trade_windows_us_sast"][0]
            return f"{Style.DIM}Pre-market · opens {opens_at[0]:02d}:{opens_at[1]:02d}{Style.RESET_ALL}"
        if mkt == "index_cfd":
            wins = CFG.get("trade_windows_index_us_sast", CFG["trade_windows_us_sast"])
            in_win = in_trade_window("index_cfd")
            if in_win:
                return f"{Fore.GREEN}ACTIVE (US Index){Style.RESET_ALL}"
            opens_at = wins[0]
            return f"{Style.DIM}US Index · opens {opens_at[0]:02d}:{opens_at[1]:02d} SAST{Style.RESET_ALL}"
        if mkt == "index_cfd_eu":
            wins = CFG.get("trade_windows_eu_index_sast", [])
            in_win = in_trade_window("index_cfd_eu") if wins else False
            if in_win:
                return f"{Fore.GREEN}ACTIVE (EU Index){Style.RESET_ALL}"
            opens_at = wins[0] if wins else (10, 0, 18, 30)
            return f"{Style.DIM}EU Index · opens {opens_at[0]:02d}:{opens_at[1]:02d} SAST{Style.RESET_ALL}"
        return ""

    mkt_labels = {
        "stock_us":    "US Stocks ",
        "stock_uk":    "UK Stocks ",
        "forex":       "Forex     ",
        "future":      "Futures   ",
        "index_cfd":   "US Index  ",
        "index_cfd_eu":"EU Index  ",
    }
    mkt_detail = {
        "stock_us":    f"15min · RR per-sym (1.5–5.0) · hold {CFG['max_hold_mins_stock']}min  ·  {len(CFG['stocks_us'])} instruments",
        "stock_uk":    f"15min · RR{CFG['rr_ratio_uk']} · hold {CFG['max_hold_mins_stock_uk']}min  ·  {len(CFG['stocks_uk'])} instruments",
        "forex":       f"15min · RR{CFG['rr_ratio_forex']}  · hold {CFG['max_hold_mins_forex']}min",
        "future":      f" 5min · RR{CFG['rr_ratio']}  · hold {CFG['max_hold_mins_future']}min",
        "index_cfd":   f"daily · RR2.0 · hold {CFG.get('max_hold_mins_index', 5760)}min  ·  {len(CFG.get('indices', []))} instruments",
        "index_cfd_eu":f"daily · RR2.0 · hold {CFG.get('max_hold_mins_index', 5760)}min  ·  {len(CFG.get('indices_eu', []))} instruments",
    }

    # only show markets that have active instruments configured
    active_markets = []
    if CFG.get("stocks_us"):   active_markets.append("stock_us")
    if CFG.get("stocks_uk"):   active_markets.append("stock_uk")
    if CFG.get("forex"):       active_markets.append("forex")
    if CFG.get("futures"):     active_markets.append("future")
    if CFG.get("indices"):     active_markets.append("index_cfd")
    if CFG.get("indices_eu"):  active_markets.append("index_cfd_eu")

    print()
    print(f"{Fore.CYAN}  {'─'*26} MARKET BREAKDOWN {'─'*32}{Style.RESET_ALL}")
    print(f"  {Style.DIM}{'Market':<11}{'Today W/L':<12}{'Today P&L':<12}{'Week W/L':<12}{'Week P&L':<12}{'Status / Detail'}{Style.RESET_ALL}")
    print(f"  {Style.DIM}{'─'*76}{Style.RESET_ALL}")

    week_w = week_l = 0
    week_pnl = 0.0
    for mkt in active_markets:
        s   = stats.get(mkt, {"today":{"w":0,"l":0,"pnl":0.0},"week":{"w":0,"l":0,"pnl":0.0}})
        td  = s["today"]
        wk  = s["week"]
        week_w   += wk["w"]; week_l += wk["l"]; week_pnl += wk["pnl"]
        tp_col  = Fore.GREEN if td["pnl"] >= 0 else Fore.RED
        wp_col  = Fore.GREEN if wk["pnl"] >= 0 else Fore.RED
        wl_col  = Fore.GREEN if td["w"] > td["l"] else (Fore.RED if td["l"] > td["w"] else Style.DIM)
        label   = mkt_labels[mkt]
        detail  = mkt_detail[mkt]
        status  = _mkt_status(mkt)
        print(f"  {Style.BRIGHT}{label}{Style.RESET_ALL}"
              f"{wl_col}{td['w']}W/{td['l']}L{Style.RESET_ALL}{'':>7}"
              f"{tp_col}${td['pnl']:+.2f}{Style.RESET_ALL}{'':>5}"
              f"{Fore.GREEN if wk['w']>wk['l'] else Fore.RED if wk['l']>wk['w'] else Style.DIM}"
              f"{wk['w']}W/{wk['l']}L{Style.RESET_ALL}{'':>7}"
              f"{wp_col}${wk['pnl']:+.2f}{Style.RESET_ALL}{'':>4}"
              f"{Style.DIM}{detail}{Style.RESET_ALL}  {status}")

    # weekly combined total
    print(f"  {Style.DIM}{'─'*76}{Style.RESET_ALL}")
    all_pnl_col = Fore.GREEN if week_pnl >= 0 else Fore.RED
    all_wl_col  = Fore.GREEN if week_w > week_l else Fore.RED
    print(f"  {Style.BRIGHT}{'WEEK TOTAL':<11}{Style.RESET_ALL}"
          f"{'':>12}"
          f"{'':>12}"
          f"{all_wl_col}{week_w}W/{week_l}L{Style.RESET_ALL}{'':>7}"
          f"{all_pnl_col}${week_pnl:+.2f}{Style.RESET_ALL}")
    print()

def show_signals(sigs):
    hdr = f"{Fore.CYAN}  {'─'*32} SIGNALS {'─'*37}{Style.RESET_ALL}"
    print(hdr)
    if not sigs:
        print(f"  {Style.DIM}No signals this cycle — market quiet or outside trade window.{Style.RESET_ALL}\n")
        return
    rows = []
    for s in sigs:
        dc  = Fore.GREEN if s["dir"]=="BUY" else Fore.RED
        rc  = REGIME_COLOR.get(s.get("regime",""), "")
        gc  = Fore.GREEN if s["gate"]=="OK" else Fore.RED
        rows.append([
            f"{Style.BRIGHT}{s['sym']}{Style.RESET_ALL}",
            f"{dc}{s['dir']}{Style.RESET_ALL}",
            s["market"],
            f"{s['price']:.5g}",
            f"{s['rsi']:.1f}",
            f"{s['macd_h']:+.4f}",
            f"{s['vsurge']:.1f}x",
            f"{s['atr_pct']*100:.3f}%",
            f"{rc}{s.get('regime','?')}{Style.RESET_ALL}",
            f"{gc}{s['gate']}{Style.RESET_ALL}",
        ])
    print("  " + tabulate(rows,
        headers=["Sym","Dir","Market","Price","RSI","MACD","Vol","ATR%","Regime","Gate"],
        tablefmt="simple").replace("\n","\n  "))
    print()

def show_open_positions(open_pos, orders):
    hdr = f"{Fore.CYAN}  {'─'*30} OPEN POSITIONS ({len(open_pos)}) {'─'*33}{Style.RESET_ALL}"
    print(hdr)
    if not open_pos:
        print(f"  {Style.DIM}None{Style.RESET_ALL}")
    else:
        for sym in open_pos:
            tr = orders._open_trades.get(sym, {})
            raw_bars = orders._open_bars.get(sym, 0)
            scan_sec = CFG.get("scan_sec", 15)
            elapsed_mins = raw_bars * scan_sec / 60
            mkt = tr.get("market", "stock_us")
            if mkt == "forex":
                max_mins = CFG.get("max_hold_mins_forex", 150)
            elif mkt == "future":
                max_mins = CFG.get("max_hold_mins_future", 50)
            elif mkt == "stock_uk":
                max_mins = CFG.get("max_hold_mins_stock_uk", 90)
            else:
                max_mins = CFG.get("max_hold_mins_stock", 120)
            entry = tr.get("entry", "?")
            sl    = tr.get("current_stop", tr.get("initial_stop","?"))
            tp    = tr.get("initial_tp","?")
            dir_  = tr.get("dir","?")
            trail = f"  {Fore.YELLOW}[trailing]{Style.RESET_ALL}" if tr.get("trailing_active") else ""
            dc    = Fore.GREEN if dir_=="BUY" else Fore.RED
            has_sl = isinstance(sl, (int, float)) and sl > 0
            has_tp = isinstance(tp, (int, float)) and tp > 0
            risk_line = (
                f"SL {Fore.RED}{sl}{Style.RESET_ALL}  "
                f"TP {Fore.GREEN}{tp}{Style.RESET_ALL}"
                if has_sl and has_tp
                else f"{Fore.YELLOW}UNPROTECTED: no bot SL/TP{Style.RESET_ALL}"
            )
            print(f"  {Style.BRIGHT}{sym}{Style.RESET_ALL}  "
                  f"{dc}{dir_}{Style.RESET_ALL}  "
                  f"entry {Style.BRIGHT}{entry}{Style.RESET_ALL}  "
                  f"{risk_line}  "
                  f"{Style.DIM}{elapsed_mins:.0f}min/{max_mins}min{Style.RESET_ALL}"
                  f"{trail}")
    print()


# ══════════════════════════════════════════════════════════════════════════════
#  WALK-FORWARD BACKTEST  (yfinance daily bars — no IBKR connection needed)
# ══════════════════════════════════════════════════════════════════════════════

class WalkForward:
    def __init__(self, sym, n_folds=5, fractional=False):
        self.sym = sym; self.n_folds = n_folds; self.fractional = fractional

    def run(self):
        df = YFData.daily(self.sym, days=CFG["backtest_days"])
        if df is None or len(df) < 60: return None
        fold_size = len(df) // self.n_folds
        results = []
        for i in range(self.n_folds):
            ts = i * fold_size + fold_size
            te = min(ts + fold_size, len(df))
            if te <= ts: continue
            test = df.iloc[max(0, ts-50):te]
            equity = 10_000.0; trades = wins = 0; peak = equity
            for j in range(50, len(test)):
                w = test.iloc[j-50:j+1]
                reg, _ = regime_of(w)
                if reg == "CHOP": continue
                sig = evaluate_signal(w)
                if sig is None: continue
                ok, _ = execution_ok(sig["price"], sig["atr"])
                if not ok: continue
                if j+1 >= len(test): continue
                entry = apply_costs(sig["price"], sig["direction"], sig["atr"])
                qty   = position_size(sig["price"], sig["atr"], equity, reg,
                                      allow_fractional=self.fractional)
                if qty <= 0: continue
                raw   = float(test["Close"].iloc[j+1])
                xdir  = "SELL" if sig["direction"]=="BUY" else "BUY"
                xpx   = apply_costs(raw, xdir, sig["atr"])
                pnl   = (xpx-entry)*qty if sig["direction"]=="BUY" else (entry-xpx)*qty
                pnl  -= CFG["commission_per_trade_usd"] * 2
                equity += pnl; peak = max(peak, equity); trades += 1
                if pnl > 0: wins += 1
            dd  = (peak-equity)/peak*100 if peak>0 else 0
            wr  = wins/trades*100 if trades>0 else 0
            ret = (equity-10_000)/10_000*100
            results.append({"fold":i+1,"trades":trades,"win_pct":round(wr,1),
                            "return":round(ret,2),"max_dd":round(dd,2)})
        if not results: return None
        avg_wr = np.mean([r["win_pct"] for r in results])
        avg_ret= np.mean([r["return"]  for r in results])
        avg_dd = np.mean([r["max_dd"]  for r in results])
        return {"symbol":self.sym,"folds":results,
                "avg_win":round(avg_wr,1),"avg_ret":round(avg_ret,2),
                "avg_dd":round(avg_dd,2),
                "verdict":"TRADEABLE" if (avg_wr>=50 and avg_ret>0 and avg_dd<15) else "RISKY"}


def _report(sym, res, results):
    if res:
        results.append(res)
        rows = [[r["fold"],r["trades"],f"{r['win_pct']}%",
                 f"{r['return']:+.1f}%",f"{r['max_dd']:.1f}%"] for r in res["folds"]]
        print(f"\n  {res['symbol']}  avg win={res['avg_win']}%  "
              f"avg ret={res['avg_ret']:+.1f}%  avg DD={res['avg_dd']:.1f}%  {res['verdict']}")
        print(tabulate(rows,headers=["Fold","Trades","Win%","Return","MaxDD"],tablefmt="simple"))
    else:
        print(f"  {sym}: not enough data")


def run_backtest():
    print(BANNER)
    print(Fore.CYAN+"  WALK-FORWARD BACKTEST  (daily bars + execution costs)\n"+Style.RESET_ALL)
    print(Fore.YELLOW+"  Note: backtest uses yfinance daily bars — live bot uses IBKR real-time bars.\n"+Style.RESET_ALL)
    results = []
    for sym in CFG["stocks_us"]:
        print(f"  Testing {sym} ...", end="\r")
        _report(sym, WalkForward(sym, n_folds=5, fractional=False).run(), results)
    for sym in CFG["stocks_uk"]:
        yf_sym = f"{sym}.L"   # Yahoo Finance suffix for LSE-listed stocks
        print(f"  Testing {yf_sym} ...", end="\r")
        _report(yf_sym, WalkForward(yf_sym, n_folds=5, fractional=False).run(), results)
    for pair in CFG["forex"]:
        yf_sym = f"{pair[:3]}{pair[3:]}=X"
        print(f"  Testing {yf_sym} ...", end="\r")
        _report(yf_sym, WalkForward(yf_sym, n_folds=5, fractional=True).run(), results)
    for c in CFG["crypto"]:
        yf_sym = f"{c}-USD"
        print(f"  Testing {yf_sym} ...", end="\r")
        _report(yf_sym, WalkForward(yf_sym, n_folds=5, fractional=True).run(), results)
    print(Fore.CYAN+"\n  SUMMARY\n"+Style.RESET_ALL)
    print(f"  Tradeable: {', '.join(r['symbol'] for r in results if r['verdict']=='TRADEABLE') or 'none'}")
    print(f"  Risky:     {', '.join(r['symbol'] for r in results if r['verdict']=='RISKY') or 'none'}\n")


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN BOT  — live IBKR bars for all signal calculations
# ══════════════════════════════════════════════════════════════════════════════

class ScalpBot:
    def __init__(self, args):
        self.args      = args
        self.dry_run   = args.dry_run
        self.mode      = "live" if args.live else "paper"
        self.ib        = IB()
        self.contracts = None
        self.orders    = None
        self.ibdata    = None
        self._halt     = False
        self._had_open_positions = False
        self._disconnect_ts: float | None = None  # debounce reconnect — time of first disconnect
        self._news_sentiment: dict = {}  # {sym: {"sentiment":..., "score":..., "headlines":[...]}}
        self._market_data_blocked_symbols: set[str] = set()

    def connect(self):
        port = CFG["live_port"] if self.args.live else CFG["paper_port"]
        if self.args.port: port = self.args.port
        log.info(f"Connecting {CFG['host']}:{port} ...")
        client_id = CFG["client_id"]
        round_num = 0
        while True:
            # Heartbeat so dashboard shows "bot alive" during connection retries.
            if _HAS_DB:
                try:
                    _ss.set_status("last_heartbeat", datetime.now().isoformat())
                except Exception:
                    pass
            try:
                self.ib.connect(CFG["host"], port, clientId=client_id,
                                readonly=False, timeout=20)
                log.info(f"Connected (client_id={client_id})")
                if _HAS_DB:
                    try:
                        _ss.set_status("ibkr_connected", True)
                    except Exception:
                        pass
                break
            except Exception as e:
                round_num += 1
                wait = min(60 * round_num, 300)
                log.warning(f"Connect failed on fixed client_id={client_id} "
                            f"(round {round_num}): {e}. Waiting {wait}s...")
                # Keep heartbeat alive during the long wait so dashboard doesn't show offline
                deadline = time.time() + wait
                while time.time() < deadline:
                    if _HAS_DB:
                        try:
                            _ss.set_status("last_heartbeat", datetime.now().isoformat())
                        except Exception:
                            pass
                    time.sleep(10)

        # Explicit account verification — confirm what IBKR reports as the
        # managed account(s) and store it so every order is stamped with it.
        managed_accounts = self.ib.managedAccounts()
        if not managed_accounts:
            log.error("No managed accounts returned by IBKR — connection may "
                      "not be properly authenticated. Aborting.")
            sys.exit(1)
        self.account_id = managed_accounts[0]
        expected_mode = "live" if self.args.live else "paper"
        log.info(f"Connected account: {self.account_id}  "
                 f"(expected mode: {expected_mode.upper()}, port: "
                 f"{CFG['live_port'] if self.args.live else CFG['paper_port']})")
        if len(managed_accounts) > 1:
            log.warning(f"Multiple managed accounts found: {managed_accounts} — "
                       f"using {self.account_id}. Verify this is correct.")

        self.contracts = Contracts(self.ib)
        self.orders    = Orders(self.ib, dry_run=self.dry_run, account_id=self.account_id)
        self.ibdata    = IBKRData(self.ib)
        if _HAS_DB:
            try:
                prior_hmds_block = _ss.read_status("hmds_blocked")
                if str(prior_hmds_block).lower() == "true":
                    reason = (_ss.read_status("hmds_block_reason")
                              or "IBKR historical data session is locked")
                    self.ibdata.block_hmds(str(reason), minutes=60)
                    log.warning(f"Startup: preserving HMDS lock from dashboard DB — {reason}")
            except Exception:
                pass

        _BENIGN_INFO_CODES = {2100, 2101, 2102, 2103, 2104, 2105, 2106, 2107,
                               2108, 2109, 2110, 2119, 2150, 2158,
                               2157,   # sec-def farm disconnected — transient IBKR infra event unless persistent
                               10349,  # TIF set to DAY by order preset — informational
                               399,    # below IdealPro minimum, routed as odd lot — executes fine
                               101,    # cancel attempted when order not cancellable — TP/SL leg already filled
                               110,    # price not on valid tick — fixed by _round_px; debug if still fires
                               135,    # can't find order id — bracket child refs parent that was rejected
                               161,    # cancel attempted when order not cancellable — normal on restart cleanup
                               202,    # order cancelled — expected when we cancel bracket children
                               162,    # HMDS no data — expected outside market hours / short history
                               200,    # no security definition — contract not found, handled in Contracts
                               354,    # market data not subscribed — expected for some symbols/modes
                               10168,  # market data not subscribed / delayed disabled
                               492,    # order timing warning — benign informational
                               10089,  # live API market data subscription missing
                               10090,  # partial market data — expected for non-subscribed streams
                               10091,  # partial market data — expected for non-subscribed streams
                               326,    # client ID already in use — transient during reconnect
                               1100,   # connectivity between IBKR and Gateway lost — handled by reconnect loop
                               1101,   # connectivity restored, data lost — informational
                               1102,   # connectivity restored, data maintained — informational
                               }
        _BACKOFF_CODES = {10292, 10287, 110, 201}

        def _on_error(reqId, errorCode, errorString, contract):
            err_text = str(errorString)
            if errorCode in {354, 10089, 10168} and contract:
                sym = getattr(contract, "symbol", None)
                currency = getattr(contract, "currency", "")
                if sym:
                    blocked_sym = f"{sym}{currency}" if getattr(contract, "secType", "") == "CASH" else sym
                    self._market_data_blocked_symbols.add(blocked_sym)
                    log.warning(f"{blocked_sym}: market data blocked by IBKR code {errorCode}; "
                                "symbol disabled for new entries until bot restart")
                    if _HAS_DB:
                        try:
                            _ss.upsert_signal(
                                blocked_sym, "unknown", "no_data", None,
                                0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
                                "NO DATA", f"IBKR market data blocked ({errorCode})",
                                False, {"ibkr_error": errorCode, "message": err_text[:240]},
                            )
                        except Exception:
                            pass
            if (errorCode == 162 and
                    # IBKR's HMDS error text uses "TWS" for this session class.
                    "Trading TWS session is connected from a different IP address" in err_text):
                if self.ibdata:
                    self.ibdata.block_hmds(
                        "IBKR reports another trading session from a different IP for historical data",
                        minutes=60,
                    )
                if _HAS_DB:
                    try:
                        _ss.set_status("hmds_blocked", True)
                        _ss.set_status("hmds_block_reason", "IBKR reports another trading session from a different IP for historical data")
                    except Exception:
                        pass
            if (errorCode in {162, 366, 420, 100} and
                    any(word in err_text.lower() for word in ("pacing", "rate", "too many requests"))):
                if self.ibdata:
                    self.ibdata.block_hmds(
                        f"IBKR historical-data pacing limit: {err_text}",
                        minutes=60,
                    )
                if _HAS_DB:
                    try:
                        _ss.set_status("hmds_blocked", True)
                        _ss.set_status("hmds_block_reason", f"IBKR historical-data pacing limit: {err_text}")
                    except Exception:
                        pass
            if errorCode in _BENIGN_INFO_CODES:
                log.debug(f"IBKR info (code={errorCode}): {errorString}")
            else:
                log.error(f"IBKR ERROR (reqId={reqId}, code={errorCode}): "
                          f"{errorString}  [contract={contract}]")
                if errorCode in _BACKOFF_CODES and contract:
                    sym = getattr(contract, "symbol", None)
                    if sym and self.orders:
                        self.orders.register_api_rejection(sym, errorCode)
        self.ib.errorEvent += _on_error

    def _broker_protection_map(self) -> dict:
        """Return factual SL/TP orders currently open at IBKR, keyed by symbol."""
        protection = {}
        try:
            trades = self.ib.openTrades()
        except Exception:
            return protection
        for trd in trades:
            try:
                c = trd.contract
                o = trd.order
                sym = (c.symbol + c.currency) if getattr(c, "secType", "") == "CASH" else c.symbol
                row = protection.setdefault(sym, {"sl": 0.0, "tp": 0.0})
                typ = str(getattr(o, "orderType", "")).upper()
                if typ in ("STP", "STOP"):
                    row["sl"] = float(getattr(o, "auxPrice", 0) or 0)
                elif typ in ("LMT", "LIMIT"):
                    row["tp"] = float(getattr(o, "lmtPrice", 0) or 0)
            except Exception:
                continue
        return protection

    def _open_trade_meta_from_db(self) -> dict:
        """Open-trade metadata from dashboard DB, keyed by symbol. Not JSON."""
        if not _HAS_DB:
            return {}
        try:
            conn = _ss.get_conn()
            rows = conn.execute("""
                SELECT trade_id,sym,direction,qty,entry,sl,tp,market
                FROM trades
                WHERE closed_at IS NULL
            """).fetchall()
            conn.close()
            return {r["sym"]: dict(r) for r in rows}
        except Exception:
            return {}

    def _broker_position_rows(self) -> list[dict]:
        """Current IBKR positions, including forex positions omitted by portfolio()."""
        portfolio_by_sym = {}
        try:
            for item in self.ib.portfolio():
                if item.position == 0:
                    continue
                c = item.contract
                sym = (c.symbol + c.currency) if getattr(c, "secType", "") == "CASH" else c.symbol
                portfolio_by_sym[sym] = item
        except Exception:
            portfolio_by_sym = {}

        rows = []
        try:
            raw_positions = self.ib.positions()
        except Exception:
            raw_positions = []
        for p in raw_positions:
            if p.position == 0:
                continue
            c = p.contract
            sym = (c.symbol + c.currency) if getattr(c, "secType", "") == "CASH" else c.symbol
            sec_type = getattr(c, "secType", "STK")
            if sec_type == "CASH":
                market = "forex"
            elif getattr(c, "primaryExchange", "") == "LSE":
                market = "stock_uk"
            else:
                market = "stock_us"
            item = portfolio_by_sym.get(sym)
            entry = float(getattr(item, "averageCost", None) or getattr(p, "avgCost", 0) or 0)
            current = float(getattr(item, "marketPrice", None) or entry)
            unreal = float(getattr(item, "unrealizedPNL", None) or 0)
            rows.append({
                "sym": sym,
                "contract": c,
                "market": market,
                "direction": "BUY" if p.position > 0 else "SELL",
                "qty": abs(float(p.position)),
                "entry": entry,
                "current": current,
                "unreal": unreal,
                "raw_position": float(p.position),
            })
        return rows

    def _repair_missing_protection_from_db(self):
        """
        Re-create missing protective SL/TP orders for current IBKR positions using
        dashboard DB open-trade rows. This does not read scalper_trades.json.
        """
        if not _HAS_DB:
            return
        try:
            conn = _ss.get_conn()
            rows = conn.execute("""
                SELECT trade_id,sym,direction,qty,sl,tp
                FROM trades
                WHERE closed_at IS NULL
                  AND coalesce(sl,0) > 0
                  AND coalesce(tp,0) > 0
            """).fetchall()
            conn.close()
        except Exception as exc:
            log.warning(f"[protect] DB protection read failed: {exc}")
            return

        if not rows:
            return
        pos_by_sym = {}
        for row in self._broker_position_rows():
            pos_by_sym[row["sym"]] = row

        protection = self._broker_protection_map()
        repaired = 0
        for r in rows:
            sym = r["sym"]
            pos = pos_by_sym.get(sym)
            if not pos:
                continue
            existing = protection.get(sym, {})
            if existing.get("sl") and existing.get("tp"):
                continue
            qty = abs(float(pos["qty"]))
            if qty <= 0:
                continue
            close_action = "SELL" if pos["raw_position"] > 0 else "BUY"
            oca = f"protect_{r['trade_id']}"
            orders = []
            if not existing.get("tp"):
                tp_order = LimitOrder(close_action, qty, float(r["tp"]))
                orders.append(tp_order)
            if not existing.get("sl"):
                sl_order = StopOrder(close_action, qty, float(r["sl"]))
                orders.append(sl_order)
            for order in orders:
                order.tif = "DAY"
                order.ocaGroup = oca
                order.ocaType = 1
                if self.account_id:
                    order.account = self.account_id
                self.ib.placeOrder(pos["contract"], order)
            if orders:
                repaired += 1
                log.warning(f"[protect] Repaired missing protection for {sym}: "
                            f"SL={float(r['sl'])} TP={float(r['tp'])} qty={qty}")
        if repaired:
            self.ib.sleep(1.0)
            log.warning(f"[protect] Submitted protective orders for {repaired} position(s)")

    def _ok(self, enforce_entry_gates=True):
        if self._halt: return False
        if not enforce_entry_gates:
            return True
        # Count only positions the bot is actively managing (has bracket orders).
        # Raw IBKR positions from previous sessions or manual trades should NOT
        # block new entries — they have no stop/target and the bot isn't managing them.
        managed = self.orders.managed_open_count()
        if managed >= CFG["max_open"]:
            log.debug(f"Max open reached ({managed}/{CFG['max_open']}) — no new entries")
            return False
        heat = self.orders.portfolio_heat_pct(self.orders.account())
        if heat >= CFG.get("max_portfolio_heat_pct", 14):
            log.debug(f"Portfolio heat {heat:.1f}% at cap — no new entries")
            return False
        # Dashboard soft-stop button — blocks new entries, open trades managed normally.
        # Resets automatically at 22:00 in _session_reset().
        if os.path.exists("bot_soft_stop.flag"):
            log.debug("Soft stop active (dashboard) — no new entries until 22:00 or cancelled")
            return False
        # health_monitor.py writes this flag when critical conditions are detected.
        # It only blocks NEW entries — existing position management is unaffected.
        if news_filter.is_event_blackout():
            log.debug("Major macro event blackout (FOMC/CPI/NFP ±45 min) — no new entries")
            return False
        flag = CFG.get("pause_flag_file", "bot_pause.flag")
        if os.path.exists(flag):
            try:
                data   = json.loads(open(flag).read())
                expiry = datetime.fromisoformat(data["expires"])
                reason = data.get("reason", "")
                # Connection/IBKR errors are always transient — clear immediately
                _transient = ("connection", "ib_insync", "TimeoutError", "non-IBKR errors",
                              "API connection", "clientId", "frozen", "scan")
                if any(k.lower() in reason.lower() for k in _transient):
                    os.remove(flag)
                elif datetime.now() < expiry:
                    log.debug(f"Health monitor pause active ({reason[:60]}) — no new entries")
                    return False
                else:
                    os.remove(flag)   # auto-expire
            except Exception:
                os.remove(flag)       # corrupt flag — clear it
        return True

    def _contract(self, sym, market, extra=None):
        if market == "stock_us":  return self.contracts.stock_us(sym)
        if market == "stock_uk":  return self.contracts.stock_uk(sym)
        if market == "forex":     return self.contracts.forex(sym)
        if market == "crypto":    return self.contracts.crypto(sym)
        if market == "index_cfd":    return self.contracts.index_cfd(sym)
        if market == "index_cfd_eu": return self.contracts.index_cfd_eu(sym)
        if market == "future" and extra: return self.contracts.future(sym, extra[1])
        return None

    def _scan_one(self, sym, market, extra=None):
        """Fetch IBKR bars, run regime + signal checks, return signal dict or None."""
        # Check the trade window FIRST, before requesting any historical
        # data. Previously the data fetch happened unconditionally every
        # cycle for every symbol, even when that market's session was
        # closed (e.g. requesting fresh US stock bars at 10:00 SAST, the
        # middle of the US overnight with no fresh trade data) — IBKR's
        # data farm correctly returned "Error 162: HMDS query returned no
        # data" for every such request, which was harmless but noisy and
        # wasteful. Skip the fetch entirely outside the window instead.
        if not in_trade_window(market):
            is_lunch = market == "stock_us" and is_us_lunch_block_now()
            gate_text = "US lunch block" if is_lunch else "outside trade window"
            reg_text = "LUNCH" if is_lunch else "WAIT"
            last_px = 0.0
            if is_lunch and _HAS_DB:
                try:
                    rows = _ss.read_candles(sym, 1)
                    if rows:
                        last_px = float(rows[-1].get("close") or rows[0].get("close") or 0.0)
                except Exception:
                    last_px = 0.0
            if _HAS_DB:
                try:
                    _ss.upsert_signal(sym, market, "waiting", None, last_px, 0.0, 0.0,
                                      0.0, 0.0, 0.0, 0.0,
                                      reg_text, gate_text, False,
                                      {"status": gate_text})
                except Exception:
                    pass
            return {"sym": sym, "market": market, "extra": extra,
                     "contract": None, "dir": None, "price": last_px, "atr": 0,
                     "rsi": 0, "macd_h": 0, "vsurge": 0, "atr_pct": 0,
                     "regime": reg_text, "gate": gate_text,
                     "gate_ok": False, "fractional": False, "_skipped": True}

        if market in ("stock_us", "stock_uk") and news_filter.has_earnings_soon(sym):
            log.info(f"{sym}: earnings blackout — skipping")
            return None

        contract = self._contract(sym, market, extra)
        if contract is None:
            return None

        # choose what_to_show, bar_size, and data quality settings by market type.
        # Stocks use useRTH=True + 3D so VWAP/EMA/MACD are computed on clean
        # regular-session bars only — pre/after-market bars have near-zero volume
        # which corrupts VWAP and makes macd_accel_up/down effectively random.
        data_contract = contract   # default: same contract for data and trading
        use_rth      = False
        duration_str = "1 D"
        if market == "forex":
            what     = CFG["ibkr_fx_show"]
            bar_size = CFG["ibkr_bar_size_forex"]
        elif market == "stock_uk":
            what         = CFG["ibkr_what_to_show"]
            bar_size     = CFG["ibkr_bar_size_stock_uk"]
            use_rth      = True
            duration_str = "7 D"
        elif market == "stock_us":
            bar_size     = CFG["ibkr_bar_size_stock"]
            use_rth      = True
            duration_str = "7 D"
            if CFG.get("use_cfd_us"):
                # IBKR won't serve historical data on CFD contracts — use the
                # underlying Stock for data but keep the CFD contract for trading.
                data_contract = Stock(sym, "SMART", "USD")
                what = CFG["ibkr_what_to_show"]
            else:
                what = CFG["ibkr_what_to_show"]
        elif market == "crypto":
            what     = CFG["ibkr_crypto_show"]
            bar_size = CFG["ibkr_bar_size_crypto"]
        else:
            what     = CFG["ibkr_what_to_show"]
            bar_size = CFG["ibkr_bar_size"]

        df = self.ibdata.fetch(data_contract, what_to_show=what, bar_size=bar_size,
                               use_rth=use_rth, duration_str=duration_str)
        if df is None:
            log.debug(f"{sym}: no data from IBKR")
            if _HAS_DB:
                try:
                    _ss.upsert_signal(sym, market, "no_data", None, 0.0, 0.0, 0.0,
                                      0.0, 0.0, 0.0, 0.0,
                                      "NO DATA", "IBKR returned no bars", False,
                                      {"status": "IBKR returned no bars"})
                except Exception:
                    pass
            return None

        if _HAS_DB:
            try:
                _ss.upsert_candles(sym, df)
            except Exception:
                pass

        reg, atr_pct = regime_of(df, market=market)
        if reg == "CHOP":
            log.debug(f"{sym}: CHOP regime (atr_pct={atr_pct:.4f}) — skipped")
            if _HAS_DB:
                try:
                    _px = float(df["Close"].iloc[-1])
                    _ss.upsert_signal(sym, market, "chop", None, _px, _px * atr_pct,
                                      atr_pct, 0.0, 0.0, 1.0, _px,
                                      "CHOP", "CHOP regime", False, {"regime": "CHOP"},
                                      htf_trend=None, daily_trend=None)
                except Exception:
                    pass
            return None

        frac   = (market == "crypto")
        is_stk = (market in ("stock_us", "stock_uk"))

        # ── ADAPTIVE MULTI-STRATEGY ENGINE (all markets) ─────────────────────
        # All markets now use evaluate_probability() for consistent signal quality.
        # Stocks get 1H trend filter + hard counter-trend guard.
        # Forex gets ORB based on London/Tokyo session opens.
        if is_stk:
            htf_trend  = self.ibdata.get_hourly_trend(data_contract, sym, what_to_show=what)
            daily_trend = self.ibdata.get_daily_trend(data_contract, sym, what_to_show=what)
        else:
            htf_trend   = "FLAT"
            daily_trend = "FLAT"

        # ── Opening Range Breakout direction (Gao et al. 2018) ───────────
        _orb_start, _orb_end = _orb_window_utc(market)
        orb_direction = None
        try:
            _idx = df.index
            if len(_idx) > 0:
                if hasattr(_idx[0], "tzinfo") and _idx[0].tzinfo is not None:
                    _orb_bars = df[(_idx >= _orb_start) & (_idx < _orb_end)]
                else:
                    _orb_bars = df[(_idx >= _orb_start.replace(tzinfo=None)) &
                                   (_idx < _orb_end.replace(tzinfo=None))]
                if len(_orb_bars) >= 2:
                    _orb_high = float(_orb_bars["High"].max())
                    _orb_low  = float(_orb_bars["Low"].min())
                    _cur      = float(df["Close"].iloc[-1])
                    if _cur > _orb_high:
                        orb_direction = "BUY"
                    elif _cur < _orb_low:
                        orb_direction = "SELL"
        except Exception:
            orb_direction = None

        # ── VPIN toxicity check ────────────────────────────────────────────
        _vpin = 0.0
        if "Volume" in df.columns:
            try:
                _vpin = _compute_vpin(
                    df["High"].values[-30:], df["Low"].values[-30:],
                    df["Close"].values[-30:], df["Volume"].values[-30:], window=20
                )
            except Exception:
                _vpin = 0.0
        if _vpin > 0.7:
            log.debug(f"{sym}: VPIN {_vpin:.2f} > 0.7 — elevated toxicity, skip entry")
            if _HAS_DB:
                try:
                    _px = float(df["Close"].iloc[-1])
                    _ss.upsert_signal(sym, market, "scanning", None, _px, _px * atr_pct,
                                      atr_pct, 0.0, 0.0, 1.0, _px,
                                      reg, f"VPIN={_vpin:.2f} (toxic flow)", False,
                                      {"regime": reg, "vpin": round(_vpin, 2)},
                                      htf_trend=htf_trend, daily_trend=daily_trend)
                except Exception:
                    pass
            return None

        # ── VWAP REVERSION (primary) — pure VWAP + Volume strategy ────────
        sig = None
        if CFG.get("vwap_enabled", True):
            try:
                sig = evaluate_vwap(df, market=market)
            except Exception as _e:
                log.debug(f"{sym}: evaluate_vwap error ({_e}) — falling back to probability engine")
                sig = None
            if sig:
                log.info(f"{sym}: VWAP_REV signal {sig['direction']} "
                         f"z={sig['vwap_z']} vol={sig['vsurge']}x score={sig['score']}")

        # ── FALLBACK: multi-strategy probability engine ───────────────────
        if sig is None:
            sig = evaluate_probability(df, htf_trend=htf_trend, orb_direction=orb_direction, sym=sym, market=market)

        if sig is None:
            log.debug(f"{sym}: no signal (VWAP or probability)")
            if _HAS_DB:
                try:
                    _px  = float(df["Close"].iloc[-1])
                    _atr = _px * atr_pct
                    _c   = df["Close"]
                    _rsi_v = float(ta.momentum.RSIIndicator(_c, 9).rsi().dropna().iloc[-1])
                    _mh_s  = ta.trend.MACD(_c, 12, 26, 9).macd_diff().dropna()
                    _mh    = float(_mh_s.iloc[-1]) if len(_mh_s) > 0 else 0.0
                    _adxs  = ta.trend.ADXIndicator(df["High"], df["Low"], _c, 14).adx().dropna()
                    _adx_v = float(_adxs.iloc[-1]) if len(_adxs) > 0 else 0.0
                    _vwap  = float(_c.rolling(20).mean().iloc[-1])
                    _disp_reg = "QUIET" if _adx_v < 12 else reg
                    _ss.upsert_signal(sym, market, "scanning", None, _px, _atr, atr_pct,
                                      round(_rsi_v, 1), round(_mh, 6), 1.0, round(_vwap, 6),
                                      _disp_reg, f"no signal (ADX={_adx_v:.1f} HTF={htf_trend})",
                                      False, {"regime": _disp_reg, "htf": htf_trend,
                                              "adx": round(_adx_v, 1), "rsi": round(_rsi_v, 1)},
                                      htf_trend=htf_trend, daily_trend=daily_trend)
                except Exception:
                    pass
            return None
        if not sig.get("direction"):
            if _HAS_DB:
                try:
                    _ss.upsert_signal(sym, market, sig.get("mode", "no_data"), None,
                                      sig.get("price", float(df["Close"].iloc[-1])), sig.get("atr", 0.0), atr_pct,
                                      sig.get("rsi", 0.0), sig.get("macd_h", 0.0),
                                      sig.get("vsurge", 1.0), sig.get("vwap", sig.get("price", 0.0)),
                                      sig.get("regime", reg),
                                      sig.get("gate_reason", f"no setup (HTF={htf_trend})"), False,
                                      sig.get("conditions", {}),
                                      vwap_z=sig.get("vwap_z"), stoch_k=sig.get("stoch_k"), stoch_d=sig.get("stoch_d"),
                                      htf_trend=htf_trend, daily_trend=daily_trend)
                except Exception:
                    pass
            return None
        sig_mode = sig.get("mode", "standard")

        # Hard counter-trend guard for trend-following strategies only.
        # MEAN_REV and BB_SQUEEZE fade extremes by design — they are direction-agnostic
        # and already require strong internal score (≥70/68) as confirmation.
        _trend_strats = {"MOMENTUM", "PULLBACK"}
        if is_stk and htf_trend != "FLAT" and sig.get("strategy") in _trend_strats:
            htf_required = "UP" if sig["direction"] == "BUY" else "DOWN"
            if htf_trend != htf_required:
                log.debug(
                    f"{sym}: 1H {htf_trend} ≠ {sig['direction']} "
                    f"[{sig['strategy']} score={sig['score']}] — counter-trend blocked"
                )
                if _HAS_DB:
                    try:
                        _ss.upsert_signal(sym, market, sig.get("mode","scanning"),
                                          sig["direction"], sig["price"], sig["atr"], atr_pct,
                                          sig.get("rsi",0), sig.get("macd_h",0),
                                          sig.get("vsurge",1.0), sig.get("vwap",sig["price"]),
                                          sig.get("regime",reg),
                                          f"1H counter-trend (HTF={htf_trend})", False,
                                          sig.get("conditions",{}),
                                          vwap_z=sig.get("vwap_z"),
                                          stoch_k=sig.get("stoch_k"), stoch_d=sig.get("stoch_d"),
                                          htf_trend=htf_trend, daily_trend=daily_trend)
                    except Exception:
                        pass
                return None

        # Daily (3rd timeframe) counter-trend guard — same exempt strategies as 1H.
        if is_stk and daily_trend != "FLAT" and sig.get("strategy") in _trend_strats:
            htf_required = "UP" if sig["direction"] == "BUY" else "DOWN"
            if daily_trend != htf_required:
                log.debug(
                    f"{sym}: daily {daily_trend} ≠ {sig['direction']} "
                    f"[{sig['strategy']} score={sig['score']}] — daily counter-trend blocked"
                )
                if _HAS_DB:
                    try:
                        _ss.upsert_signal(sym, market, sig.get("mode","scanning"),
                                          sig["direction"], sig["price"], sig["atr"], atr_pct,
                                          sig.get("rsi",0), sig.get("macd_h",0),
                                          sig.get("vsurge",1.0), sig.get("vwap",sig["price"]),
                                          sig.get("regime",reg),
                                          f"daily counter-trend (daily={daily_trend})", False,
                                          sig.get("conditions",{}),
                                          vwap_z=sig.get("vwap_z"),
                                          stoch_k=sig.get("stoch_k"), stoch_d=sig.get("stoch_d"),
                                          htf_trend=htf_trend, daily_trend=daily_trend)
                    except Exception:
                        pass
                return None

        # SPY relative strength filter for US stocks — skip if stock moves
        # against its relative strength vs SPY (laggard BUY / leader SELL)
        if market == "stock_us" and sig.get("strategy") != "MEAN_REV":
            spy_ret = getattr(self, "_spy_return", 0.0)
            if df is not None and len(df) >= 2 and abs(spy_ret) > 0.001:
                try:
                    _stk_open  = float(df["Open"].iloc[0])
                    _stk_close = float(df["Close"].iloc[-1])
                    stk_ret = (_stk_close - _stk_open) / _stk_open if _stk_open > 0 else 0.0
                    rel_str = stk_ret - spy_ret
                    if sig["direction"] == "BUY" and rel_str < -0.005:
                        log.debug(f"{sym}: relative strength {rel_str:+.4f} vs SPY — laggard, skip BUY")
                        if _HAS_DB:
                            try:
                                _ss.upsert_signal(sym, market, sig.get("mode","scanning"),
                                                  sig["direction"], sig["price"], sig["atr"], atr_pct,
                                                  sig.get("rsi",0), sig.get("macd_h",0),
                                                  sig.get("vsurge",1.0), sig.get("vwap",sig["price"]),
                                                  sig.get("regime",reg),
                                                  f"SPY laggard ({rel_str:+.4f})", False,
                                                  sig.get("conditions",{}), vwap_z=sig.get("vwap_z"),
                                                  stoch_k=sig.get("stoch_k"), stoch_d=sig.get("stoch_d"),
                                                  htf_trend=htf_trend, daily_trend=daily_trend)
                            except Exception:
                                pass
                        return None
                    if sig["direction"] == "SELL" and rel_str > 0.005:
                        log.debug(f"{sym}: relative strength {rel_str:+.4f} vs SPY — leader, skip SELL")
                        if _HAS_DB:
                            try:
                                _ss.upsert_signal(sym, market, sig.get("mode","scanning"),
                                                  sig["direction"], sig["price"], sig["atr"], atr_pct,
                                                  sig.get("rsi",0), sig.get("macd_h",0),
                                                  sig.get("vsurge",1.0), sig.get("vwap",sig["price"]),
                                                  sig.get("regime",reg),
                                                  f"SPY leader ({rel_str:+.4f})", False,
                                                  sig.get("conditions",{}), vwap_z=sig.get("vwap_z"),
                                                  stoch_k=sig.get("stoch_k"), stoch_d=sig.get("stoch_d"),
                                                  htf_trend=htf_trend, daily_trend=daily_trend)
                            except Exception:
                                pass
                        return None
                except Exception:
                    pass

        # ── Bid-ask spread gate ───────────────────────────────────────────────
        _spread_pct = self.ibdata.get_spread_pct(contract)
        if _spread_pct > 0 and atr_pct > 0:
            _spread_atr_ratio = _spread_pct / atr_pct
            _max_spread_ratio = CFG.get("max_spread_atr_ratio", 0.35)
            if _spread_atr_ratio > _max_spread_ratio:
                log.debug(f"{sym}: spread {_spread_pct:.3%} = {_spread_atr_ratio:.2f}x ATR — too wide")
                if _HAS_DB:
                    try:
                        _ss.upsert_signal(sym, market, sig.get("mode","scanning"),
                                          sig["direction"], sig["price"], sig["atr"], atr_pct,
                                          sig.get("rsi",0), sig.get("macd_h",0),
                                          sig.get("vsurge",1.0), sig.get("vwap",sig["price"]),
                                          sig.get("regime",reg),
                                          f"spread {_spread_pct:.3%} ({_spread_atr_ratio:.2f}x ATR)", False,
                                          sig.get("conditions",{}), vwap_z=sig.get("vwap_z"),
                                          stoch_k=sig.get("stoch_k"), stoch_d=sig.get("stoch_d"),
                                          htf_trend=htf_trend, daily_trend=daily_trend,
                                          spread_pct=_spread_pct)
                    except Exception:
                        pass
                return None

        # ── VIX volatility gate ───────────────────────────────────────────────
        _vix = _get_vix_level()
        if _vix > 35:
            log.debug(f"{sym}: VIX={_vix:.1f} > 35 (extreme) — entry blocked")
            if _HAS_DB:
                try:
                    _ss.upsert_signal(sym, market, sig.get("mode","scanning"),
                                      sig["direction"], sig["price"], sig["atr"], atr_pct,
                                      sig.get("rsi",0), sig.get("macd_h",0),
                                      sig.get("vsurge",1.0), sig.get("vwap",sig["price"]),
                                      sig.get("regime",reg),
                                      f"VIX={_vix:.1f} extreme", False,
                                      sig.get("conditions",{}), vwap_z=sig.get("vwap_z"),
                                      stoch_k=sig.get("stoch_k"), stoch_d=sig.get("stoch_d"),
                                      htf_trend=htf_trend, daily_trend=daily_trend,
                                      spread_pct=_spread_pct, vix_level=_vix)
                except Exception:
                    pass
            return None
        if _vix > 28 and sig.get("strategy") in {"MOMENTUM", "PULLBACK"}:
            log.debug(f"{sym}: VIX={_vix:.1f} > 28 — trend-following blocked (elevated vol)")
            if _HAS_DB:
                try:
                    _ss.upsert_signal(sym, market, sig.get("mode","scanning"),
                                      sig["direction"], sig["price"], sig["atr"], atr_pct,
                                      sig.get("rsi",0), sig.get("macd_h",0),
                                      sig.get("vsurge",1.0), sig.get("vwap",sig["price"]),
                                      sig.get("regime",reg),
                                      f"VIX={_vix:.1f} elevated (MOMENTUM/PULLBACK blocked)", False,
                                      sig.get("conditions",{}), vwap_z=sig.get("vwap_z"),
                                      stoch_k=sig.get("stoch_k"), stoch_d=sig.get("stoch_d"),
                                      htf_trend=htf_trend, daily_trend=daily_trend,
                                      spread_pct=_spread_pct, vix_level=_vix)
                except Exception:
                    pass
            return None

        # ── Sector strength gate (US stocks only) ─────────────────────────────
        _sector_etf = None
        _sector_ret = 0.0
        if market == "stock_us":
            _sector_etf = _SECTOR_MAP.get(sym)
            if _sector_etf:
                _sector_ret = _SECTOR_RETURNS.get(_sector_etf, 0.0)
                _sector_threshold = CFG.get("sector_headwind_pct", 0.005)
                _sector_blocked = ((sig["direction"] == "BUY"  and _sector_ret < -_sector_threshold) or
                                   (sig["direction"] == "SELL" and _sector_ret >  _sector_threshold))
                if _sector_blocked:
                    log.debug(f"{sym}: sector {_sector_etf} {_sector_ret:+.2%} — headwind for {sig['direction']}")
                    if _HAS_DB:
                        try:
                            _ss.upsert_signal(sym, market, sig.get("mode","scanning"),
                                              sig["direction"], sig["price"], sig["atr"], atr_pct,
                                              sig.get("rsi",0), sig.get("macd_h",0),
                                              sig.get("vsurge",1.0), sig.get("vwap",sig["price"]),
                                              sig.get("regime",reg),
                                              f"sector {_sector_etf} {_sector_ret:+.2%} headwind", False,
                                              sig.get("conditions",{}), vwap_z=sig.get("vwap_z"),
                                              stoch_k=sig.get("stoch_k"), stoch_d=sig.get("stoch_d"),
                                              htf_trend=htf_trend, daily_trend=daily_trend,
                                              spread_pct=_spread_pct, vix_level=_vix,
                                              sector_etf=_sector_etf, sector_ret=_sector_ret)
                        except Exception:
                            pass
                    return None

        direction = sig.get("direction")
        if not direction:
            if _HAS_DB:
                try:
                    _ss.upsert_signal(sym, market, sig.get("mode", "no_data"), None,
                                      sig.get("price", 0.0), sig.get("atr", 0.0), atr_pct,
                                      sig.get("rsi", 0.0), sig.get("macd_h", 0.0),
                                      sig.get("vsurge", 1.0), sig.get("vwap", sig.get("price", 0.0)),
                                      sig.get("regime", reg),
                                      sig.get("gate_reason", f"no setup (HTF={htf_trend})"), False,
                                      sig.get("conditions", {}),
                                      vwap_z=sig.get("vwap_z"), stoch_k=sig.get("stoch_k"), stoch_d=sig.get("stoch_d"),
                                      htf_trend=htf_trend, daily_trend=daily_trend)
                except Exception:
                    pass
            return None
        # Long-only override — leveraged ETFs can't be shorted via API
        if sym in CFG.get("long_only_symbols", set()) and direction == "SELL":
            return None

        execution_1m_score = 0
        execution_1m_result = "disabled"
        smc_score = 0
        smc_confirmation = False
        smc_result = "disabled"
        if CFG.get("execution_1m_enabled") or CFG.get("smc_confirmation_enabled"):
            try:
                df_1m = self.ibdata.fetch(
                    data_contract,
                    what_to_show=what,
                    bar_size=CFG.get("ibkr_bar_size_execution", "1 min"),
                    use_rth=is_stk,
                    duration_str="1800 S",
                )
            except Exception:
                df_1m = None
            if CFG.get("execution_1m_enabled"):
                execution_1m_score, execution_1m_result = evaluate_execution_1m(df_1m, direction)
            if CFG.get("smc_confirmation_enabled"):
                smc_score, smc_confirmation, smc_result = evaluate_smc_confirmation(df_1m, direction)

        sig["execution_1m_score"] = execution_1m_score
        sig["execution_1m_result"] = execution_1m_result
        sig["smc_score"] = smc_score
        sig["smc_confirmation"] = smc_confirmation
        sig["smc_result"] = smc_result
        score_details = _score_details_from_signal(sig, market, htf_trend=htf_trend)
        sig.update(score_details)
        sig["conditions"] = {
            **(sig.get("conditions", {}) or {}),
            "1M execution": execution_1m_result == "confirmed",
            "SMC": smc_confirmation,
        }

        ok, why = execution_ok(sig["price"], sig["atr"], market=market)

        # News sentiment gate — skip trades that go against strong news sentiment
        news_gate    = "OK"
        news_gate_ok = True
        if self._news_sentiment:
            nd = self._news_sentiment.get(sym)
            if nd:
                sentiment = nd["sentiment"]
                n_score   = nd["score"]
                if direction == "BUY"  and sentiment == "bearish" and n_score < -0.25:
                    news_gate    = f"bearish news ({n_score:+.2f})"
                    news_gate_ok = False
                elif direction == "SELL" and sentiment == "bullish" and n_score > 0.25:
                    news_gate    = f"bullish news ({n_score:+.2f})"
                    news_gate_ok = False

        gate_final = ok and news_gate_ok
        gate_str   = news_gate if not news_gate_ok else ("OK" if ok else why)

        strategy = sig.get("strategy", sig_mode)
        score    = sig.get("score", 0)
        prob_regime = sig.get("regime", reg)
        nd_info  = self._news_sentiment.get(sym, {})

        log.info(
            f"{sym} SIGNAL {direction} [{strategy} score={score} regime={prob_regime}] "
            f"price={sig['price']:.2f} atr={sig['atr']:.4f} rsi={sig.get('rsi',0):.1f} "
            f"vwap_z={sig.get('vwap_z',0):.2f} stoch_k={sig.get('stoch_k',50):.1f} "
            f"orb={sig.get('conditions',{}).get('ORB','N/A')} 1H={htf_trend}"
        )

        return {
            "sym": sym, "market": market, "extra": extra,
            "contract": contract,
            "dir": direction, "price": sig["price"], "atr": sig["atr"],
            "rsi": sig.get("rsi", 0), "macd_h": sig.get("macd_h", 0),
            "vsurge": sig.get("vsurge", 1.0),
            "vwap":   sig.get("vwap", 0.0),
            "vwap_z": sig.get("vwap_z", 0.0),
            "stoch_k": sig.get("stoch_k", 50.0),
            "stoch_d": sig.get("stoch_d", 50.0),
            "atr_pct": atr_pct, "regime": prob_regime, "sig_mode": sig_mode,
            "strategy": strategy, "score": score,
            "score_band": sig.get("score_band"),
            "score_breakdown": sig.get("score_breakdown", {}),
            "bias_1h_score": sig.get("bias_1h_score"),
            "setup_15m_score": sig.get("setup_15m_score"),
            "trigger_5m_score": sig.get("trigger_5m_score"),
            "execution_1m_score": execution_1m_score,
            "smc_score": smc_score,
            "risk_reward_score": sig.get("risk_reward_score"),
            "session_score": sig.get("session_score"),
            "timeframe_setup": sig.get("timeframe_setup"),
            "execution_1m_result": execution_1m_result,
            "smc_confirmation": smc_confirmation,
            "smc_result": smc_result,
            "gate": gate_str, "gate_ok": gate_final,
            "news_sentiment": nd_info.get("sentiment", "neutral"),
            "news_score":     nd_info.get("score", 0.0),
            "fractional": frac, "is_stock": is_stk,
            "conditions": sig.get("conditions", {"strategy": strategy, "score": score}),
            "vwap_target":    sig.get("vwap_target"),
            "vwap_stop_dist": sig.get("vwap_stop_dist"),
            "htf_trend": htf_trend, "daily_trend": daily_trend,
            "sector_etf": _sector_etf, "sector_ret": _sector_ret,
            "spread_pct": _spread_pct, "vix_level": _vix,
            "comparison_bucket": sig.get("comparison_bucket"),
            "forward_phase": sig.get("forward_phase"),
            "session_name": sig.get("session_name"),
        }

    def _scan_one_index(self, sym, market="index_cfd"):
        """Fetch daily bars and run mean reversion signal for index CFDs (US or EU)."""
        if not in_trade_window(market):
            return {"sym": sym, "market": market, "_skipped": True,
                    "dir": None, "price": 0, "atr": 0, "rsi": 0,
                    "macd_h": 0, "vsurge": 0, "atr_pct": 0, "regime": "N/A",
                    "gate": "outside trade window", "gate_ok": False, "fractional": False}

        # IBKR won't serve historical data on CFD contracts — use underlying ETF
        if market == "index_cfd_eu":
            _EU_DATA = {"IBDE30": "EWG", "IBGB100": "EWU"}
            contract  = self.contracts.index_cfd_eu(sym)
            data_sym  = _EU_DATA.get(sym, sym)
        else:
            _INDEX_ETF = {"IBUS500": "SPY", "IBUST100": "QQQ"}
            contract  = self.contracts.index_cfd(sym)
            data_sym  = _INDEX_ETF.get(sym, sym)

        if contract is None:
            return None

        data_ctr = Stock(data_sym, "SMART", "USD")
        df = self.ibdata.fetch_daily(data_ctr, what_to_show=CFG["ibkr_what_to_show"])
        if df is None:
            return None

        long_only = (market == "index_cfd")  # US indices: buy side only; EU: both directions
        sig = evaluate_signal_index(df, long_only=long_only)
        if sig is None:
            return None

        ok, why = execution_ok(sig["price"], sig["atr"], market=market)
        return {
            "sym": sym, "market": market, "extra": None,
            "contract": contract,
            "dir": sig["direction"], "price": sig["price"], "atr": sig["atr"],
            "rsi": sig["rsi"], "macd_h": sig["macd_h"], "vsurge": sig["vsurge"],
            "atr_pct": sig["atr"] / sig["price"] if sig["price"] > 0 else 0,
            "regime": "TREND", "sig_mode": "mean_reversion",
            "gate": "OK" if ok else why, "gate_ok": ok,
            "fractional": False, "is_stock": False,
        }

    def _run_ibkr_fill_recovery(self):
        """Re-run IBKR reqExecutions() fill recovery — pairs BUY+SELL fills to
        reconstruct closed trades with correct P&L. Called on startup and on demand
        when dashboard writes bot_recover_fills.flag via Sync Trades button.

        Uses a 3-day look-back window via ExecutionFilter so positions opened on a
        previous session/day can be paired with today's closing fills.
        """
        try:
            from ib_insync import ExecutionFilter
            from datetime import timedelta
            ef = ExecutionFilter()
            # Request fills from last 3 days so multi-session trades can be paired.
            # IBKR format: "YYYYMMDD HH:MM:SS"
            since = (datetime.now() - timedelta(days=3)).strftime("%Y%m%d-00:00:00")
            ef.time = since
            fills = self.ib.reqExecutions(ef)
        except Exception:
            # Fallback: no filter (current session only)
            try:
                fills = self.ib.reqExecutions()
            except Exception as _fe:
                log.warning(f"[fill_recovery] reqExecutions failed: {_fe}")
                return

        sym_fills: dict = {}
        for fill in fills:
            ex = fill.execution
            time_str = str(getattr(ex, "time", ""))
            if not time_str:
                continue
            sym = getattr(fill.contract, "symbol", None)
            if not sym:
                continue
            sec_type = getattr(fill.contract, "secType", "STK")
            if sec_type == "CASH":
                sym = sym + getattr(fill.contract, "currency", "")
            mkt = "forex" if sec_type == "CASH" else "stock_us"
            side = "BUY" if ex.side == "BOT" else "SELL"
            sym_fills.setdefault(sym, {"mkt": mkt, "fills": []})["fills"].append({
                "side": side, "qty": float(ex.shares),
                "price": float(ex.avgPrice), "time": str(ex.time),
                "execId": ex.execId,
            })

        recovered_closed = 0
        recovered_open   = 0
        for sym, info in sym_fills.items():
            mkt = info["mkt"]
            fs = sorted(info["fills"], key=lambda f: f["time"])
            open_stack: list = []
            for f in fs:
                if not open_stack:
                    open_stack.append(f)
                elif open_stack[-1]["side"] != f["side"]:
                    entry_f = open_stack.pop()
                    is_long = entry_f["side"] == "BUY"
                    qty = min(entry_f["qty"], f["qty"])
                    direction = "BUY" if is_long else "SELL"
                    pnl = _gross_pnl_usd(entry_f["price"], f["price"], qty, direction, mkt)
                    pnl -= CFG.get("commission_per_trade_usd", 1.5) * 2
                    trade_id = f"ibkr_rec_{sym}_{entry_f['execId']}"
                    if _HAS_DB:
                        try:
                            _ss.close_trade(
                                trade_id,
                                exit_px=f["price"],
                                realized=round(pnl, 4),
                                outcome="win" if pnl > 0 else "loss",
                                closed_at=f["time"],
                                sym=sym, market=mkt,
                                direction="BUY" if is_long else "SELL",
                                qty=qty, entry=entry_f["price"],
                                sl=0, tp=0, atr=0,
                                sig_mode="recovered",
                                opened_at=entry_f["time"],
                            )
                        except Exception as _dbe:
                            log.debug(f"[fill_recovery] close_trade {sym}: {_dbe}")
                    recovered_closed += 1
                else:
                    open_stack.append(f)
            # Do not insert unpaired executions as open dashboard trades. Those
            # fragments are not current bot-managed state and were repeatedly
            # polluting the dashboard after restarts.
        if recovered_closed or recovered_open:
            log.info(f"[fill_recovery] {recovered_closed} closed trades, "
                     f"{recovered_open} open position(s) recovered from IBKR fills")

    def _cycle(self):
        # Write heartbeat before any early-return so the health monitor always
        # knows the bot process is alive. Do not write last_scan here; that field
        # is reserved for completed 14m50s scan cycles so the dashboard does not
        # make heartbeats look like fresh scans.
        if _HAS_DB:
            try:
                _ss.set_status("last_heartbeat", datetime.now().isoformat())
                _ss.set_status("scan_state", "running")
                _ss.set_status("scan_started_at", datetime.now().isoformat())
            except Exception:
                pass

        # Dashboard Sync Trades button writes this flag to trigger on-demand fill recovery
        _recover_flag = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_recover_fills.flag")
        if os.path.exists(_recover_flag):
            try:
                os.remove(_recover_flag)
            except Exception:
                pass
            self._run_ibkr_fill_recovery()

        # Keep scanning/dashboard state fresh even when the account is already
        # over max_open. The max-open gate belongs at order-entry time only.
        if not self._ok(enforce_entry_gates=False): return

        if self.ibdata and self.ibdata.hmds_blocked():
            reason = self.ibdata.hmds_block_reason() or "IBKR historical data temporarily unavailable"
            nxt = datetime.now() + timedelta(seconds=CFG["scan_sec"])
            log.warning(f"HMDS locked — skipping historical-data scan: {reason}. "
                        f"Next retry {nxt.strftime('%H:%M:%S')}")
            if _HAS_DB:
                try:
                    _ss.set_status("hmds_blocked", True)
                    _ss.set_status("hmds_block_reason", reason)
                    _ss.set_status("regime", "HMDS_LOCK")
                    _ss.set_status("mode", self.mode.upper())
                    _ss.set_status("ibkr_connected", True)
                    _ss.set_status("last_heartbeat", datetime.now().isoformat())
                    _ss.set_status("scan_state", "hmds_locked")
                    _ss.set_status("cap_usd", CFG.get("capital_cap_usd", 5000.0))
                    _ss.set_status("strategy_equity", self.orders.account())
                except Exception:
                    pass
            print(f"{Fore.YELLOW}  HMDS historical data locked — {reason}{Style.RESET_ALL}")
            print(f"  {Style.DIM}Next retry {nxt.strftime('%H:%M:%S')}  ·  "
                  f"Ctrl+C to quit{Style.RESET_ALL}\n")
            return
        elif _HAS_DB:
            try:
                _ss.set_status("hmds_blocked", False)
                _ss.set_status("hmds_block_reason", "")
            except Exception:
                pass

        # get overall market regime from SPY — always use Stock contract, not CFD.
        # IBKR does not serve historical data on CFD contracts; use the underlying.
        spy_contract = Stock("SPY", "SMART", "USD")
        spy_df = self.ibdata.fetch(spy_contract, what_to_show=CFG["ibkr_what_to_show"],
                                   bar_size=CFG["ibkr_bar_size_stock"],
                                   use_rth=True, duration_str="7 D")
        spy_data_ok = spy_df is not None
        if not spy_data_ok:
            log.debug("SPY data fetch failed or returned nothing — "
                      "header will show NO DATA instead of a misleading 0.00%")
        reg, atr_pct = regime_of(spy_df) if spy_df is not None else ("CHOP", 0.0)

        # SPY intraday return for relative strength filter
        self._spy_return = 0.0
        if spy_df is not None and len(spy_df) >= 2:
            try:
                _spy_open = float(spy_df["Open"].iloc[0])
                _spy_close = float(spy_df["Close"].iloc[-1])
                if _spy_open > 0:
                    self._spy_return = (_spy_close - _spy_open) / _spy_open
            except Exception:
                pass

        acct = self.orders.account()
        pnl  = self.orders.daily_pnl()
        show_header(acct, pnl, reg, atr_pct, self.mode, self.dry_run,
                    spy_data_ok=spy_data_ok, orders=self.orders)

        # Refresh sector ETF returns once per scan cycle (used in _scan_one sector gate)
        try:
            _fetch_sector_returns()
            log.debug(f"Sector returns: { {k: f'{v:+.2%}' for k,v in _SECTOR_RETURNS.items()} }")
        except Exception:
            pass

        sigs = []
        # SPY CHOP: no longer a hard block — individual symbols can still trend
        # when macro is directionless (sector rotation, earnings, etc).
        # Instead halve position size in CHOP to reduce exposure during uncertainty.
        spy_chop = (reg == "CHOP")
        if spy_chop:
            log.debug("SPY regime CHOP — scanning all symbols at 50% size")

        # SPY direction gate: compute once per scan, used in entry loop below
        spy_bearish = False  # SPY below EMA20 → block new longs
        spy_bullish = False  # SPY above EMA20 → block new shorts
        if spy_df is not None and len(spy_df) >= 20:
            try:
                _spy_ema20 = float(spy_df["Close"].ewm(span=20, adjust=False).mean().iloc[-1])
                _spy_last  = float(spy_df["Close"].iloc[-1])
                spy_bearish = _spy_last < _spy_ema20 * 0.998
                spy_bullish = _spy_last > _spy_ema20 * 1.002
                log.debug(f"SPY EMA20={_spy_ema20:.2f} last={_spy_last:.2f} "
                          f"bearish={spy_bearish} bullish={spy_bullish}")
            except Exception:
                pass

        def _hb():
            """Write a mid-scan heartbeat so dashboard doesn't go stale during long fetches."""
            if _HAS_DB:
                try:
                    _ss.set_status("last_heartbeat", datetime.now().isoformat())
                    _ss.set_status("scan_state", "running")
                except Exception:
                    pass

        for s in CFG["stocks_us"]:
            r = self._scan_one(s, "stock_us")
            if r and not r.get("_skipped"):
                r["spy_chop"] = spy_chop
                sigs.append(r)
            _hb()
        for s in CFG["stocks_uk"]:
            r = self._scan_one(s, "stock_uk")
            if r and not r.get("_skipped"):
                r["spy_chop"] = spy_chop
                sigs.append(r)
            _hb()
        for p in CFG["forex"]:
            r = self._scan_one(p, "forex")
            if r and not r.get("_skipped"): sigs.append(r)
            _hb()
        for s in CFG["crypto"]:
            r = self._scan_one(s, "crypto")
            if r and not r.get("_skipped"): sigs.append(r)
        for f in CFG["futures"]:
            r = self._scan_one(f[0], "future", f)
            if r and not r.get("_skipped"): sigs.append(r)
        for s in CFG.get("indices", []):
            r = self._scan_one_index(s)
            if r and not r.get("_skipped"): sigs.append(r)
        for s in CFG.get("indices_eu", []):
            r = self._scan_one_index(s, market="index_cfd_eu")
            if r and not r.get("_skipped"): sigs.append(r)

        show_signals(sigs)

        # ── log all signals to file for dashboard tracking ────────────────────
        sig_log = []
        try:
            with open("scalper_signals.json") as f:
                sig_log = json.load(f)
        except Exception:
            sig_log = []
        for s in sigs:
            sig_log.append({
                "ts":      str(datetime.now()),
                "sym":     s["sym"],
                "dir":     s["dir"],
                "market":  s["market"],
                "price":   s["price"],
                "atr":     round(s["atr"], 4),
                "atr_pct": round(s["atr_pct"] * 100, 3),
                "rsi":     round(s["rsi"], 1),
                "macd_h":  round(s["macd_h"], 4),
                "vsurge":  round(s["vsurge"], 2),
                "regime":  s["regime"],
                "gate":    s["gate"],
            })
        # keep last 500 signals only
        sig_log = sig_log[-500:]
        try:
            with open("scalper_signals.json", "w") as f:
                json.dump(sig_log, f, indent=2, default=str)
        except Exception:
            pass

        open_pos = self.orders.positions()

        self.orders.tick_bars(open_pos)
        self.orders.tick_symbol_cooldowns()
        # close_profitable_positions() REMOVED — it cancelled bracket orders and
        # closed at the first profitable tick ($0.01–$2 P&L), defeating ATR targets.
        # Bracket orders (TP/SL) placed at entry handle all exits.

        # check if any previously-open trades have closed since last cycle,
        # and tag their outcome (win/loss) in the trade log for real stats
        _pre_close_count = len(self.orders._open_trades)
        self.orders.check_open_trade_outcomes(open_pos)
        _post_close_count = len(self.orders._open_trades)

        # When all tracked positions just closed, re-run fill recovery once so
        # ibkr_rec_* open DB entries get their closing fills paired immediately.
        if _pre_close_count > 0 and _post_close_count == 0:
            try:
                self._run_ibkr_fill_recovery()
            except Exception:
                pass

        # update trailing stops — gather fresh prices for ALL open positions,
        # not just symbols that happened to produce a signal this cycle.
        # (a held position can fail regime/gate checks and still need its
        # trailing stop managed, so we fetch its price directly here.)
        current_prices = {s["sym"]: s["price"] for s in sigs}
        for sym in open_pos:
            if sym in current_prices:
                continue
            trade_meta = self.orders._open_trades.get(sym)
            if trade_meta is None:
                continue  # position from previous session — no metadata, can't trail
            held_market = trade_meta.get("market", "stock_us")
            contract = self._contract(sym, held_market)
            if contract is None:
                continue
            # For CFD positions use the underlying contract for data — IBKR
            # does not serve historical data on CFD contracts directly.
            if held_market == "stock_us" and CFG.get("use_cfd_us"):
                data_contract = Stock(sym, "SMART", "USD")
                what = CFG["ibkr_what_to_show"]
            elif held_market == "index_cfd":
                _INDEX_ETF = {"IBUS500": "SPY", "IBUST100": "QQQ"}
                data_contract = Stock(_INDEX_ETF.get(sym, sym), "SMART", "USD")
                what = CFG["ibkr_what_to_show"]
            elif held_market == "index_cfd_eu":
                _EU_DATA = {"IBDE30": "EWG", "IBGB100": "EWU"}
                data_contract = Stock(_EU_DATA.get(sym, sym), "SMART", "USD")
                what = CFG["ibkr_what_to_show"]
            elif held_market == "forex":
                data_contract = contract
                what = CFG["ibkr_fx_show"]
            elif held_market == "crypto":
                data_contract = contract
                what = CFG["ibkr_crypto_show"]
            else:
                data_contract = contract
                what = CFG["ibkr_what_to_show"]
            bsz  = (CFG["ibkr_bar_size_forex"] if held_market == "forex"
                    else CFG["ibkr_bar_size_stock_uk"] if held_market == "stock_uk"
                    else CFG["ibkr_bar_size_stock"] if held_market == "stock_us"
                    else CFG["ibkr_bar_size_crypto"] if held_market == "crypto"
                    else CFG["ibkr_bar_size_index"] if held_market in ("index_cfd", "index_cfd_eu")
                    else CFG["ibkr_bar_size"])
            if held_market in ("index_cfd", "index_cfd_eu"):
                df = self.ibdata.fetch_daily(data_contract, what_to_show=what)
            else:
                df = self.ibdata.fetch(data_contract, what_to_show=what, bar_size=bsz)
            if df is not None and len(df) > 0:
                current_prices[sym] = float(df["Close"].iloc[-1])
        self.orders.manage_partial_take_profit(current_prices)
        self.orders.manage_pyramid(current_prices)
        self.orders.manage_trailing_stops(current_prices)

        show_open_positions(open_pos, self.orders)
        print(self.orders.status_line())

        allowed, reason = self.orders.can_trade()
        if not allowed:
            print(Fore.YELLOW+f"  {Style.BRIGHT}Not trading:{Style.RESET_ALL} {reason}"
                  +Style.RESET_ALL)
        print()

        # SIGNAL -> ORDER AUDIT TRAIL — every signal that passed the initial
        # gate gets a record of exactly what happened to it next, even if it
        # never reached bracket(). Previously a "continue" at any of these
        # checkpoints silently dropped the signal with zero trace, which is
        # exactly why "167 signals but unclear why so few became trades"
        # was undiagnosable without re-reading code line by line.
        audit_entries = []

        for sig in sigs:
            audit = {"ts": str(datetime.now()), "sym": sig["sym"],
                      "dir": sig["dir"], "market": sig["market"],
                      "gate": sig["gate"], "qty": None, "result": None,
                      "strategy": sig.get("strategy"), "score": sig.get("score", 0),
                      "score_band": sig.get("score_band"), "score_breakdown": sig.get("score_breakdown", {}),
                      "bias_1h_score": sig.get("bias_1h_score"),
                      "setup_15m_score": sig.get("setup_15m_score"),
                      "trigger_5m_score": sig.get("trigger_5m_score"),
                      "execution_1m_score": sig.get("execution_1m_score"),
                      "smc_score": sig.get("smc_score"),
                      "risk_reward_score": sig.get("risk_reward_score"),
                      "session_score": sig.get("session_score"),
                      "timeframe_setup": sig.get("timeframe_setup"),
                      "execution_1m_result": sig.get("execution_1m_result"),
                      "smc_confirmation": sig.get("smc_confirmation"),
                      "spread_signal": sig.get("spread_pct", 0.0),
                      "spread_execution": sig.get("spread_pct", 0.0),
                      "entry_price": sig.get("price"),
                      "stop_price": None,
                      "target_price": None,
                      "comparison_bucket": sig.get("comparison_bucket"),
                      "forward_phase": sig.get("forward_phase"),
                      "session_name": sig.get("session_name")}

            if not allowed:
                audit["result"] = f"loop_halted: {reason}"
                audit_entries.append(audit); break
            if not self._ok():
                audit["result"] = "bot_halted_or_max_open_reached"
                audit_entries.append(audit); break
            if not sig["gate_ok"]:
                audit["result"] = f"gate_blocked: {sig['gate']}"
                audit_entries.append(audit); continue

            # Same-direction cap: max 3 longs OR max 3 shorts at once
            _sig_dir = sig.get("dir")
            if _sig_dir:
                _n_long  = self.orders.managed_open_direction_count("BUY")
                _n_short = self.orders.managed_open_direction_count("SELL")
                if _sig_dir == "BUY" and _n_long >= 3:
                    audit["result"] = f"same_dir_cap ({_n_long} longs)"
                    audit_entries.append(audit); continue
                if _sig_dir == "SELL" and _n_short >= 3:
                    audit["result"] = f"same_dir_cap ({_n_short} shorts)"
                    audit_entries.append(audit); continue

            # SPY direction gate: block longs when SPY below EMA20, block shorts when above
            if sig.get("market") == "stock_us" and sig.get("strategy") not in ("MEAN_REV",):
                if spy_bearish and sig.get("dir") == "BUY":
                    audit["result"] = "spy_bearish_gate"
                    audit_entries.append(audit); continue
                if spy_bullish and sig.get("dir") == "SELL":
                    audit["result"] = "spy_bullish_gate"
                    audit_entries.append(audit); continue

            # Opening range filter: skip first 30 min of US session (13:30–14:00 UTC)
            if sig.get("market") == "stock_us" and sig.get("strategy") in ("MOMENTUM", "PULLBACK"):
                _now_utc  = datetime.utcnow()
                _or_start = _now_utc.replace(hour=13, minute=30, second=0, microsecond=0)
                _or_end   = _now_utc.replace(hour=14, minute=0,  second=0, microsecond=0)
                if _or_start <= _now_utc < _or_end:
                    audit["result"] = "opening_range_filter"
                    audit_entries.append(audit); continue

            allowed, reason = self.orders.can_trade(market=sig["market"])
            if not allowed:
                audit["result"] = f"can_trade_blocked: {reason}"
                audit_entries.append(audit)
                print(Fore.YELLOW+f"  Skipping {sig['sym']}: {reason}\n"+Style.RESET_ALL)
                continue

            # check positions, internal tracker, AND pending IBKR orders
            open_order_syms_scan = set()
            try:
                for o in self.orders.ib.openOrders():
                    c = getattr(o, 'contract', None)
                    if c:
                        s = (c.symbol + c.currency) if getattr(c, 'secType', '') == 'CASH' else c.symbol
                        open_order_syms_scan.add(s)
            except Exception:
                pass
            if sig["sym"] in open_pos or sig["sym"] in self.orders._open_trades or sig["sym"] in open_order_syms_scan:
                audit["result"] = "already_have_open_position"
                audit_entries.append(audit); continue

            sym_ok, sym_reason = self.orders.can_trade_symbol(sig["sym"], direction=sig.get("dir"))
            if not sym_ok:
                audit["result"] = f"symbol_cooldown: {sym_reason}"
                audit_entries.append(audit); continue

            # Sector correlation limit: max 2 open positions in the same sector
            _max_sec = CFG.get("max_same_sector", 2)
            _this_sector = _sector_of(sig["sym"])
            _sector_count = sum(
                1 for s in open_pos if _sector_of(s) == _this_sector
            )
            if _sector_count >= _max_sec:
                audit["result"] = f"sector_cap: {_this_sector} already has {_sector_count} positions"
                log.debug(f"{sig['sym']}: sector {_this_sector!r} at cap ({_sector_count})")
                audit_entries.append(audit); continue

            if sig["market"] in ("stock_us", "stock_uk") and news_filter.has_negative_news(sig["sym"]):
                audit["result"] = "negative_news_filter"
                audit_entries.append(audit); continue

            is_crypto = (sig["market"] == "crypto")
            qty = position_size(sig["price"], sig["atr"], acct, sig["regime"],
                                allow_fractional=sig["fractional"], is_crypto=is_crypto,
                                is_stock=sig.get("is_stock", False), market=sig["market"],
                                prob_score=sig.get("score"),
                                vwap_stop_dist=sig.get("vwap_stop_dist"))
            if qty > 0 and sig.get("spy_chop"):
                qty = max(1, int(qty * 0.5))
                log.debug(f"{sig['sym']}: SPY CHOP — position halved to {qty}")
            audit["qty"] = qty
            if qty <= 0:
                audit["result"] = "qty_rounded_to_zero"
                audit_entries.append(audit); continue

            stop_d = _compute_stop_distance(
                sig["price"], sig["atr"], market=sig["market"],
                vwap_stop_dist=sig.get("vwap_stop_dist"),
                is_crypto=is_crypto,
            )
            audit["stop_price"] = self.orders._round_px(sig["price"] - stop_d if sig["dir"] == "BUY" else sig["price"] + stop_d, sig["market"])
            if sig.get("vwap_target") is not None and sig.get("vwap_stop_dist") is not None:
                audit["target_price"] = self.orders._round_px(sig["vwap_target"], sig["market"])
            else:
                rr_for_target = _rr_for_market(sig["market"], sym=sig["sym"], sig_mode=sig.get("sig_mode"), is_crypto=is_crypto)
                audit["target_price"] = self.orders._round_px(sig["price"] + stop_d * rr_for_target if sig["dir"] == "BUY" else sig["price"] - stop_d * rr_for_target, sig["market"])

            order_result = self.orders.bracket(
                sig["contract"], sig["dir"], sig["price"], sig["atr"], qty,
                sig["sym"], is_crypto=is_crypto, market=sig["market"],
                sig_mode=sig.get("sig_mode", "standard"),
                strategy=sig.get("strategy"), score=sig.get("score", 0),
                vwap_target=sig.get("vwap_target"),
                vwap_stop_dist=sig.get("vwap_stop_dist"),
                signal_meta=sig)
            audit["result"] = "order_placed_success" if order_result else "order_placed_failed"
            audit_entries.append(audit)
            time.sleep(0.3)

        # persist the audit trail to its own file, capped at the last 500
        # entries — separate from scalper_signals.json (raw signal data)
        # and scalper_trades.json (actual order records), this file answers
        # specifically "what happened to each signal and why".
        if audit_entries:
            if _HAS_DB:
                for idx, rec in enumerate(audit_entries):
                    if rec.get("result") == "order_placed_success":
                        continue
                    try:
                        _ss.record_setup_event(
                            event_id=f"{rec['sym']}_{rec['ts']}_{idx}",
                            sym=rec.get("sym"), market=rec.get("market"),
                            direction=rec.get("dir"), strategy=rec.get("strategy"),
                            sig_mode=rec.get("strategy"), timeframe_setup=rec.get("timeframe_setup"),
                            final_score=rec.get("score", 0),
                            score_breakdown=rec.get("score_breakdown", {}),
                            bias_1h_score=rec.get("bias_1h_score"),
                            setup_15m_score=rec.get("setup_15m_score"),
                            trigger_5m_score=rec.get("trigger_5m_score"),
                            execution_1m_score=rec.get("execution_1m_score"),
                            smc_score=rec.get("smc_score"),
                            risk_reward_score=rec.get("risk_reward_score"),
                            session_score=rec.get("session_score"),
                            execution_1m_result=rec.get("execution_1m_result"),
                            smc_confirmation=rec.get("smc_confirmation"),
                            spread_signal=rec.get("spread_signal"),
                            spread_execution=rec.get("spread_execution"),
                            slippage=rec.get("slippage"),
                            commission=CFG.get("commission_per_trade_usd"),
                            entry_price=rec.get("entry_price"),
                            stop_price=rec.get("stop_price"),
                            target_price=rec.get("target_price"),
                            result_r=0.0, result_ccy=0.0,
                            outcome="cancelled" if "blocked" in str(rec.get("result")) or "cooldown" in str(rec.get("result")) or "cap" in str(rec.get("result")) else "rejected",
                            exit_reason=rec.get("result"), status="setup_blocked",
                            trade_date=str(rec.get("ts", ""))[:10],
                        )
                    except Exception as _dbe:
                        log.debug(f"[DB] record_setup_event failed: {_dbe}")
            try:
                with open("scalper_audit.json") as f:
                    full_audit = json.load(f)
            except Exception:
                full_audit = []
            full_audit.extend(audit_entries)
            full_audit = full_audit[-500:]
            try:
                with open("scalper_audit.json", "w") as f:
                    json.dump(full_audit, f, indent=2, default=str)
            except Exception:
                pass

        nxt = datetime.now() + timedelta(seconds=CFG["scan_sec"])
        print(f"{Fore.CYAN}  {'─'*78}{Style.RESET_ALL}")
        print(f"  {Style.DIM}Next scan {nxt.strftime('%H:%M:%S')}  ·  "
              f"Ctrl+C to quit{Style.RESET_ALL}\n")
        log.info(f"Scan complete. {len(sigs)} signals. Next {nxt.strftime('%H:%M:%S')}")

        # ── Write state to dashboard DB ───────────────────────────────────────
        if _HAS_DB:
            try:
                real_equity = self.orders.real_account_equity()
                if real_equity is not None:
                    _ss.set_status("equity", real_equity)
                _ss.set_status("strategy_equity", acct)
                _ss.set_status("daily_pnl", pnl)
                _ss.set_status("regime",           reg)
                _ss.set_status("mode",             self.mode.upper())
                _ss.set_status("halt",             self._halt)
                _ss.set_status("last_scan",        datetime.now().isoformat())
                _ss.set_status("last_scan_completed", datetime.now().isoformat())
                _ss.set_status("last_heartbeat",   datetime.now().isoformat())
                _ss.set_status("next_scan_due",    nxt.isoformat())
                _ss.set_status("scan_state",       "waiting")
                _ss.set_status("ibkr_connected",   True)
                _ss.set_status("portfolio_heat",   round(self.orders.portfolio_heat_pct(acct), 1))
                _ss.set_status("portfolio_heat_cap", CFG.get("max_portfolio_heat_pct", 14))
                _ss.set_status("cap_usd",          CFG.get("capital_cap_usd", 8000.0))

                # Drawdown status — lets dashboard show WHY bot isn't taking trades
                peak = self.orders._peak_equity
                dd_pct = round((peak - acct) / peak * 100, 1) if peak > 0 else 0.0
                _ss.set_status("drawdown_pct",    dd_pct)
                _ss.set_status("peak_equity",     peak)
                _ss.set_status("drawdown_halted", self.orders._drawdown_locked)

                for s in sigs:
                    _ss.upsert_signal(
                        sym=s["sym"], market=s["market"],
                        sig_mode=s.get("sig_mode", "standard"),
                        direction=s["dir"], price=s["price"],
                        atr=s["atr"], atr_pct=s["atr_pct"],
                        rsi=s["rsi"], macd_h=s["macd_h"],
                        vsurge=s["vsurge"], vwap=s.get("vwap", 0.0),
                        regime=s["regime"], gate=s["gate"],
                        gate_ok=s["gate_ok"],
                        conditions=s.get("conditions",
                                         {"strategy": s.get("strategy", ""),
                                          "score": s.get("score", 0)}),
                        vwap_z=s.get("vwap_z"),
                        stoch_k=s.get("stoch_k"),
                        stoch_d=s.get("stoch_d"),
                        htf_trend=s.get("htf_trend"),
                        daily_trend=s.get("daily_trend"),
                        sector_etf=s.get("sector_etf"),
                        sector_ret=s.get("sector_ret"),
                        spread_pct=s.get("spread_pct"),
                        vix_level=s.get("vix_level"),
                        score=s.get("score"),
                        strategy=s.get("strategy"),
                        score_breakdown=s.get("score_breakdown"),
                        bias_1h_score=s.get("bias_1h_score"),
                        setup_15m_score=s.get("setup_15m_score"),
                        trigger_5m_score=s.get("trigger_5m_score"),
                        execution_1m_score=s.get("execution_1m_score"),
                        smc_score=s.get("smc_score"),
                        risk_reward_score=s.get("risk_reward_score"),
                        session_score=s.get("session_score"),
                        execution_1m_result=s.get("execution_1m_result"),
                        smc_confirmation=s.get("smc_confirmation"),
                    )

                # Dashboard visibility follows broker truth. _open_trades still
                # controls bot-managed SL/TP, but real IBKR paper positions must
                # stay visible even when they were opened before this process.
                active_syms = set(self.orders._open_trades.keys())
                broker_syms = set()

                for sym, tr in self.orders._open_trades.items():
                    px       = current_prices.get(sym, tr.get("entry", 0) or 0)
                    entry    = tr.get("entry", 0) or 0
                    direction= tr.get("dir", "BUY")
                    qty      = tr.get("qty", 0) or 0
                    sl       = tr.get("current_stop") or tr.get("stop") or 0
                    tp       = tr.get("initial_tp", 0) or 0
                    atr      = tr.get("atr", 0) or 0
                    market   = tr.get("market", "stock_us")
                    unreal   = ((px - entry) * qty if direction == "BUY"
                                else (entry - px) * qty)
                    _ss.upsert_position(sym, market, direction, qty,
                                        entry, px, sl, tp, atr, unreal)

                # Preserve broker-truth positions even when they were opened
                # outside this process. This keeps the dashboard aligned with
                # live IBKR/TWS exposure instead of only bot-managed state.
                try:
                    for pos in self.ib.positions():
                        contract = pos.contract
                        qty = float(pos.position or 0)
                        if abs(qty) < 0.001:
                            continue
                        if getattr(contract, "secType", "") == "CASH":
                            sym = contract.symbol + contract.currency
                            market = "forex"
                        else:
                            sym = contract.symbol
                            market = "stock_uk" if getattr(contract, "currency", "") == "GBP" else "stock_us"
                        broker_syms.add(sym)
                        if sym in active_syms:
                            continue
                        direction = "BUY" if qty > 0 else "SELL"
                        abs_qty = abs(qty)
                        entry = float(getattr(pos, "avgCost", 0) or 0)
                        px = current_prices.get(sym, entry) or entry
                        unreal = ((px - entry) * abs_qty if direction == "BUY"
                                  else (entry - px) * abs_qty)
                        _ss.upsert_position(sym, market, direction, abs_qty,
                                            entry, px, 0, 0, 0, unreal)
                    if broker_syms:
                        log.debug(f"[reconcile] broker-truth positions visible: {sorted(broker_syms)}")
                except Exception as _ibpe:
                    log.debug(f"[reconcile] broker position sync skipped: {_ibpe}")

                # Reconcile: remove DB positions that neither our tracker nor IBKR knows about.
                # Use isConnected() as the guard — if IBKR is live and returns 0 positions,
                # that's valid: clean up any stale DB rows. Only skip cleanup if IBKR is
                # not connected at all (race condition during reconnect at startup).
                _ibkr_ok = self.ib.isConnected()
                visible_syms = active_syms | broker_syms
                if visible_syms or _ibkr_ok:
                    for p in _ss.read_positions():
                        if p["sym"] not in visible_syms:
                            log.info(f"[reconcile] removing stale DB position: {p['sym']}")
                            _ss.remove_position(p["sym"])
            except Exception as _dbe:
                log.error(f"[dashboard] db write error: {_dbe}")

    def _send_daily_summary(self):
        try:
            today = _now_trade_tz().date().isoformat()
            trades = [t for t in self.orders._log
                      if (t.get("trade_date") or _trade_date_from_value(t.get("exit_ts"), fallback=t.get("ts"))) == today
                      and t.get("outcome") in ("win", "loss")]
            wins   = [t for t in trades if t.get("outcome") == "win"]
            losses = [t for t in trades if t.get("outcome") == "loss"]
            pnl    = sum(t.get("pnl_usd") or 0 for t in trades)
            best   = max((t.get("pnl_usd") or 0 for t in trades), default=0)
            worst  = min((t.get("pnl_usd") or 0 for t in trades), default=0)
            markets = {}
            for t in trades:
                m = t.get("market", "unknown")
                markets[m] = markets.get(m, 0) + 1
            mkt_str = "  ".join(f"{m}:{n}" for m, n in markets.items()) or "none"
            notify(
                f"📋 DAILY SUMMARY\n"
                f"Date: {today}\n"
                f"Trades: {len(trades)}  ✅{len(wins)} / ❌{len(losses)}\n"
                f"P&L: ${pnl:+.2f}\n"
                f"Best: ${best:+.2f}  Worst: ${worst:+.2f}\n"
                f"Markets: {mkt_str}"
            )
            log.info(f"Daily summary sent — {len(trades)} trades, P&L ${pnl:+.2f}")
        except Exception as e:
            log.error(f"Daily summary failed: {e}")

    def _friday_soft_close(self):
        """Called at 21:00 SAST on Fridays — close all positions while NYSE is still liquid (15:00 ET)."""
        if datetime.now().weekday() != 4:
            return
        open_pos = self.orders.positions()
        if not open_pos:
            log.info("Friday 21:00 — no open positions, nothing to close.")
            return
        syms = ", ".join(open_pos)
        log.info(f"Friday 21:00 soft close — requesting close of {len(open_pos)} position(s): {syms}")
        notify(f"🔔 FRIDAY CLOSE 21:00\nClosing {len(open_pos)} open position(s) before weekend:\n{syms}")
        self.orders.close_all_positions()
        self._halt = True  # block any new entries until session reset

    def _session_reset(self):
        """Called at 22:00 SAST — resets P&L, unlocks trading, clears halt flag.
        On Fridays also hard-closes any positions still open after the 21:00 soft close."""
        if datetime.now().weekday() == 4:  # Friday
            open_pos = self.orders.positions()
            if open_pos:
                syms = ", ".join(open_pos)
                log.warning(f"Friday 22:00 hard close — forcing {len(open_pos)} remaining position(s): {syms}")
                notify(f"🔴 FRIDAY HARD CLOSE 22:00\nForce-closing {len(open_pos)} remaining position(s) for weekend:\n{syms}")
                self.orders.close_all_positions()
            else:
                log.info("Friday 22:00 — all positions already closed, weekend clear.")
        self._send_daily_summary()
        self.orders.reset_session_pnl()
        self._halt = False
        self._had_open_positions = False
        self.orders._stop_loss_direction_block.clear()
        # Auto-clear dashboard soft-stop at session reset so Monday starts fresh
        if os.path.exists("bot_soft_stop.flag"):
            os.remove("bot_soft_stop.flag")
            log.info("Soft stop flag cleared at 22:00 session reset")

    def _reconnect(self) -> bool:
        """Re-establish IBKR connection after a confirmed drop (called after 30s debounce)."""
        port = CFG["live_port"] if self.args.live else CFG["paper_port"]
        if self.args.port: port = self.args.port
        client_id = CFG["client_id"]
        log.warning("IBKR disconnected — attempting reconnect...")
        # Explicitly disconnect first to release the client ID on Gateway side.
        try:
            self.ib.disconnect()
            time.sleep(3)
        except Exception:
            pass
        try:
            self.ib.connect(CFG["host"], port, clientId=client_id,
                            readonly=False, timeout=20)
            log.info(f"Reconnected (client_id={client_id})")
            if _HAS_DB:
                try:
                    _ss.set_status("ibkr_connected", True)
                except Exception:
                    pass
            # Reconcile open trades against IBKR truth
            self.ib.sleep(2)
            actual = set(self.orders.positions())
            stale = [s for s in self.orders._open_trades if s not in actual]
            if stale:
                log.info(f"Clearing {len(stale)} phantom positions not in IBKR: {stale}")
                for s in stale:
                    self.orders._open_trades.pop(s, None)
                    if _HAS_DB:
                        try:
                            _ss.remove_position(s)
                        except Exception:
                            pass
            notify("ScalpBot RECONNECTED to IBKR")
            try:
                open(".last_wa_startup", "w").write(datetime.now().isoformat())
            except Exception:
                pass
            return True
        except Exception as e:
            log.warning(f"Reconnect failed on fixed client_id={client_id}: {e}")
            log.error("Reconnect failed — will retry in 60s")
            return False

    def _real_account_summary(self) -> dict:
        """Return full IBKR account summary dict (all tags)."""
        result = {}
        try:
            for x in self.ib.accountSummary():
                result[x.tag] = x.value
        except Exception:
            pass
        return result

    def _fetch_session_news(self, session: str = "US"):
        """
        Fetch news for all instruments via IBKR (primary) and Yahoo Finance (fallback).
        Stores results in dashboard DB and sends WhatsApp summary.
        session: "UK" | "US" | "STARTUP"
        """
        if not _HAS_NEWS:
            return
        log.info(f"Fetching session news ({session})...")
        try:
            all_syms = CFG["stocks_us"] + CFG.get("stocks_uk", [])
            qualified = {}
            for sym in CFG["stocks_us"]:
                try:
                    c = self.contracts.stock_us(sym)
                    if c and getattr(c, "conId", None):
                        qualified[sym] = c
                except Exception:
                    pass
            for sym in CFG.get("stocks_uk", []):
                try:
                    c = self.contracts.stock_uk(sym)
                    if c and getattr(c, "conId", None):
                        qualified[sym] = c
                except Exception:
                    pass

            sentiment = _news_fetcher.fetch_all(self.ib, qualified, all_syms)

            if _HAS_DB and sentiment:
                for sym, data in sentiment.items():
                    try:
                        _ss.upsert_news(sym, data["sentiment"], data["score"],
                                        data.get("headlines", []))
                    except Exception:
                        pass

            if sentiment:
                msg = _news_fetcher.whatsapp_summary(sentiment, session)
                notify(msg)

            self._news_sentiment = sentiment
            log.info(f"Session news ready: {len(sentiment)} symbols analysed")
        except Exception as e:
            log.warning(f"News fetch failed: {e}")
            self._news_sentiment = {}

    def run(self):
        # Preserve soft-stop across restarts — if the user pressed Stop on the
        # dashboard it must survive a LaunchAgent code-push restart. Only the
        # 22:00 session reset or an explicit Resume click should clear it.
        if os.path.exists("bot_soft_stop.flag"):
            log.info("Startup: soft-stop flag is set — new entries blocked until Resume is clicked.")

        # Health-monitor pause flags ARE cleared on restart — they're transient
        # (FOMC/CPI windows, IBKR errors) and should not block after a restart.
        pause_flag = CFG.get("pause_flag_file", "bot_pause.flag")
        if os.path.exists(pause_flag):
            try:
                os.remove(pause_flag)
                log.info(f"Startup: cleared transient health-monitor pause flag.")
            except Exception:
                pass

        if _HAS_DB:
            _ss.set_status("ibkr_connected", False)
            _ss.set_status("regime", "—")   # clear any stale value from a previous run
            # Write a heartbeat NOW so the dashboard shows the bot process is alive
            # even if IBKR Gateway is down and connect() blocks in its retry loop.
            _ss.set_status("last_heartbeat", datetime.now().isoformat())
        self.connect()
        log.info(f"ScalpBot v4.0 | {self.mode.upper()} | dry={self.dry_run} | "
                 f"risk={CFG['risk_pct']}% | cap=${CFG['capital_cap_usd']:.0f} | "
                 f"DATA=IBKR")
        # Startup notification — throttled to once per 30 min so rapid LaunchAgent
        # restarts (e.g. during a code-push cycle) don't flood CallMeBot and
        # trigger rate-limit blocks on the API key.
        _wa_ts_file = ".last_wa_startup"
        _send_startup_wa = True
        try:
            if os.path.exists(_wa_ts_file):
                age_mins = (datetime.now() - datetime.fromisoformat(
                    open(_wa_ts_file).read().strip())).total_seconds() / 60
                if age_mins < 30:
                    _send_startup_wa = False
        except Exception:
            pass
        if _send_startup_wa:
            fx_pairs = CFG.get("forex", [])
            notify(f"ScalpBot STARTED\n"
                   f"Mode: {self.mode.upper()}  Cap: ${CFG['capital_cap_usd']:.0f}\n"
                   f"Watching {len(CFG['stocks_us'])} US / {len(CFG['stocks_uk'])} UK"
                   + (f" / {' '.join(fx_pairs)} FX" if fx_pairs else ""))
            try:
                open(_wa_ts_file, "w").write(datetime.now().isoformat())
            except Exception:
                pass
        # zero out P&L immediately on startup so prior-day carry-over doesn't
        # trigger the loss limit before the first trade of the session
        self.orders.reset_session_pnl()
        # Do not call reqGlobalCancel() on startup. It cancels protective
        # stop-loss/take-profit children and leaves real positions unprotected.
        # wait for IBKR to push the initial positions snapshot before the first
        # scan — without this, positions() returns [] and the "already open"
        # guard fails, causing duplicate bracket orders on every restart
        self.ib.reqPositions()
        self.ib.sleep(6.0)  # wait for IBKR to push full positions snapshot before first scan
        if _HAS_DB:
            self._run_ibkr_fill_recovery()
        self._fetch_session_news("STARTUP")
        next_cycle_due = datetime.now()
        if _HAS_DB:
            try:
                last_scan_raw = _ss.read_status("last_scan")
                if last_scan_raw:
                    last_scan_dt = datetime.fromisoformat(str(last_scan_raw))
                    elapsed = (datetime.now() - last_scan_dt).total_seconds()
                    if 0 <= elapsed < CFG["scan_sec"]:
                        next_cycle_due = last_scan_dt + timedelta(seconds=CFG["scan_sec"])
                        _ss.set_status("next_scan_due", next_cycle_due.isoformat())
                        _ss.set_status("scan_state", "waiting")
                        log.info(f"Startup scan deferred until {next_cycle_due.strftime('%H:%M:%S')} "
                                 f"to preserve {CFG['scan_sec']}s cadence")
            except Exception as _cadence_exc:
                log.debug(f"startup cadence check failed: {_cadence_exc}")
        schedule.every().day.at("10:00").do(lambda: self._fetch_session_news("UK"))
        schedule.every().day.at("15:20").do(lambda: self._fetch_session_news("US"))
        schedule.every().day.at("22:00").do(self._session_reset)
        schedule.every().friday.at("21:00").do(self._friday_soft_close)
        _last_hb = 0.0  # track when we last wrote a process-alive heartbeat
        try:
            while True:
                try:
                    schedule.run_pending()
                    now_dt = datetime.now()
                    if now_dt >= next_cycle_due:
                        self._cycle()
                        next_cycle_due = datetime.now() + timedelta(seconds=CFG["scan_sec"])
                    self.ib.sleep(1)
                    # Write a process-alive heartbeat independent of scan cadence.
                    # _cycle() writes its own per-symbol heartbeats, but if _cycle()
                    # hasn't run yet (first minute after start) or is between runs,
                    # this keeps the dashboard from showing "offline".
                    now_ts = time.time()
                    if _HAS_DB and now_ts - _last_hb >= CFG["heartbeat_sec"]:
                        try:
                            _ss.set_status("last_heartbeat", datetime.now().isoformat())
                            _last_hb = now_ts
                        except Exception:
                            pass
                    if not self.ib.isConnected():
                        if self._disconnect_ts is None:
                            # First time we notice disconnect — start the reconnect debounce timer
                            self._disconnect_ts = now_ts
                            if _HAS_DB:
                                _ss.set_status("ibkr_connected", False)
                            log.warning(f"IBKR connection lost — will reconnect in {CFG['disconnect_debounce_sec']}s if still down")
                        elif now_ts - self._disconnect_ts >= CFG["disconnect_debounce_sec"]:
                            # Confirmed disconnect — attempt reconnect then reset timer
                            self._disconnect_ts = None
                            self._reconnect()
                    else:
                        self._disconnect_ts = None  # back online — reset debounce
                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    log.error(f"Main loop error (bot continuing): {e}", exc_info=True)
                    time.sleep(5)
        except KeyboardInterrupt:
            log.info("Stopped.")
            self.orders.close_all_positions()
            self._send_daily_summary()
        finally:
            if _HAS_DB:
                _ss.set_status("ibkr_connected", False)
            self.ib.disconnect()
            log.info("Disconnected.")


# ══════════════════════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════════════════════

def cli():
    p = argparse.ArgumentParser(description="IBKR Scalping Bot v4.0")
    p.add_argument("--live",     action="store_true")
    p.add_argument("--dry-run",  action="store_true")
    p.add_argument("--backtest", action="store_true")
    p.add_argument("--port",     type=int,   default=None)
    p.add_argument("--risk",     type=float, default=None)
    return p.parse_args()


if __name__ == "__main__":
    args = cli()
    if args.risk:
        CFG["risk_pct"] = args.risk
    if args.backtest:
        run_backtest()
        sys.exit(0)
    if args.live and not args.dry_run:
        print(Fore.RED+"""
  ====================================================
   LIVE MODE - REAL MONEY  -  Ctrl+C to abort
  ====================================================
"""+Style.RESET_ALL)
        try:
            time.sleep(5)
        except KeyboardInterrupt:
            print("Aborted."); sys.exit(0)
    ScalpBot(args).run()
