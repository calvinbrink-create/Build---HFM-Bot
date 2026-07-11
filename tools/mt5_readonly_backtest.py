#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import types
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd


APP_DIR = Path(__file__).resolve().parents[1]
SAST = ZoneInfo("Africa/Johannesburg")
EU_INDEX_SYMBOLS = {"GER40", "DE40", "DAX40", "FRA40", "EU50", "UK100", "AUS200"}
GROUP_MARKET = {
    "forex": "forex",
    "metals": "metal",
    "energies": "energy",
    "crypto": "crypto",
    "indices": "index_cfd",
    "stocks": "stock_us",
    "etfs": "stock_us",
}


@dataclass
class Trade:
    symbol: str
    market: str
    direction: str
    entry_time: datetime
    exit_time: datetime
    entry: float
    exit: float
    sl: float
    tp: float
    r_multiple: float
    pnl_price: float
    reason: str
    strategy: str
    score: float
    spread_r: float
    session: str


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def install_readonly_state_store_stub() -> None:
    dashboard = types.ModuleType("dashboard")
    backend = types.ModuleType("dashboard.backend")

    class StateStoreStub(types.ModuleType):
        def __getattr__(self, name: str):
            def _noop(*args, **kwargs):
                if name.startswith("read_"):
                    return []
                if name.startswith("find_") or name.startswith("get_"):
                    return None
                return None

            return _noop

    state_store = StateStoreStub("dashboard.backend.state_store")
    backend.state_store = state_store
    dashboard.backend = backend
    sys.modules.setdefault("dashboard", dashboard)
    sys.modules.setdefault("dashboard.backend", backend)
    sys.modules.setdefault("dashboard.backend.state_store", state_store)


def import_strategy_modules():
    if str(APP_DIR) not in sys.path:
        sys.path.insert(0, str(APP_DIR))
    install_readonly_state_store_stub()
    from mt5_xm_config import MT5RuntimeConfig
    from mt5_news_archive import events_in_window
    from scalping_bot_v4 import CFG, _compute_stop_distance, _rr_for_market, evaluate_probability, execution_ok

    return MT5RuntimeConfig, events_in_window, CFG, _compute_stop_distance, _rr_for_market, evaluate_probability, execution_ok


def env_float(name: str, default: float, lo: float | None = None, hi: float | None = None) -> float:
    try:
        value = float(os.getenv(name, str(default)) or default)
    except Exception:
        value = float(default)
    if lo is not None:
        value = max(lo, value)
    if hi is not None:
        value = min(hi, value)
    return value


def env_int(name: str, default: int, lo: int | None = None, hi: int | None = None) -> int:
    try:
        value = int(float(os.getenv(name, str(default)) or default))
    except Exception:
        value = int(default)
    if lo is not None:
        value = max(lo, value)
    if hi is not None:
        value = min(hi, value)
    return value


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def canonical_market(runtime, canonical: str) -> str:
    group = runtime.group_for_symbol(canonical)
    if group == "indices" and canonical.upper() in EU_INDEX_SYMBOLS:
        return "index_cfd_eu"
    return GROUP_MARKET.get(group, "forex")


def env_float_for_symbol(prefix: str, canonical: str, market: str, default: float) -> float:
    clean = canonical.upper().replace(".", "_")
    if os.getenv(f"{prefix}_{clean}") is not None:
        return env_float(f"{prefix}_{clean}", default)
    if canonical.upper().startswith(("XAU", "XAG", "XPT", "XPD")) and os.getenv(f"{prefix}_METALS") is not None:
        return env_float(f"{prefix}_METALS", default)
    suffix = {
        "metal": "METALS",
        "energy": "ENERGIES",
        "index_cfd": "INDICES",
        "index_cfd_eu": "INDICES",
        "stock_us": "STOCKS",
        "crypto": "CRYPTO",
        "forex": "FOREX",
    }.get(market)
    if suffix and os.getenv(f"{prefix}_{suffix}") is not None:
        return env_float(f"{prefix}_{suffix}", default)
    return env_float(prefix, default) if os.getenv(prefix) is not None else float(default)


def min_score_for(canonical: str, market: str, strategy: str, priority: bool) -> float:
    score = env_float_for_symbol("MT5_MIN_SCORE", canonical, market, env_float("MT5_MIN_SCORE", 82.0, 0.0, 100.0))
    if market in {"index_cfd", "index_cfd_eu"} and os.getenv("MT5_INDEX_MIN_SCORE") is not None:
        score = max(score, env_float("MT5_INDEX_MIN_SCORE", score, 0.0, 100.0))
    if market == "stock_us" and os.getenv("MT5_STOCK_MIN_SCORE") is not None:
        score = max(score, env_float("MT5_STOCK_MIN_SCORE", score, 0.0, 100.0))
    if market in {"metal", "energy", "crypto"}:
        score = max(score, env_float_for_symbol("MT5_ALT_MARKET_MIN_SCORE", canonical, market, score))
    if strategy == "PULLBACK":
        score = max(score, env_float_for_symbol("MT5_PULLBACK_MIN_SCORE", canonical, market, score))
    if strategy == "MEAN_REV":
        score = max(score, env_float_for_symbol("MT5_MEAN_REV_MIN_SCORE", canonical, market, score))
    if priority:
        score = max(score, env_float_for_symbol("MT5_PRIORITY_MIN_SCORE", canonical, market, score))
    return max(0.0, min(100.0, score))


def priority_symbols() -> set[str]:
    raw = os.getenv("MT5_PRIORITY_SYMBOLS", "")
    return {item.strip().upper() for item in raw.replace(";", ",").split(",") if item.strip()}


def max_daily_trades_for_symbol(symbol: str, priority: bool) -> int:
    default = env_int("MT5_MAX_DAILY_TRADES_PER_SYMBOL", 0, lo=0)
    if priority:
        default = env_int("MT5_PRIORITY_MAX_DAILY_TRADES_PER_SYMBOL", default, lo=0)
    clean = symbol.upper().replace(".", "_")
    return env_int(f"MT5_MAX_DAILY_TRADES_PER_SYMBOL_{clean}", default, lo=0)


def load_rates(runtime, canonical: str, timeframe: str, days: int | None, max_bars: int | None) -> pd.DataFrame:
    resolved = runtime.resolved_symbol(canonical)
    path = runtime.bridge_dir / f"rates_{resolved}_{timeframe}.csv"
    if not path.exists():
        raise FileNotFoundError(f"missing bridge history file: {path}")
    df = pd.read_csv(path)
    if df.empty:
        raise ValueError(f"empty bridge history file: {path}")
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.rename(
        columns={
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "volume": "Volume",
            "spread_points": "SpreadPoints",
            "spread_price": "Spread",
            "spread": "SpreadPoints",
        }
    )
    cols = ["Open", "High", "Low", "Close", "Volume"]
    for optional in ("SpreadPoints", "Spread"):
        if optional in df.columns:
            df[optional] = pd.to_numeric(df[optional], errors="coerce").fillna(0.0)
            cols.append(optional)
    df = df.set_index("time")[cols].sort_index()
    if days:
        cutoff = df.index.max() - pd.Timedelta(days=days)
        df = df[df.index >= cutoff]
    if max_bars:
        df = df.tail(max_bars)
    return df


def atr_series(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["Close"].shift(1)
    tr = pd.concat(
        [
            df["High"] - df["Low"],
            (df["High"] - prev_close).abs(),
            (df["Low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.rolling(period).mean()


def minutes_since_midnight(dt: datetime) -> int:
    local = dt.astimezone(SAST)
    return local.hour * 60 + local.minute


def window_active(now: datetime, windows: list[tuple[int, int, int, int]]) -> bool:
    mins = minutes_since_midnight(now)
    for sh, sm, eh, em in windows:
        start = sh * 60 + sm
        end = eh * 60 + em
        if start <= end and start <= mins <= end:
            return True
        if start > end and (mins >= start or mins <= end):
            return True
    return False


def market_window_open(CFG: dict, market: str, now: datetime) -> bool:
    if market == "crypto" and CFG.get("allow_forex_crypto_anytime"):
        return True
    if market == "forex":
        return window_active(now, CFG.get("trade_windows_forex_sast", CFG["trade_windows_us_sast"]))
    if market == "stock_uk":
        return window_active(now, CFG["trade_windows_uk_sast"])
    if market == "index_cfd_eu":
        return window_active(now, CFG.get("trade_windows_eu_index_sast", []))
    if market == "index_cfd":
        return window_active(now, CFG.get("trade_windows_index_us_sast", CFG["trade_windows_us_sast"]))
    return window_active(now, CFG["trade_windows_us_sast"])


def session_label(now: datetime) -> str:
    hour = now.astimezone(SAST).hour
    if 8 <= hour < 12:
        return "europe_open"
    if 12 <= hour < 15:
        return "midday"
    if 15 <= hour < 22:
        return "us_session"
    return "off_hours"


def trend_invalidated(df: pd.DataFrame, direction: str) -> bool:
    completed = df.iloc[:-1] if len(df) > 1 else df
    close = pd.to_numeric(completed["Close"], errors="coerce").dropna()
    if len(close) < 52:
        return False
    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    last_two = close.tail(2)
    if direction == "BUY":
        return bool((last_two < ema20.tail(2)).all() and float(ema20.iloc[-1]) < float(ema50.iloc[-1]))
    return bool((last_two > ema20.tail(2)).all() and float(ema20.iloc[-1]) > float(ema50.iloc[-1]))


def exit_trade(
    df: pd.DataFrame,
    start_i: int,
    symbol: str,
    market: str,
    direction: str,
    entry: float,
    sl: float,
    tp: float,
    strategy: str,
    score: float,
    spread_r: float,
    CFG: dict,
    events_in_window,
) -> Trade:
    entry_time = df.index[start_i].to_pydatetime()
    risk = abs(entry - sl)
    max_hold = env_int("MT5_MAX_HOLD_MINUTES", 30, lo=1) if env_bool("MT5_MAX_HOLD_ENABLE", True) else 10**9
    trend_min_age = env_int("MT5_TREND_MONITOR_MIN_AGE_MINUTES", 5, lo=0)
    for j in range(start_i + 1, len(df)):
        row = df.iloc[j]
        now = df.index[j].to_pydatetime()
        open_px = float(row["Open"])
        age_minutes = (now - entry_time).total_seconds() / 60.0
        if env_bool("MT5_SESSION_MONITOR_ENABLE", True) and not market_window_open(CFG, market, now):
            return build_trade(symbol, market, direction, entry_time, now, entry, open_px, sl, tp, "session_closed", strategy, score, spread_r)
        if env_bool("MT5_NEWS_MONITOR_ENABLE", True) and events_in_window(now):
            return build_trade(symbol, market, direction, entry_time, now, entry, open_px, sl, tp, "news_blackout", strategy, score, spread_r)
        if age_minutes >= max_hold:
            return build_trade(symbol, market, direction, entry_time, now, entry, open_px, sl, tp, "max_hold", strategy, score, spread_r)
        if env_bool("MT5_TREND_MONITOR_ENABLE", True) and age_minutes >= trend_min_age and trend_invalidated(df.iloc[: j + 1], direction):
            return build_trade(symbol, market, direction, entry_time, now, entry, open_px, sl, tp, "trend_invalidated", strategy, score, spread_r)

        high = float(row["High"])
        low = float(row["Low"])
        if direction == "BUY":
            hit_sl = low <= sl
            hit_tp = high >= tp
            if hit_sl:
                return build_trade(symbol, market, direction, entry_time, now, entry, sl, sl, tp, "hard_sl", strategy, score, spread_r)
            if hit_tp:
                return build_trade(symbol, market, direction, entry_time, now, entry, tp, sl, tp, "take_profit", strategy, score, spread_r)
        else:
            hit_sl = high >= sl
            hit_tp = low <= tp
            if hit_sl:
                return build_trade(symbol, market, direction, entry_time, now, entry, sl, sl, tp, "hard_sl", strategy, score, spread_r)
            if hit_tp:
                return build_trade(symbol, market, direction, entry_time, now, entry, tp, sl, tp, "take_profit", strategy, score, spread_r)
    final_time = df.index[-1].to_pydatetime()
    final_exit = float(df.iloc[-1]["Close"])
    return build_trade(symbol, market, direction, entry_time, final_time, entry, final_exit, sl, tp, "end_of_data", strategy, score, spread_r)


def build_trade(
    symbol: str,
    market: str,
    direction: str,
    entry_time: datetime,
    exit_time: datetime,
    entry: float,
    exit_px: float,
    sl: float,
    tp: float,
    reason: str,
    strategy: str,
    score: float,
    spread_r: float,
) -> Trade:
    risk = max(abs(entry - sl), 1e-12)
    pnl_price = (exit_px - entry) if direction == "BUY" else (entry - exit_px)
    return Trade(
        symbol=symbol,
        market=market,
        direction=direction,
        entry_time=entry_time,
        exit_time=exit_time,
        entry=entry,
        exit=exit_px,
        sl=sl,
        tp=tp,
        r_multiple=pnl_price / risk,
        pnl_price=pnl_price,
        reason=reason,
        strategy=strategy,
        score=score,
        spread_r=spread_r,
        session=session_label(entry_time),
    )


def infer_strategy_type(strategy: str) -> str:
    name = strategy.upper()
    if "MEAN" in name:
        return "mean reversion"
    if "BREAK" in name or "ORB" in name:
        return "breakout"
    if "PULLBACK" in name:
        return "trend continuation"
    if "MOMENTUM" in name or "PRIORITY" in name:
        return "momentum"
    return "no trade" if not name else name.lower()


def summarize(symbol: str, market: str, trades: list[Trade], bars: int, spread_source: str) -> dict:
    if not trades:
        return {
            "symbol": symbol,
            "market": market,
            "bars": bars,
            "total_trades": 0,
            "recommendation": "NEEDS SEPARATE LOGIC",
            "best_strategy_type": "no trade",
            "risk_tier": "demo-only",
            "lot_recommendation": "no lot-size change without approval",
            "spread_source": spread_source,
        }
    wins = [t.r_multiple for t in trades if t.r_multiple > 0]
    losses = [t.r_multiple for t in trades if t.r_multiple < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    pf = gross_win / gross_loss if gross_loss > 0 else (999.0 if gross_win > 0 else 0.0)
    expectancy = sum(t.r_multiple for t in trades) / len(trades)
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in trades:
        equity += t.r_multiple
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    by_session: dict[str, list[float]] = {}
    by_strategy: dict[str, list[float]] = {}
    for t in trades:
        by_session.setdefault(t.session, []).append(t.r_multiple)
        by_strategy.setdefault(infer_strategy_type(t.strategy), []).append(t.r_multiple)
    session_scores = {k: sum(v) / len(v) for k, v in by_session.items()}
    strategy_scores = {k: sum(v) / len(v) for k, v in by_strategy.items()}
    best_session = max(session_scores, key=session_scores.get)
    worst_session = min(session_scores, key=session_scores.get)
    best_strategy = max(strategy_scores, key=strategy_scores.get)
    if len(trades) < 5:
        rec = "DEMO ONLY"
    elif expectancy > 0.12 and pf >= 1.25:
        rec = "KEEP"
    elif expectancy > 0.0 and pf >= 1.05:
        rec = "DEMO ONLY"
    else:
        rec = "DISABLE"
    if market in {"index_cfd", "index_cfd_eu", "metal"}:
        risk_tier = "aggressive-test" if rec == "KEEP" else "careful"
    elif symbol.upper() in {"GBPJPY", "EURJPY", "XAGUSD", "SILVER", "USDZAR"}:
        risk_tier = "careful"
    else:
        risk_tier = "normal"
    return {
        "symbol": symbol,
        "market": market,
        "bars": bars,
        "total_trades": len(trades),
        "win_rate": round(len(wins) / len(trades), 4),
        "profit_factor": round(pf, 4),
        "max_drawdown_r": round(abs(max_dd), 4),
        "average_win_r": round(sum(wins) / len(wins), 4) if wins else 0.0,
        "average_loss_r": round(sum(losses) / len(losses), 4) if losses else 0.0,
        "expectancy_r": round(expectancy, 4),
        "spread_impact_r": round(sum(t.spread_r for t in trades) / len(trades), 4),
        "spread_source": spread_source,
        "best_session": best_session,
        "worst_session": worst_session,
        "best_strategy_type": best_strategy,
        "risk_tier": risk_tier,
        "lot_recommendation": "no lot-size change without approval",
        "recommendation": rec,
        "exit_reasons": {reason: sum(1 for t in trades if t.reason == reason) for reason in sorted({t.reason for t in trades})},
    }


def backtest_symbol(runtime, symbol: str, args, modules) -> dict:
    _, events_in_window, CFG, _compute_stop_distance, _rr_for_market, evaluate_probability, execution_ok = modules
    market = canonical_market(runtime, symbol)
    df = load_rates(runtime, symbol, args.timeframe, args.days, args.max_bars)
    atr = atr_series(df)
    priority = symbol.upper() in priority_symbols()
    spread_source = "historical" if "Spread" in df.columns else "missing"
    trades: list[Trade] = []
    daily_counts: dict[tuple[str, str], int] = {}
    daily_limit = max_daily_trades_for_symbol(symbol, priority)
    lookback = max(80, int(args.lookback))
    i = lookback
    while i < len(df) - 2:
        now = df.index[i].to_pydatetime()
        if not market_window_open(CFG, market, now):
            i += 1
            continue
        if events_in_window(now):
            i += 1
            continue
        day_key = (symbol, now.astimezone(SAST).date().isoformat())
        if daily_limit > 0 and daily_counts.get(day_key, 0) >= daily_limit:
            i += 1
            continue
        window = df.iloc[: i + 1].copy()
        sig = evaluate_probability(window, sym=symbol, market=market, now_dt=now)
        if not sig:
            i += 1
            continue
        direction = str(sig.get("direction") or "").upper()
        if direction not in {"BUY", "SELL"}:
            i += 1
            continue
        score = float(sig.get("score") or 0.0)
        strategy = str(sig.get("strategy") or sig.get("mode") or "").upper()
        if score < min_score_for(symbol, market, strategy, priority):
            i += 1
            continue
        price = float(df.iloc[i]["Close"])
        atr_value = float(atr.iloc[i]) if not pd.isna(atr.iloc[i]) else 0.0
        ok, _reason = execution_ok(price, atr_value, market=market)
        if not ok:
            i += 1
            continue
        next_i = i + 1
        entry = float(df.iloc[next_i]["Open"])
        stop_dist = _compute_stop_distance(entry, atr_value, market=market, is_crypto=(market == "crypto"))
        rr = _rr_for_market(market, sym=symbol, sig_mode=strategy.lower(), is_crypto=(market == "crypto"))
        sl = entry - stop_dist if direction == "BUY" else entry + stop_dist
        tp = entry + stop_dist * rr if direction == "BUY" else entry - stop_dist * rr
        target_r = env_float_for_symbol("MT5_SCALP_TP_R", symbol, market, 1.0)
        if env_bool("MT5_SCALP_TP_ENABLE", True) and target_r > 0:
            tp = entry + stop_dist * target_r if direction == "BUY" else entry - stop_dist * target_r
        spread = float(df.iloc[next_i].get("Spread", 0.0) or 0.0) if "Spread" in df.columns else 0.0
        spread_r = spread / max(stop_dist, 1e-12)
        trade = exit_trade(df, next_i, symbol, market, direction, entry, sl, tp, strategy, score, spread_r, CFG, events_in_window)
        trades.append(trade)
        daily_counts[day_key] = daily_counts.get(day_key, 0) + 1
        exit_idx = df.index.get_indexer([pd.Timestamp(trade.exit_time)], method="nearest")[0]
        i = max(exit_idx + 1, next_i + 1)
    return summarize(symbol, market, trades, len(df), spread_source)


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only MT5 bridge backtester for deployed evaluate_probability().")
    parser.add_argument("--env", default="/etc/scalpbot/scalpbot-mt5.env")
    parser.add_argument("--symbols", default="")
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--max-bars", type=int, default=0)
    parser.add_argument("--lookback", type=int, default=80)
    parser.add_argument("--timeframe", default="M15")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    load_env(Path(args.env))
    modules = import_strategy_modules()
    MT5RuntimeConfig = modules[0]
    runtime = MT5RuntimeConfig()
    configured = [item.strip().upper() for item in args.symbols.split(",") if item.strip()]
    symbols = configured or [sym.upper() for sym in runtime.all_symbols()]
    results = []
    errors = {}
    for symbol in symbols:
        try:
            results.append(backtest_symbol(runtime, symbol, args, modules))
        except Exception as exc:
            errors[symbol] = str(exc)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "read_only": True,
        "engine": "scalping_bot_v4.evaluate_probability",
        "timeframe": args.timeframe,
        "days": args.days,
        "results": results,
        "errors": errors,
    }
    if args.json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        for row in results:
            print(
                f"{row['symbol']}: trades={row.get('total_trades', 0)} "
                f"pf={row.get('profit_factor', 0)} expR={row.get('expectancy_r', 0)} "
                f"rec={row.get('recommendation', '')}"
            )
        if errors:
            print("errors=" + json.dumps(errors, indent=2))
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
