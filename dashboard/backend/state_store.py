"""
SQLite read/write helpers for the bot dashboard.
The bot writes here; the FastAPI backend reads here.
WAL mode so both processes can hit the file simultaneously.
"""
import sqlite3
import json
import csv
import os
from datetime import datetime, date, timedelta, timezone
from trading.observability import InstrumentedSQLiteConnection
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

MT5_EXPECTED_STATE_DB = Path("/opt/cipherfx_mt5/state/mt5_state.db")


def _state_db_path() -> Path:
    raw = os.getenv("SCALPBOT_STATE_DB")
    backend = os.getenv("CIPHERFX_BROKER_BACKEND", "").strip().lower()

    if backend == "mt5":
        if not raw:
            raise RuntimeError("SCALPBOT_STATE_DB is required when CIPHERFX_BROKER_BACKEND=mt5")
        value = str(raw).strip()
        if value != str(MT5_EXPECTED_STATE_DB):
            raise RuntimeError(f"SCALPBOT_STATE_DB must be {MT5_EXPECTED_STATE_DB} for MT5; got {value}")
        if "/opt/scalp_v3" in value:
            raise RuntimeError("SCALPBOT_STATE_DB must not point into /opt/scalp_v3 for MT5")
        if Path(value).name == "bot_state.db":
            raise RuntimeError("SCALPBOT_STATE_DB must not use bot_state.db for MT5")
        return Path(value)

    return Path(raw) if raw else Path(__file__).parent / "bot_state.db"


DB_PATH = _state_db_path()
DEFAULT_TZ = "Africa/Johannesburg"
DEFAULT_TZ_LABEL = "SAST"
SCORE_BANDS = (
    (0, 59, "0-59"),
    (60, 69, "60-69"),
    (70, 79, "70-79"),
    (80, 89, "80-89"),
    (90, 100, "90-100"),
)


def _tz_name() -> str:
    return DEFAULT_TZ


def _tz_label() -> str:
    return DEFAULT_TZ_LABEL


def _mt5_server_utc_offset_hours() -> float:
    try:
        return float(os.getenv("MT5_SERVER_UTC_OFFSET_HOURS", "3"))
    except Exception:
        return 3.0


def _numeric_times_are_broker_time() -> bool:
    return str(os.getenv("MT5_NUMERIC_TIMES_ARE_BROKER_TIME", "1")).strip().lower() not in {"0", "false", "no", "off"}


def _tzinfo():
    if ZoneInfo is None:
        return None
    try:
        return ZoneInfo(_tz_name())
    except Exception:
        return None


def _parse_dt(value):
    if value in (None, ""):
        return None
    raw = str(value).strip().replace("Z", "+00:00")
    if not raw:
        return None
    try:
        # MT5 bridge files commonly expose times as Unix seconds. Treat only
        # timestamp-sized numbers this way so compact dates like 20260706 still
        # flow through the date format parser below.
        if raw.replace(".", "", 1).isdigit():
            stamp = float(raw)
            if stamp > 100000000000:
                stamp /= 1000
            if stamp > 1000000000:
                if _numeric_times_are_broker_time():
                    stamp -= _mt5_server_utc_offset_hours() * 3600
                return datetime.fromtimestamp(stamp, tz=timezone.utc)
    except Exception:
        pass
    try:
        return datetime.fromisoformat(raw)
    except Exception:
        pass
    for fmt in (
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
        "%Y%m%d",
        "%Y%m%dT%H%M%S",
        "%Y%m%dT%H%M",
        "%Y%m%d %H:%M:%S",
        "%Y%m%d-%H:%M:%S",
        "%Y.%m.%d %H:%M:%S",
        "%Y.%m.%d %H:%M",
        "%Y.%m.%d",
    ):
        try:
            return datetime.strptime(raw, fmt)
        except Exception:
            continue
    return None


def _local_dt(ts_value=None, fallback=None):
    dt = _parse_dt(ts_value) or _parse_dt(fallback)
    tz = _tzinfo()
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
        if tz:
            dt = dt.astimezone(tz)
    elif tz:
        dt = dt.astimezone(tz)
    return dt


def trade_date_for(ts_value=None, fallback=None) -> str:
    dt = _local_dt(ts_value, fallback)
    tz = _tzinfo()
    if dt is None:
        now = datetime.now(tz) if tz else datetime.now()
        return now.date().isoformat()
    return dt.date().isoformat()


def trading_today() -> str:
    tz = _tzinfo()
    now = datetime.now(tz) if tz else datetime.now()
    return now.date().isoformat()

def broker_trading_today() -> str:
    offset = timedelta(hours=_mt5_server_utc_offset_hours())
    return (datetime.now(timezone.utc) + offset).date().isoformat()


def broker_trade_date_for(ts_value=None, fallback=None) -> str:
    dt = _parse_dt(ts_value) or _parse_dt(fallback)
    if dt is None:
        return broker_trading_today()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return (dt + timedelta(hours=_mt5_server_utc_offset_hours())).date().isoformat()



def _empty_date_detail() -> dict:
    return {
        "iso": "",
        "date": "",
        "day": "",
        "month": "",
        "month_name": "",
        "month_short": "",
        "year": "",
        "time": "",
        "label": "",
        "tz": _tz_label(),
        "timezone": _tz_name(),
        "month_label": "",
    }


def _date_detail_from_dt(dt) -> dict:
    if dt is None:
        return _empty_date_detail()
    month_short = dt.strftime("%b")
    month_name = dt.strftime("%B")
    return {
        "iso": dt.isoformat(),
        "date": dt.date().isoformat(),
        "day": dt.day,
        "month": dt.month,
        "month_name": month_name,
        "month_short": month_short,
        "year": dt.year,
        "time": dt.strftime("%H:%M:%S"),
        "label": f"{month_short} {dt.day}, {dt.year} {dt.strftime('%H:%M')} {_tz_label()}",
        "tz": _tz_label(),
        "timezone": _tz_name(),
        "month_label": f"{month_short} {dt.year}",
    }


def date_detail_for(ts_value=None, fallback=None) -> dict:
    return _date_detail_from_dt(_local_dt(ts_value, fallback))


def _broker_encoded_iso_to_local_dt(value):
    dt = _parse_dt(value)
    tz = _tzinfo()
    if dt is None or tz is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    utc_dt = dt.astimezone(timezone.utc) - timedelta(hours=_mt5_server_utc_offset_hours())
    return utc_dt.astimezone(tz)


def _add_date_prefix(row: dict, prefix: str, detail: dict) -> None:
    for key, value in detail.items():
        row[f"{prefix}_{key}"] = value


def duration_label(opened_at=None, closed_at=None) -> str:
    opened = _local_dt(opened_at)
    if opened is None:
        return ""
    closed = _local_dt(closed_at)
    if closed is None:
        tz = _tzinfo()
        closed = datetime.now(tz) if tz else datetime.now()
    seconds = int((closed - opened).total_seconds())
    if seconds < 0:
        return ""
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{secs}s"


def enrich_position_dates(row: dict) -> dict:
    data = dict(row)
    opened_source = (
        data.get("trade_opened_at")
        or data.get("opened_at")
        or data.get("time")
        or data.get("updated_at")
    )
    opened = date_detail_for(opened_source)
    _add_date_prefix(data, "opened", opened)
    if opened.get("iso"):
        data["opened_at"] = opened["iso"]
    data["trade_date"] = data.get("trade_date") or opened.get("date") or trade_date_for(opened_source)
    data["duration_label"] = duration_label(opened.get("iso") or opened_source)
    return data


def enrich_trade_dates(row: dict) -> dict:
    data = dict(row)
    opened_source = data.get("opened_at") or data.get("time") or data.get("trade_date")
    closed_source = data.get("closed_at") or data.get("exit_time") or data.get("trade_date")
    opened = date_detail_for(opened_source)
    closed = date_detail_for(closed_source)
    opened_dt = _local_dt(opened_source)
    closed_dt = _local_dt(closed_source)
    trade_id = str(data.get("trade_id") or data.get("id") or "")
    sig_mode = str(data.get("sig_mode") or "")
    if (
        opened_dt is not None
        and closed_dt is not None
        and opened_dt > closed_dt + timedelta(minutes=1)
        and (trade_id.startswith("mt5_adopted_") or sig_mode == "mt5_adopted")
    ):
        corrected_opened = _broker_encoded_iso_to_local_dt(opened_source)
        if corrected_opened is not None and corrected_opened <= closed_dt + timedelta(minutes=1):
            opened = _date_detail_from_dt(corrected_opened)
    trade_detail = date_detail_for(data.get("trade_date") or closed_source or opened_source)
    _add_date_prefix(data, "opened", opened)
    _add_date_prefix(data, "closed", closed)
    _add_date_prefix(data, "trade", trade_detail)
    if opened.get("iso"):
        data["opened_at"] = opened["iso"]
    if closed.get("iso"):
        data["closed_at"] = closed["iso"]
    data["trade_date"] = data.get("trade_date") or trade_detail.get("date") or trade_date_for(closed_source, fallback=opened_source)
    data["duration_label"] = duration_label(opened.get("iso") or opened_source, closed.get("iso") or closed_source)
    return data


def trading_week_range(today_value: str | None = None) -> tuple[str, str]:
    if today_value:
        today = datetime.fromisoformat(today_value).date()
    else:
        today = datetime.fromisoformat(trading_today()).date()
    week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=6)
    return week_start.isoformat(), week_end.isoformat()


def trading_month_range(today_value: str | None = None) -> tuple[str, str]:
    if today_value:
        today = datetime.fromisoformat(today_value).date()
    else:
        today = datetime.fromisoformat(trading_today()).date()
    month_start = today.replace(day=1)
    if today.month == 12:
        next_month = today.replace(year=today.year + 1, month=1, day=1)
    else:
        next_month = today.replace(month=today.month + 1, day=1)
    month_end = next_month - timedelta(days=1)
    return month_start.isoformat(), month_end.isoformat()


def get_conn():
    # Keep dashboard/trading writes bounded. A long SQLite wait must not pin the
    # trading process during a service stop or feed outage.
    conn = sqlite3.connect(DB_PATH, timeout=5.0, check_same_thread=False, factory=InstrumentedSQLiteConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    # WAL mode is initialized once; do not request a database-level mode change on every connection.
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA wal_autocheckpoint=1000")
    conn.execute("PRAGMA journal_size_limit=67108864")
    return conn


def checkpoint_wal() -> dict:
    """Perform a non-blocking WAL checkpoint for bounded runtime persistence."""
    conn = sqlite3.connect(DB_PATH, timeout=2.0, check_same_thread=False, factory=InstrumentedSQLiteConnection)
    try:
        conn.execute("PRAGMA busy_timeout=2000")
        result = conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone() or (0, 0, 0)
        return {
            "busy": int(result[0] or 0),
            "log_pages": int(result[1] or 0),
            "checkpointed_pages": int(result[2] or 0),
            "status": "PASS" if int(result[0] or 0) == 0 else "BUSY",
        }
    except sqlite3.Error as exc:
        return {"status": "FAIL", "error": str(exc)[:180]}
    finally:
        conn.close()


def score_band(score) -> str:
    try:
        val = int(round(float(score)))
    except Exception:
        return "0-59"
    for lo, hi, label in SCORE_BANDS:
        if lo <= val <= hi:
            return label
    return "90-100" if val > 100 else "0-59"


def init_db():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS positions (
            sym         TEXT PRIMARY KEY,
            market      TEXT,
            direction   TEXT,
            qty         REAL,
            entry       REAL,
            current     REAL,
            sl          REAL,
            tp          REAL,
            atr         REAL,
            unrealized  REAL,
            updated_at  TEXT
        );

        CREATE TABLE IF NOT EXISTS signals (
            sym         TEXT PRIMARY KEY,
            market      TEXT,
            sig_mode    TEXT,
            direction   TEXT,
            price       REAL,
            atr         REAL,
            atr_pct     REAL,
            rsi         REAL,
            macd_h      REAL,
            vsurge      REAL,
            vwap        REAL,
            vwap_z      REAL,
            stoch_k     REAL,
            stoch_d     REAL,
            regime      TEXT,
            gate        TEXT,
            gate_ok     INTEGER,
            conditions  TEXT,
            updated_at  TEXT
        );

        CREATE TABLE IF NOT EXISTS trades (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id    TEXT UNIQUE,
            sym         TEXT,
            market      TEXT,
            direction   TEXT,
            qty         REAL,
            entry       REAL,
            exit_px     REAL,
            sl          REAL,
            tp          REAL,
            atr         REAL,
            realized    REAL,
            outcome     TEXT,
            duration_s  REAL,
            sig_mode    TEXT,
            trade_date  TEXT,
            opened_at   TEXT,
            closed_at   TEXT,
            session     TEXT,
            population  TEXT,
            setup_created_at TEXT,
            m1_candle_closed_at TEXT,
            confirmation_detected_at TEXT,
            order_send_at TEXT,
            mt5_fill_at TEXT,
            confirmation_latency_ms REAL,
            send_latency_ms REAL,
            total_latency_ms REAL
        );

        CREATE TABLE IF NOT EXISTS setup_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id     TEXT UNIQUE,
            sym          TEXT,
            market       TEXT,
            direction    TEXT,
            strategy     TEXT,
            sig_mode     TEXT,
            timeframe_setup TEXT,
            final_score  REAL,
            score_band   TEXT,
            score_breakdown TEXT,
            bias_1h_score REAL,
            setup_15m_score REAL,
            trigger_5m_score REAL,
            execution_1m_score REAL,
            smc_score    REAL,
            risk_reward_score REAL,
            session_score REAL,
            execution_1m_result TEXT,
            smc_confirmation INTEGER,
            spread_signal REAL,
            spread_execution REAL,
            slippage     REAL,
            commission   REAL,
            entry_price  REAL,
            stop_price   REAL,
            target_price REAL,
            result_r     REAL,
            result_ccy   REAL,
            outcome      TEXT,
            exit_reason  TEXT,
            status       TEXT,
            trade_date   TEXT,
            created_at   TEXT
        );

        CREATE TABLE IF NOT EXISTS candles (
            sym         TEXT,
            ts          TEXT,
            open        REAL,
            high        REAL,
            low         REAL,
            close       REAL,
            volume      REAL,
            PRIMARY KEY (sym, ts)
        );

        CREATE TABLE IF NOT EXISTS m1_candles (
            sym         TEXT,
            ts          TEXT,
            open        REAL,
            high        REAL,
            low         REAL,
            close       REAL,
            volume      REAL,
            spread      REAL,
            PRIMARY KEY (sym, ts)
        );

        CREATE TABLE IF NOT EXISTS bot_status (
            key   TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS news (
            sym         TEXT PRIMARY KEY,
            sentiment   TEXT,
            score       REAL,
            headlines   TEXT,
            updated_at  TEXT
        );
    """)
    conn.commit()

    # Migrate: add columns that may not exist in older DBs
    for col, typ in [
        ("trade_date",   "TEXT"),
        ("vwap_z",      "REAL"),
        ("stoch_k",     "REAL"),
        ("stoch_d",     "REAL"),
        ("htf_trend",   "TEXT"),
        ("daily_trend", "TEXT"),
        ("sector_etf",  "TEXT"),
        ("sector_ret",  "REAL"),
        ("spread_pct",  "REAL"),
        ("vix_level",   "REAL"),
        ("score", "REAL"),
        ("strategy", "TEXT"),
        ("score_band", "TEXT"),
        ("score_breakdown", "TEXT"),
        ("bias_1h_score", "REAL"),
        ("setup_15m_score", "REAL"),
        ("trigger_5m_score", "REAL"),
        ("execution_1m_score", "REAL"),
        ("smc_score", "REAL"),
        ("risk_reward_score", "REAL"),
        ("session_score", "REAL"),
        ("execution_1m_result", "TEXT"),
        ("smc_confirmation", "INTEGER"),
    ]:
        try:
            conn.execute(f"ALTER TABLE signals ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass  # column already exists
    for col, typ in [
        ("status", "TEXT"),
        ("strategy", "TEXT"),
        ("score", "REAL"),
        ("score_band", "TEXT"),
        ("score_breakdown", "TEXT"),
        ("bias_1h_score", "REAL"),
        ("setup_15m_score", "REAL"),
        ("trigger_5m_score", "REAL"),
        ("execution_1m_score", "REAL"),
        ("smc_score", "REAL"),
        ("risk_reward_score", "REAL"),
        ("session_score", "REAL"),
        ("timeframe_setup", "TEXT"),
        ("execution_1m_result", "TEXT"),
        ("smc_confirmation", "INTEGER"),
        ("spread_signal", "REAL"),
        ("spread_execution", "REAL"),
        ("slippage", "REAL"),
        ("commission", "REAL"),
        ("result_r", "REAL"),
        ("result_ccy", "REAL"),
        ("exit_reason", "TEXT"),
        ("comparison_bucket", "TEXT"),
        ("forward_phase", "TEXT"),
        ("rejected_reason", "TEXT"),
        ("session", "TEXT"),
        ("population", "TEXT"),
        ("setup_created_at", "TEXT"),
        ("m1_candle_closed_at", "TEXT"),
        ("confirmation_detected_at", "TEXT"),
        ("order_send_at", "TEXT"),
        ("mt5_fill_at", "TEXT"),
        ("confirmation_latency_ms", "REAL"),
        ("send_latency_ms", "REAL"),
        ("total_latency_ms", "REAL"),
    ]:
        try:
            conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass
    conn.execute("""
        UPDATE trades
        SET population = CASE
            WHEN lower(COALESCE(sig_mode, "")) LIKE "%adopt%"
              OR lower(COALESCE(sig_mode, "")) LIKE "%manual%"
              OR lower(COALESCE(sig_mode, "")) LIKE "%import%" THEN "adopted_imported"
            WHEN lower(COALESCE(sig_mode, "")) LIKE "%pyramid%"
              OR lower(COALESCE(strategy, "")) LIKE "%pyramid%" THEN "profit_pyramid"
            WHEN lower(COALESCE(sig_mode, "")) LIKE "%recovery%"
              OR lower(COALESCE(sig_mode, "")) LIKE "%management%" THEN "recovery_management"
            ELSE "direct_strategy"
        END
        WHERE population IS NULL OR population = ""
    """)

    rows = conn.execute(
        """
        SELECT trade_id, opened_at, closed_at, trade_date
        FROM trades
        WHERE trade_date IS NULL
           OR trade_date = ''
           OR length(trade_date) != 10
           OR trade_date LIKE '%T%'
        """
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE trades SET trade_date=? WHERE trade_id=?",
            (trade_date_for(row["closed_at"], fallback=row["opened_at"]), row["trade_id"])
        )
    conn.commit()
    conn.close()


# ── Writers (called from scalping_bot_v4.py) ──────────────────────────────────

def upsert_position(sym, market, direction, qty, entry, current, sl, tp, atr, unrealized):
    conn = get_conn()
    conn.execute("""
        INSERT INTO positions (sym,market,direction,qty,entry,current,sl,tp,atr,unrealized,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(sym) DO UPDATE SET
            current=excluded.current, unrealized=excluded.unrealized,
            sl=excluded.sl, tp=excluded.tp, updated_at=excluded.updated_at
    """, (sym, market, direction, qty, entry, current, sl, tp, atr, unrealized,
          datetime.now().isoformat()))
    conn.commit(); conn.close()


def remove_position(sym):
    conn = get_conn()
    conn.execute("DELETE FROM positions WHERE sym=?", (sym,))
    conn.commit(); conn.close()


def upsert_signal(sym, market, sig_mode, direction, price, atr, atr_pct,
                  rsi, macd_h, vsurge, vwap, regime, gate, gate_ok, conditions: dict,
                  vwap_z=None, stoch_k=None, stoch_d=None,
                  htf_trend=None, daily_trend=None,
                  sector_etf=None, sector_ret=None,
                  spread_pct=None, vix_level=None,
                  score=None, strategy=None, score_breakdown=None,
                  bias_1h_score=None, setup_15m_score=None, trigger_5m_score=None,
                  execution_1m_score=None, smc_score=None, risk_reward_score=None,
                  session_score=None, execution_1m_result=None, smc_confirmation=None):
    conn = get_conn()
    conn.execute("""
        INSERT INTO signals (sym,market,sig_mode,direction,price,atr,atr_pct,rsi,macd_h,
                             vsurge,vwap,vwap_z,stoch_k,stoch_d,regime,gate,gate_ok,conditions,
                             htf_trend,daily_trend,sector_etf,sector_ret,spread_pct,vix_level,
                             score,strategy,score_band,score_breakdown,bias_1h_score,
                             setup_15m_score,trigger_5m_score,execution_1m_score,smc_score,
                             risk_reward_score,session_score,execution_1m_result,smc_confirmation,
                             updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(sym) DO UPDATE SET
            market=excluded.market, sig_mode=excluded.sig_mode,
            direction=excluded.direction, price=excluded.price, atr=excluded.atr,
            atr_pct=excluded.atr_pct, rsi=excluded.rsi, macd_h=excluded.macd_h,
            vsurge=excluded.vsurge, vwap=excluded.vwap, vwap_z=excluded.vwap_z,
            stoch_k=excluded.stoch_k, stoch_d=excluded.stoch_d, regime=excluded.regime,
            gate=excluded.gate, gate_ok=excluded.gate_ok, conditions=excluded.conditions,
            htf_trend=excluded.htf_trend, daily_trend=excluded.daily_trend,
            sector_etf=excluded.sector_etf, sector_ret=excluded.sector_ret,
            spread_pct=excluded.spread_pct, vix_level=excluded.vix_level,
            score=excluded.score, strategy=excluded.strategy, score_band=excluded.score_band,
            score_breakdown=excluded.score_breakdown, bias_1h_score=excluded.bias_1h_score,
            setup_15m_score=excluded.setup_15m_score, trigger_5m_score=excluded.trigger_5m_score,
            execution_1m_score=excluded.execution_1m_score, smc_score=excluded.smc_score,
            risk_reward_score=excluded.risk_reward_score, session_score=excluded.session_score,
            execution_1m_result=excluded.execution_1m_result, smc_confirmation=excluded.smc_confirmation,
            updated_at=excluded.updated_at
    """, (sym, market, sig_mode, direction, price, atr, atr_pct, rsi, macd_h,
          vsurge, vwap, vwap_z, stoch_k, stoch_d, regime, gate, 1 if gate_ok else 0,
          json.dumps(conditions),
          htf_trend, daily_trend, sector_etf, sector_ret, spread_pct, vix_level,
          score, strategy, score_band(score), json.dumps(score_breakdown or {}),
          bias_1h_score, setup_15m_score, trigger_5m_score, execution_1m_score,
          smc_score, risk_reward_score, session_score, execution_1m_result,
          None if smc_confirmation is None else (1 if smc_confirmation else 0),
          datetime.now().isoformat()))
    conn.commit(); conn.close()


def insert_trade(trade_id, sym, market, direction, qty, entry, sl, tp, atr, sig_mode, opened_at,
                 status=None, strategy=None, score=None, score_breakdown=None,
                 bias_1h_score=None, setup_15m_score=None, trigger_5m_score=None,
                 execution_1m_score=None, smc_score=None, risk_reward_score=None,
                 session_score=None, timeframe_setup=None, execution_1m_result=None,
                 smc_confirmation=None, spread_signal=None, spread_execution=None,
                 slippage=None, commission=None, result_r=None, result_ccy=None,
                 exit_reason=None, comparison_bucket=None, forward_phase=None,
                 rejected_reason=None, session=None, population=None, setup_created_at=None, m1_candle_closed_at=None,
                 confirmation_detected_at=None, order_send_at=None, mt5_fill_at=None,
                 confirmation_latency_ms=None, send_latency_ms=None, total_latency_ms=None):
    conn = get_conn()
    trade_date = trade_date_for(opened_at)
    conn.execute("""
        INSERT OR IGNORE INTO trades
        (trade_id,sym,market,direction,qty,entry,sl,tp,atr,sig_mode,trade_date,opened_at,
         status,strategy,score,score_band,score_breakdown,bias_1h_score,setup_15m_score,
         trigger_5m_score,execution_1m_score,smc_score,risk_reward_score,session_score,
         timeframe_setup,execution_1m_result,smc_confirmation,spread_signal,spread_execution,
         slippage,commission,result_r,result_ccy,exit_reason,comparison_bucket,forward_phase,rejected_reason,
         session,population,setup_created_at,m1_candle_closed_at,confirmation_detected_at,order_send_at,mt5_fill_at,
         confirmation_latency_ms,send_latency_ms,total_latency_ms)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (trade_id, sym, market, direction, qty, entry, sl, tp, atr, sig_mode,
          trade_date, str(opened_at), status, strategy, score, score_band(score),
          json.dumps(score_breakdown or {}), bias_1h_score, setup_15m_score, trigger_5m_score,
          execution_1m_score, smc_score, risk_reward_score, session_score, timeframe_setup,
          execution_1m_result, None if smc_confirmation is None else (1 if smc_confirmation else 0),
          spread_signal, spread_execution, slippage, commission, result_r, result_ccy,
          exit_reason, comparison_bucket, forward_phase, rejected_reason, session, population, setup_created_at,
          m1_candle_closed_at, confirmation_detected_at, order_send_at, mt5_fill_at, confirmation_latency_ms,
          send_latency_ms, total_latency_ms))
    conn.commit(); conn.close()


def close_trade(trade_id, exit_px, realized, outcome, closed_at,
                sym=None, market=None, direction=None, qty=None,
                entry=None, sl=None, tp=None, atr=None, sig_mode=None, opened_at=None,
                trade_date=None, status=None, result_r=None, result_ccy=None,
                exit_reason=None, spread_execution=None, slippage=None, commission=None):
    conn = get_conn()
    entry_row = conn.execute("SELECT entry, opened_at, trade_date FROM trades WHERE trade_id=?", (trade_id,)).fetchone()
    dur = None
    ref_opened = opened_at or (entry_row["opened_at"] if entry_row else None)
    row_trade_date = trade_date or (entry_row["trade_date"] if entry_row else None)
    resolved_trade_date = row_trade_date or trade_date_for(closed_at, fallback=opened_at)
    if ref_opened:
        try:
            dur = (datetime.fromisoformat(str(closed_at)) - datetime.fromisoformat(str(ref_opened))).total_seconds()
        except Exception:
            pass
    if entry_row:
        conn.execute("""
            UPDATE trades SET exit_px=?, realized=?, outcome=?, closed_at=?, duration_s=?, trade_date=?,
                              status=COALESCE(?, status), result_r=COALESCE(?, result_r),
                              result_ccy=COALESCE(?, result_ccy), exit_reason=COALESCE(?, exit_reason),
                              spread_execution=COALESCE(?, spread_execution), slippage=COALESCE(?, slippage),
                              commission=COALESCE(?, commission)
            WHERE trade_id=?
        """, (exit_px, realized, outcome, str(closed_at), dur, resolved_trade_date,
              status, result_r, result_ccy, exit_reason, spread_execution, slippage,
              commission, trade_id))
    else:
        conn.execute("""
            INSERT OR IGNORE INTO trades
            (trade_id,sym,market,direction,qty,entry,sl,tp,atr,sig_mode,
             exit_px,realized,outcome,trade_date,opened_at,closed_at,duration_s,status,
             result_r,result_ccy,exit_reason,spread_execution,slippage,commission)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (trade_id, sym, market, direction, qty, entry, sl, tp, atr, sig_mode,
              exit_px, realized, outcome, resolved_trade_date, str(opened_at) if opened_at else None,
              str(closed_at), dur, status, result_r, result_ccy, exit_reason,
              spread_execution, slippage, commission))
    conn.commit(); conn.close()


def update_trade_protection(trade_id, sl, tp, atr=None):
    conn = get_conn()
    conn.execute(
        """
        UPDATE trades
        SET sl=?, tp=?, atr=COALESCE(?, atr)
        WHERE trade_id=? AND closed_at IS NULL
        """,
        (sl, tp, atr, trade_id),
    )
    conn.commit(); conn.close()


def find_live_trade_for_symbol(sym, direction=None, entry=None, tolerance_pct=0.001):
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT *
        FROM trades
        WHERE sym = ?
          AND closed_at IS NULL
          AND COALESCE(status, '') NOT IN ('rejected', 'cancelled', 'duplicate', 'reconciled_duplicate')
        ORDER BY id DESC
        """,
        (sym,),
    ).fetchall()
    conn.close()
    direction_text = str(direction or "").upper()
    try:
        entry_value = float(entry)
    except Exception:
        entry_value = None
    for row in rows:
        row_direction = str(row["direction"] or "").upper()
        if direction_text and row_direction and row_direction != direction_text:
            continue
        if entry_value is not None:
            try:
                row_entry = float(row["entry"] or 0.0)
            except Exception:
                row_entry = 0.0
            tolerance = max(abs(entry_value) * float(tolerance_pct or 0.0), 1e-6)
            if abs(row_entry - entry_value) > tolerance:
                continue
        return dict(row)
    return None


def mark_trade_duplicate(trade_id, closed_at=None, exit_reason="reconciled duplicate live row"):
    conn = get_conn()
    now = closed_at or datetime.now().isoformat()
    conn.execute(
        """
        UPDATE trades
        SET status='reconciled_duplicate',
            outcome=COALESCE(outcome, 'duplicate'),
            realized=COALESCE(realized, 0),
            closed_at=COALESCE(closed_at, ?),
            exit_reason=COALESCE(exit_reason, ?)
        WHERE trade_id=? AND closed_at IS NULL
        """,
        (str(now), exit_reason, trade_id),
    )
    conn.commit(); conn.close()


def record_setup_event(event_id, sym, market, direction, strategy, sig_mode,
                       timeframe_setup, final_score, score_breakdown=None,
                       bias_1h_score=None, setup_15m_score=None, trigger_5m_score=None,
                       execution_1m_score=None, smc_score=None, risk_reward_score=None,
                       session_score=None, execution_1m_result=None, smc_confirmation=None,
                       spread_signal=None, spread_execution=None, slippage=None, commission=None,
                       entry_price=None, stop_price=None, target_price=None, result_r=None,
                       result_ccy=None, outcome=None, exit_reason=None, status=None,
                       trade_date=None):
    conn = get_conn()
    conn.execute("""
        INSERT OR REPLACE INTO setup_events
        (event_id,sym,market,direction,strategy,sig_mode,timeframe_setup,final_score,score_band,
         score_breakdown,bias_1h_score,setup_15m_score,trigger_5m_score,execution_1m_score,
         smc_score,risk_reward_score,session_score,execution_1m_result,smc_confirmation,
         spread_signal,spread_execution,slippage,commission,entry_price,stop_price,target_price,
         result_r,result_ccy,outcome,exit_reason,status,trade_date,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (event_id, sym, market, direction, strategy, sig_mode, timeframe_setup, final_score,
          score_band(final_score), json.dumps(score_breakdown or {}), bias_1h_score,
          setup_15m_score, trigger_5m_score, execution_1m_score, smc_score,
          risk_reward_score, session_score, execution_1m_result,
          None if smc_confirmation is None else (1 if smc_confirmation else 0),
          spread_signal, spread_execution, slippage, commission, entry_price, stop_price,
          target_price, result_r, result_ccy, outcome, exit_reason, status,
          trade_date or trading_today(), datetime.now().isoformat()))
    conn.commit(); conn.close()


def upsert_candles(sym, df):
    """Write OHLCV rows from a pandas DataFrame."""
    import math
    conn = get_conn()
    rows = []
    for ts, row in df.iterrows():
        try:
            o, h, l, c = float(row["Open"]), float(row["High"]), float(row["Low"]), float(row["Close"])
            v = float(row.get("Volume") or 0)
        except (ValueError, TypeError):
            continue
        if any(math.isnan(x) for x in (o, h, l, c)):
            continue
        rows.append((sym, str(ts), o, h, l, c, 0.0 if math.isnan(v) else v))
    conn.executemany("""
        INSERT OR REPLACE INTO candles (sym,ts,open,high,low,close,volume)
        VALUES (?,?,?,?,?,?,?)
    """, rows)
    conn.commit(); conn.close()


def upsert_m1_candles(sym, df, retention_days: int = 7):
    """Persist completed M1 OHLCV/spread rows and retain at least the requested replay window."""
    import math
    conn = get_conn()
    rows = []
    for ts, row in df.iterrows():
        try:
            o, h, l, c = float(row["Open"]), float(row["High"]), float(row["Low"]), float(row["Close"])
            v = float(row.get("Volume") or 0.0)
            spread = float(row.get("Spread") or row.get("spread") or 0.0)
        except (ValueError, TypeError, KeyError):
            continue
        if any(math.isnan(x) for x in (o, h, l, c, v, spread)):
            continue
        rows.append((sym, str(ts), o, h, l, c, v, spread))
    conn.executemany("""
        INSERT OR REPLACE INTO m1_candles (sym,ts,open,high,low,close,volume,spread)
        VALUES (?,?,?,?,?,?,?,?)
    """, rows)
    cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=max(7, int(retention_days or 7)))).isoformat()
    conn.execute("DELETE FROM m1_candles WHERE ts < ?", (cutoff,))
    conn.commit(); conn.close()



_M1_HISTORY_IMPORT_MTIMES = {}


def import_m1_history_archives(bridge_dir, retention_days: int = 7):
    """Import throttled MT5 M1_HISTORY archives into the authoritative SQLite replay table."""
    root = Path(str(bridge_dir or ""))
    if not root.is_dir():
        return {"status": "NO_BRIDGE_DIR", "files_seen": 0, "rows_imported": 0}
    conn = get_conn()
    rows_total = 0
    files_imported = 0
    files_seen = 0
    try:
        for path in sorted(root.glob("rates_*_M1_HISTORY.csv")):
            files_seen += 1
            try:
                mtime = path.stat().st_mtime_ns
            except OSError:
                continue
            if _M1_HISTORY_IMPORT_MTIMES.get(str(path)) == mtime:
                continue
            name = path.name
            prefix = "rates_"
            suffix = "_M1_HISTORY.csv"
            if not (name.startswith(prefix) and name.endswith(suffix)):
                continue
            sym = name[len(prefix):-len(suffix)].upper()
            alias_to_canonical = {}
            try:
                config = json.loads(Path("/opt/cipherfx_mt5/mt5_symbols.json").read_text())
                alias_to_canonical = {
                    str(alias).strip().upper(): str(canonical).strip().upper()
                    for canonical, alias in (config.get("aliases") or {}).items()
                    if alias and canonical
                }
            except Exception:
                alias_to_canonical = {}
            sym = alias_to_canonical.get(sym, sym)
            rows = []
            with path.open("r", encoding="utf-8", newline="") as handle:
                for row in csv.DictReader(handle):
                    try:
                        raw_ts = row.get("time_utc") or row.get("time") or ""
                        ts = datetime.fromtimestamp(float(raw_ts), timezone.utc).isoformat()
                        o = float(row.get("open") or 0.0)
                        h = float(row.get("high") or 0.0)
                        low = float(row.get("low") or 0.0)
                        close = float(row.get("close") or 0.0)
                        volume = float(row.get("volume") or 0.0)
                        spread = float(row.get("spread_price") or 0.0)
                    except (TypeError, ValueError, OverflowError):
                        continue
                    if min(o, h, low, close) <= 0.0:
                        continue
                    rows.append((sym, ts, o, h, low, close, volume, spread))
            if rows:
                conn.executemany(
                    """INSERT OR IGNORE INTO m1_candles
                       (sym,ts,open,high,low,close,volume,spread)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    rows,
                )
                rows_total += len(rows)
                files_imported += 1
            _M1_HISTORY_IMPORT_MTIMES[str(path)] = mtime
        cutoff = (
            datetime.now(timezone.utc).replace(tzinfo=None)
            - timedelta(days=max(7, int(retention_days or 7)))
        ).isoformat()
        conn.execute("DELETE FROM m1_candles WHERE ts < ?", (cutoff,))
        conn.commit()
        return {
            "status": "OK",
            "files_seen": files_seen,
            "files_imported": files_imported,
            "rows_imported": rows_total,
            "retention_days": max(7, int(retention_days or 7)),
        }
    finally:
        conn.close()


def set_status(key, value):
    payload = (key, json.dumps(value) if not isinstance(value, str) else value)
    last_error = None
    for attempt in range(5):
        conn = None
        try:
            conn = get_conn()
            conn.execute("INSERT OR REPLACE INTO bot_status (key,value) VALUES (?,?)", payload)
            conn.commit()
            return
        except sqlite3.OperationalError as exc:
            last_error = exc
            if "locked" not in str(exc).lower():
                raise
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:
                    pass
            time.sleep(0.25 * (attempt + 1))
        finally:
            if conn is not None:
                conn.close()
    if last_error is not None:
        raise last_error


# ── Readers (called from FastAPI) ─────────────────────────────────────────────

def read_positions(include_stale=False):
    conn = get_conn()
    # 2-minute cutoff: the live bot writes every ~15s, so old bot-owned rows are stale.
    # IBKR-recovered rows are broker truth from a manual/API recovery and must stay
    # visible while the bot is stopped; they are cleared/replaced on the next recovery.
    cutoff = (datetime.now() - timedelta(minutes=2)).isoformat()
    query = """
        SELECT p.*, t.opened_at AS trade_opened_at
        FROM positions p
        LEFT JOIN (
            SELECT sym, MAX(opened_at) AS opened_at,
                   MAX(CASE WHEN sig_mode IN ('ibkr_position', 'ibkr_recovered', 'adopted') THEN 1 ELSE 0 END) AS broker_recovered
            FROM trades
            WHERE closed_at IS NULL
            GROUP BY sym
        ) t ON t.sym = p.sym
    """
    if include_stale:
        query += " ORDER BY p.updated_at DESC"
        rows = conn.execute(query).fetchall()
    else:
        query += """
        WHERE p.updated_at >= ?
           OR t.broker_recovered = 1
        ORDER BY p.updated_at DESC
        """
        rows = conn.execute(query, (cutoff,)).fetchall()
    conn.close()
    return [enrich_position_dates(dict(r)) for r in rows]


def read_signals():
    conn = get_conn()
    # Current scanner rows cover two completed M5 cycles. Older decisions are history, not live state.
    cutoff = (datetime.now() - timedelta(minutes=7)).isoformat()
    rows = conn.execute(
        """
        SELECT * FROM signals
        WHERE updated_at >= ?
        ORDER BY updated_at DESC
        """,
        (cutoff,)
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = _systematic_public_fields(dict(r))
        if d.get("market") == "forex" and (d.get("price") or 0) > 0 and d.get("sig_mode") == "no_data":
            d["sig_mode"] = "scanning"
        out.append(d)
    return out


def _safe_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def _safe_int(value, default=0):
    try:
        return int(float(value))
    except Exception:
        return default


def _json_dict(value):
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _priority_symbols_from_env() -> list[str]:
    raw = os.getenv("MT5_PRIORITY_SYMBOLS", "")
    symbols = []
    seen = set()
    for part in raw.split(","):
        sym = part.strip().upper()
        if sym and sym not in seen:
            symbols.append(sym)
            seen.add(sym)
    return symbols


def _market_session_for_symbol(symbol: str) -> str:
    sym = str(symbol or "").upper()
    if sym in {"NAS100", "US30", "SPX500"}:
        return "us_new_york"
    if sym == "JP225":
        return "asia_tokyo"
    if sym in {"GER40", "UK100", "FRA40", "EU50"}:
        return "uk_london"
    return ""


def _symbol_market_is_open(symbol: str, now=None) -> bool:
    """Return whether a symbol belongs in the *current* scanner universe.

    This is a display filter only. It never gates the trading engine. The
    scanner must not present stale rows for a closed session as live signals.
    """
    now = now or datetime.now(timezone.utc)
    if now.weekday() >= 5:
        return False
    session = _market_session_for_symbol(symbol)
    if not session:
        return True  # FX/metals are weekday markets in the dashboard view.
    if session == "us_new_york":
        local = now.astimezone(ZoneInfo("America/New_York")) if ZoneInfo else now
        return (local.hour, local.minute) >= (9, 30) and (local.hour, local.minute) < (16, 0)
    if session == "asia_tokyo":
        local = now.astimezone(ZoneInfo("Asia/Tokyo")) if ZoneInfo else now
        return (local.hour, local.minute) >= (9, 0) and (local.hour, local.minute) < (17, 0)
    # Cipher FX dashboard convention: London opens at 10:00 SAST.
    local = now.astimezone(_tzinfo()) if _tzinfo else now
    return (local.hour, local.minute) >= (10, 0) and (local.hour, local.minute) < (19, 30)


def _filter_latest_priority_signals(signals: list[dict]) -> list[dict]:
    priority = _priority_symbols_from_env()
    if not priority:
        return signals
    priority_set = set(priority)
    latest = {}
    for row in signals:
        sym = str(row.get("sym") or "").upper()
        if sym not in priority_set:
            continue
        previous = latest.get(sym)
        if previous is None or str(row.get("updated_at") or "") > str(previous.get("updated_at") or ""):
            latest[sym] = row
    return sorted(latest.values(), key=lambda item: str(item.get("updated_at") or ""), reverse=True)


def _simple_public_reason(raw) -> str:
    value = str(raw or "").lower()
    if "market_window_closed" in value or "markets_closed" in value or "market closed" in value or "weekend" in value:
        return "Markets closed"
    if "h4_no_direction" in value or "h4_score" in value:
        return "H4 has no clear direction"
    if "h1_not_aligned" in value or "h1_score" in value:
        return "H1 not aligned with H4"
    if "m15_not_aligned" in value or "m15_score" in value:
        return "M15 structure not aligned"
    if "m5_trigger_not_ready" in value or "m5_trigger_below" in value or "m5_break" in value or "m5_retest" in value:
        return "Waiting for M5 break or retest"
    if "strategy_score_below" in value:
        return "Score below minimum"
    if "extended" in value or "exhaust" in value or "vertical" in value or "danger" in value:
        return "Move exhausted"
    if "spread" in value or "volatile" in value:
        return "Volatile market"
    if "pullback" in value or "reclaim" in value or "continuation" in value:
        return "Choppy market"
    if "range" in value:
        return "Range-bound market"
    if "no directional" in value or "flat" in value:
        return "Flat market"
    if "trend" in value or "htf" in value or "alignment" in value:
        return "Trend not aligned"
    if "stale" in value or "missing" in value or "data_" in value:
        return "Data updating"
    if "1m" in value or "confirmation" in value:
        return "Waiting for confirmation"
    if "cost" in value:
        return "Trading cost too high"
    if "profile" in value or "engine policy" in value or "final_gate" in value:
        return "Quality check"
    if "daily" in value or "loss" in value or "risk" in value:
        return "Risk limit"
    if "rejected" in value:
        return "Order declined"
    if "not qualified" in value:
        return "No clear setup"
    return "No clear setup"

def _engine_display_defaults(sym: str, market: str) -> dict:
    market_key = str(market or "").lower()
    if market_key == "forex":
        return {"engine": "FOREX_ENGINE", "asset_class": "forex", "strategy_after": ""}
    if market_key in {"index_cfd", "index_cfd_eu"}:
        return {"engine": "INDEX_ENGINE", "asset_class": "index", "strategy_after": ""}
    if market_key == "metal":
        return {"engine": "METALS_ENGINE", "asset_class": "metal", "strategy_after": ""}
    return {"engine": "", "asset_class": "", "strategy_after": ""}


def _public_scan_story(raw_reason, gate_ok=False, direction="", pending_setup=False,
                       m1_status="", direct_m5=False, setup_state="",
                       waiting_for="") -> dict:
    """Expose the active H4/H1 -> M15 POI -> M5 -> order flow."""
    code = str(raw_reason or "").upper()
    state = str(setup_state or "").upper()
    waiting = str(waiting_for or "").upper()
    stages = [
        {"key": "H4", "label": "Direction", "state": "inactive"},
        {"key": "H1", "label": "Confirm", "state": "inactive"},
        {"key": "M15", "label": "POI + Structure", "state": "inactive"},
        {"key": "M5", "label": "Break / Retest", "state": "inactive"},
        {"key": "ORDER", "label": "Order", "state": "inactive"},
    ]

    if any(token in code for token in ("DATA_STALE", "DATA_MISSING", "DATA_UNAVAILABLE", "INSUFFICIENT_HISTORY")):
        return {
            "stage": "DATA", "status": "UPDATING",
            "story": "Market data is updating; stale candles are not used.", "progress": stages,
        }

    if state in {"ARMED", "WAITING_M5"} or "M5_BREAK_OR_RETEST" in waiting:
        for stage in stages[:3]:
            stage["state"] = "complete"
        stages[3]["state"] = "current"
        return {
            "stage": "M5", "status": "WATCHING",
            "story": "M5 break or retest was not present on this scan; no setup was created.",
            "progress": stages,
        }

    if direct_m5 or state in {"CONFIRMED", "M5_TRIGGERED", "EXECUTION_PENDING", "PASS"}:
        for stage in stages[:4]:
            stage["state"] = "complete"
        stages[4]["state"] = "current" if state not in {"EXECUTED"} else "complete"
        return {
            "stage": "ORDER", "status": "READY",
            "story": "M5 break or retest passed; final MT5 order checks are active.",
            "progress": stages,
        }

    current_index = 0
    story = "H4 has no clear direction yet."
    if "H1_" in code:
        current_index, story = 1, "H4 found direction; H1 has not confirmed it."
    elif "M15_" in code or "POI" in code or "CHOCH" in code or "BOS" in code:
        current_index, story = 2, "H4 and H1 agree; M15 has not confirmed a point of interest and structure."
    elif "M5_" in code or "TRIGGER" in code:
        current_index, story = 3, "Higher-timeframe context is present; no live M5 break or retest is ready."
    elif "STRATEGY_SCORE" in code:
        current_index, story = 2, "The context or structure score did not reach the configured level."
    for stage in stages[:current_index]:
        stage["state"] = "complete"
    stages[current_index]["state"] = "current"
    return {"stage": stages[current_index]["key"], "status": "WATCHING", "story": story, "progress": stages}

def _systematic_public_fields(data: dict) -> dict:
    score_breakdown = data.get("score_breakdown") if isinstance(data.get("score_breakdown"), dict) else _json_dict(data.get("score_breakdown"))
    conditions = data.get("conditions") if isinstance(data.get("conditions"), dict) else _json_dict(data.get("conditions"))
    thresholds = score_breakdown.get("thresholds") if isinstance(score_breakdown.get("thresholds"), dict) else {}
    strategy_engine = thresholds.get("strategy_engine") if isinstance(thresholds.get("strategy_engine"), dict) else {}
    systematic_cost = thresholds.get("systematic_cost") if isinstance(thresholds.get("systematic_cost"), dict) else {}
    systematic = thresholds.get("systematic") if isinstance(thresholds.get("systematic"), dict) else {}
    session = systematic.get("session") if isinstance(systematic.get("session"), dict) else {}
    cost = systematic_cost.get("cost") if isinstance(systematic_cost.get("cost"), dict) else {}
    data["conditions"] = conditions
    data["score_breakdown"] = score_breakdown
    display_defaults = _engine_display_defaults(data.get("sym"), data.get("market"))
    data["engine"] = data.get("engine") or score_breakdown.get("engine") or strategy_engine.get("engine") or display_defaults.get("engine") or ""
    data["asset_class"] = data.get("asset_class") or score_breakdown.get("asset_class") or strategy_engine.get("asset_class") or display_defaults.get("asset_class") or ""
    data["strategy_before"] = data.get("strategy_before") or score_breakdown.get("strategy_before") or strategy_engine.get("strategy_before") or ""
    gate_text = str(data.get("gate") or conditions.get("block_reason") or score_breakdown.get("block_reason") or "").lower()
    no_setup_row = gate_text.startswith("no setup") or gate_text.startswith("waiting for scan")
    data["strategy_after"] = "" if no_setup_row else (
        data.get("strategy_after") or score_breakdown.get("strategy_after") or strategy_engine.get("strategy_after") or data.get("strategy") or display_defaults.get("strategy_after") or ""
    )
    data["strategy_status"] = "no_setup" if no_setup_row else (score_breakdown.get("strategy_status") or strategy_engine.get("strategy_status") or "")
    data["score_before"] = data.get("score_before") if data.get("score_before") is not None else score_breakdown.get("score_before", strategy_engine.get("score_before"))
    data["score_after"] = data.get("score_after") if data.get("score_after") is not None else score_breakdown.get("score_after", strategy_engine.get("score_after"))
    data["min_score"] = data.get("min_score") if data.get("min_score") is not None else score_breakdown.get("min_score", strategy_engine.get("min_score"))
    data["decision_trace_id"] = data.get("decision_trace_id") or score_breakdown.get("decision_trace_id") or conditions.get("decision_trace_id") or ""
    data["decision_trace"] = score_breakdown.get("decision_trace") or conditions.get("decision_trace") or []
    if not isinstance(data["decision_trace"], list):
        data["decision_trace"] = []
    data["score_function"] = data.get("score_function") or score_breakdown.get("score_function") or conditions.get("score_function") or ""
    data["score_function_source"] = data.get("score_function_source") or score_breakdown.get("score_function_source") or conditions.get("score_function_source") or ""
    data["adx_function"] = data.get("adx_function") or score_breakdown.get("adx_function") or conditions.get("adx_function") or ""
    data["spread_function"] = data.get("spread_function") or score_breakdown.get("spread_function") or conditions.get("spread_function") or ""
    data["risk_gate_function"] = data.get("risk_gate_function") or score_breakdown.get("risk_gate_function") or "mt5_systematic_engine.PortfolioRisk.can_trade"
    data["final_status"] = data.get("final_status") or score_breakdown.get("final_status") or conditions.get("final_status") or ""
    data["block_reason"] = data.get("block_reason") or score_breakdown.get("block_reason") or conditions.get("block_reason") or ""
    data["blocked_at_step"] = data.get("blocked_at_step") or score_breakdown.get("blocked_at_step") or conditions.get("blocked_at_step") or ""
    data["pending_setup_created"] = bool(score_breakdown.get("pending_setup_created") or conditions.get("pending_setup_created") or False)
    data["systematic_reason"] = data.get("systematic_reason") or conditions.get("systematic_reason") or ""
    data["session"] = data.get("session") or conditions.get("systematic session") or session.get("session") or score_breakdown.get("session_name") or ""
    raw_cost_to_target = data.get("cost_to_target_pct")
    if raw_cost_to_target is None:
        raw_cost_to_target = cost.get("cost_to_target_pct")
    data["cost_to_target_pct"] = None if raw_cost_to_target is None else _safe_float(raw_cost_to_target)
    data["cost_to_target_status"] = cost.get("cost_to_target_status") or ("unavailable" if raw_cost_to_target is None else "calculated")
    data["cost_to_target_reason"] = cost.get("cost_to_target_reason") or ("" if raw_cost_to_target is not None else "target missing")
    data["cost_usd"] = _safe_float(data.get("cost_usd") or cost.get("total_cost_usd"))

    # The live signal row is the canonical current snapshot. Some replacement
    # engine paths store detailed timeframe scores only in score_breakdown, so
    # expose them consistently instead of falling back to older audit events.
    def first_present(*values):
        for value in values:
            if value not in (None, ""):
                return value
        return None

    metric = score_breakdown.get("score_metric") if isinstance(score_breakdown.get("score_metric"), dict) else {}
    score_total = _safe_float(first_present(metric.get("total"), data.get("score_after"), data.get("score")))
    score_minimum = _safe_float(first_present(metric.get("minimum"), data.get("min_score"), 70.0))
    score_pass = (
        score_total is not None
        and score_minimum is not None
        and score_total >= score_minimum
    )
    # A malformed/stale row must never be rendered as actionable merely
    # because an older gate flag survived in storage.
    if not score_pass:
        data["gate_ok"] = 0
        data["block_reason"] = data.get("block_reason") or "STRATEGY_SCORE_BELOW_MINIMUM"
        if str(data.get("final_status") or "").upper() == "READY":
            data["final_status"] = "NOT_QUALIFIED"
    data["side"] = first_present(data.get("side"), data.get("direction"))
    data["h4_score"] = first_present(data.get("h4_score"), score_breakdown.get("h4"), conditions.get("h4"))
    data["h1_score"] = first_present(data.get("h1_score"), data.get("bias_1h_score"), score_breakdown.get("bias_1h"), conditions.get("bias_1h"))
    data["m15_score"] = first_present(data.get("m15_score"), data.get("setup_15m_score"), score_breakdown.get("setup_15m"), conditions.get("setup_15m"))
    data["m5_trigger_score"] = first_present(data.get("m5_trigger_score"), data.get("trigger_5m_score"), score_breakdown.get("trigger_5m"), conditions.get("trigger_5m"))
    data["alignment_score"] = first_present(data.get("alignment_score"), conditions.get("alignment_score"), metric.get("HTF alignment"))
    data["setup_quality_score"] = first_present(data.get("setup_quality_score"), conditions.get("setup_quality_score"), conditions.get("m15_setup_quality"), metric.get("M15 structure/location"))
    data["trigger_score"] = first_present(data.get("trigger_score"), conditions.get("trigger_score"), data.get("m5_trigger_score"), metric.get("M5 trigger quality"))
    data["setup_state"] = str(first_present(
        data.get("setup_state"), data.get("flow_state"), data.get("state"),
        conditions.get("setup_state"), conditions.get("flow_state"), ""
    ) or "")
    data["waiting_for"] = str(first_present(data.get("waiting_for"), conditions.get("waiting_for"), "") or "")
    data["m15_setup_type"] = first_present(data.get("m15_setup_type"), conditions.get("m15_setup_type"), data.get("setup_type"))
    data["m15_poi_types"] = first_present(data.get("m15_poi_types"), conditions.get("m15_poi_types"), [])
    data["m15_choch"] = first_present(data.get("m15_choch"), conditions.get("m15_choch"), False)
    data["m15_bos"] = first_present(data.get("m15_bos"), conditions.get("m15_bos"), False)
    data["m15_retracement"] = first_present(data.get("m15_retracement"), conditions.get("m15_retracement"), False)
    data["m5_trigger_type"] = first_present(data.get("m5_trigger_type"), conditions.get("m5_trigger_type"), "")
    data["m5_trigger_result"] = first_present(data.get("m5_trigger_result"), conditions.get("m5_trigger_result"), "")
    data["final_eligibility"] = first_present(data.get("final_eligibility"), conditions.get("final_eligibility"), data.get("final_status"))
    data["execution_status"] = first_present(data.get("execution_status"), conditions.get("execution_status"), data.get("final_status"))
    data["rejection_stage"] = first_present(data.get("rejection_stage"), conditions.get("rejection_stage"), data.get("blocked_at_step"))
    data["rejection_reason"] = first_present(data.get("rejection_reason"), conditions.get("rejection_reason"), data.get("block_reason"))
    data["plan_alignment"] = first_present(data.get("plan_alignment"), conditions.get("plan_alignment"))
    data["learning_mode"] = first_present(data.get("learning_mode"), conditions.get("learning_mode")) or "SHADOW_ONLY"
    data["m1_role"] = "not_required"
    data["m1_status"] = "NOT_REQUIRED"
    data["reason"] = first_present(data.get("reason"), data.get("systematic_reason"), data.get("gate"), data.get("block_reason")) or ""
    raw_public_reason = first_present(data.get("systematic_reason"), data.get("gate"), data.get("block_reason"), data.get("reason"))
    direct_m5 = bool(
        bool(_safe_int(data.get("gate_ok"))) and score_pass and (
            data.get("setup_state") in {"CONFIRMED", "M5_TRIGGERED", "EXECUTION_PENDING", "PASS"}
            or str(conditions.get("confirmation_source") or "").upper() in {"M5_TRIGGER", "M5_BREAK_RETEST"}
        )
    )
    scan_view = _public_scan_story(
        raw_public_reason,
        gate_ok=bool(_safe_int(data.get("gate_ok"))),
        direction=data.get("side"),
        pending_setup=data.get("pending_setup_created"),
        m1_status=data.get("m1_status"),
        direct_m5=direct_m5,
        setup_state=data.get("setup_state"),
        waiting_for=data.get("waiting_for"),
    )
    data["scan_stage"] = scan_view["stage"]
    data["scan_status"] = scan_view["status"]
    data["scan_story"] = scan_view["story"]
    data["scan_progress"] = scan_view["progress"]
    bar_times = score_breakdown.get("bar_times") if isinstance(score_breakdown.get("bar_times"), dict) else {}
    data["score_stage"] = score_breakdown.get("score_stage") or scan_view["stage"]
    data["scan_trigger"] = score_breakdown.get("scan_trigger") or "COMPLETED_M5"
    data["scan_cycle_at"] = score_breakdown.get("scan_cycle_at") or data.get("updated_at")
    data["scan_candle_times"] = {key: value for key, value in bar_times.items() if value}
    public_reason = _simple_public_reason(raw_public_reason)
    data["public_metric_name"] = "Cipher FX Score"
    data["public_mode"] = "Shadow"
    data["public_reason"] = public_reason
    data["reason"] = public_reason
    data["gate"] = public_reason
    data["final_status"] = "READY" if bool(_safe_int(data.get("gate_ok"))) else public_reason
    data["status"] = data["final_status"]
    data["score_metric"] = {
        "version": first_present(metric.get("version"), "cipher_fx_poi_v1"),
        "total": first_present(metric.get("total"), data.get("score")),
        "minimum": first_present(metric.get("minimum"), data.get("min_score")),
        "strong": first_present(metric.get("strong"), 85.0),
        "band": first_present(metric.get("band"), data.get("score_band"), score_band(data.get("score"))),
        "stage": first_present(metric.get("stage"), data.get("score_stage"), data.get("scan_stage")),
        "enforced": bool(metric.get("enforced", True)),
        "probability": bool(metric.get("probability", False)),
        "components": metric.get("components") if isinstance(metric.get("components"), dict) else {},
        "maxima": metric.get("maxima") if isinstance(metric.get("maxima"), dict) else {},
    }
    for hidden in ("h4_bias", "h1_bias", "m15_bias", "m5_trigger_side", "m1_confirmation_side"):
        data[hidden] = "" if hidden.endswith("_bias") or hidden.endswith("_side") else data.get(hidden, "")
    return data


def _date_between(value: str, start: str, end: str) -> bool:
    return bool(value) and start <= value <= end


def _row_score_passes(row: dict) -> bool:
    metric = row.get("score_metric") if isinstance(row.get("score_metric"), dict) else {}
    total = _safe_float(metric.get("total") if metric.get("total") is not None else row.get("score"))
    minimum = _safe_float(metric.get("minimum") if metric.get("minimum") is not None else row.get("min_score"))
    if minimum is None:
        minimum = 70.0
    return total is not None and total >= minimum


def _public_signal_row(row) -> dict:
    data = _systematic_public_fields(dict(row))
    updated = date_detail_for(data.get("updated_at"))
    _add_date_prefix(data, "updated", updated)
    raw_score = data.get("score")
    data["score"] = None if raw_score in (None, "") else _safe_float(raw_score)
    data["gate_ok"] = bool(_safe_int(data.get("gate_ok")))
    data["has_direction"] = bool(data.get("direction"))
    return data


def _public_setup_event(row) -> dict:
    data = _systematic_public_fields(dict(row))
    created = date_detail_for(data.get("created_at"))
    _add_date_prefix(data, "created", created)
    data["final_score"] = _safe_float(data.get("final_score"))
    data["gate_ok"] = str(data.get("status") or "").lower() == "actionable"
    data["reason"] = data.get("systematic_reason") or data.get("exit_reason") or data.get("status") or ""
    return data


def _score_band_table(rows, score_key="final_score") -> list[dict]:
    out = []
    for _, _, label in SCORE_BANDS:
        band_rows = [r for r in rows if (r.get("score_band") or score_band(r.get(score_key))) == label]
        scores = [_safe_float(r.get(score_key)) for r in band_rows]
        actionable = [r for r in band_rows if str(r.get("status") or "").lower() == "actionable" or r.get("gate_ok") is True]
        out.append({
            "score_band": label,
            "count": len(band_rows),
            "actionable": len(actionable),
            "avg_score": round(sum(scores) / len(scores), 1) if scores else 0,
        })
    return out



def _compact_scanner_row(row: dict) -> dict:
    keys = (
        "sym", "market", "direction", "side", "score", "score_band", "gate",
        "gate_ok", "has_direction", "reason", "public_reason", "status",
        "engine", "asset_class", "strategy", "strategy_after", "strategy_status",
        "updated_at", "updated_label", "scan_stage", "scan_status", "scan_story",
        "scan_progress", "scan_trigger", "scan_cycle_at", "scan_candle_times",
        "market_session", "signal_age_seconds", "pending_setup_created", "setup_state", "waiting_for",
        "m1_status", "h4_score", "h1_score", "m15_score", "m5_trigger_score",
        "m15_setup_type", "m15_poi_types", "m15_choch", "m15_bos", "m15_retracement",
        "m5_trigger_type", "m5_trigger_result", "final_status", "public_metric_name",
        "public_mode", "score_metric",
    )
    compact = {key: row.get(key) for key in keys if key in row}
    if isinstance(compact.get("score_metric"), dict):
        metric = compact["score_metric"]
        compact["score_metric"] = {
            "version": metric.get("version"),
            "total": metric.get("total"),
            "minimum": metric.get("minimum"),
            "strong": metric.get("strong"),
            "band": metric.get("band"),
            "stage": metric.get("stage"),
            "enforced": metric.get("enforced"),
            "probability": metric.get("probability"),
            "components": metric.get("components") if isinstance(metric.get("components"), dict) else {},
        }
    return compact


def _compact_scanner_event(row: dict) -> dict:
    keys = (
        "sym", "market", "direction", "side", "final_score", "score",
        "score_band", "status", "reason", "systematic_reason", "engine",
        "asset_class", "strategy", "created_at", "created_label", "trade_date",
        "gate_ok", "scan_stage", "scan_story", "scan_progress",
    )
    return {key: row.get(key) for key in keys if key in row}

def read_signal_scanner():
    today = trading_today()
    week_start, week_end = trading_week_range(today)
    signal_cutoff = (datetime.now() - timedelta(minutes=7)).isoformat()
    conn = get_conn()
    signal_rows = conn.execute(
        """
        SELECT * FROM signals
        WHERE updated_at >= ?
        ORDER BY updated_at DESC
        """,
        (signal_cutoff,),
    ).fetchall()
    setup_rows = conn.execute(
        """
        SELECT *
        FROM setup_events
        WHERE trade_date >= ? AND trade_date <= ?
        ORDER BY created_at DESC
        LIMIT 500
        """,
        (week_start, week_end),
    ).fetchall()
    rejected_rows = conn.execute(
        """
        SELECT sym, market, direction, strategy, sig_mode, score, score_band,
               rejected_reason, opened_at, trade_date, status
        FROM trades
        WHERE status='rejected' AND trade_date >= ? AND trade_date <= ?
        ORDER BY opened_at DESC
        LIMIT 120
        """,
        (week_start, week_end),
    ).fetchall()
    status_rows = conn.execute(
        """
        SELECT key, value
        FROM bot_status
        WHERE key IN ('mt5_symbol_count', 'priority_scan_symbol_count')
        """
    ).fetchall()
    conn.close()

    scanner_status = {row["key"]: row["value"] for row in status_rows}
    configured_symbol_count = _safe_int(scanner_status.get("mt5_symbol_count")) or _safe_int(scanner_status.get("priority_scan_symbol_count"))
    signals = _filter_latest_priority_signals([_public_signal_row(row) for row in signal_rows])
    signals = [row for row in signals if str(row.get("gate") or "").strip().lower() not in {"market_window_closed", "markets closed"}]
    # A closed session is not a current signal. Historical data remains in
    # SQLite and the history views, but it is excluded from this live list.
    signals = [row for row in signals if _symbol_market_is_open(row.get("sym"))]
    for row in signals:
        row["market_session"] = row.get("market_session") or _market_session_for_symbol(row.get("sym"))
        updated = _parse_dt(row.get("updated_at"))
        row["signal_age_seconds"] = round(max(0.0, (datetime.now() - updated).total_seconds()), 1) if updated else None
    observed_symbols = {str(row.get("sym") or "").upper() for row in signals}
    for sym in _priority_symbols_from_env():
        if not _symbol_market_is_open(sym):
            continue
        if sym in observed_symbols:
            continue
        session_id = _market_session_for_symbol(sym)
        signals.append({
            "sym": sym,
            "market": "index_cfd_us" if session_id == "us_new_york" else "index_cfd_asia" if session_id == "asia_tokyo" else "mt5",
            "direction": "",
            "side": "",
            "score": None,
            "score_band": "",
            "gate": "no_recent_scan",
            "gate_ok": None,
            "has_direction": False,
            "reason": "no_recent_scan",
            "public_reason": "Waiting for live scan",
            "status": "WATCHING",
            "engine": "INDEX_SESSION_BREAKOUT" if session_id else "",
            "asset_class": "index" if session_id else "",
            "strategy": "",
            "updated_at": "",
            "updated_label": "",
            "scan_stage": "SESSION",
            "scan_status": "WATCHING",
            "scan_story": "Configured MT5 symbol; waiting for its next live scan event.",
            "scan_progress": [
                {"key": "H4", "label": "Direction", "state": "inactive"},
                {"key": "H1", "label": "Confirm", "state": "inactive"},
                {"key": "M15", "label": "POI + Structure", "state": "inactive"},
                {"key": "M5", "label": "Break / Retest", "state": "inactive"},
                {"key": "ORDER", "label": "Order", "state": "inactive"},
            ],
            "scan_trigger": "WAITING_FOR_SESSION",
            "scan_cycle_at": "",
            "scan_candle_times": {},
            "market_session": session_id,
            "pending_setup_created": False,
            "m1_status": "",
            "public_metric_name": "Cipher FX Score",
            "public_mode": "Shadow",
            "score_metric": {"version": "cipher_fx_score", "total": None, "band": ""},
        })
    events = [_public_setup_event(row) for row in setup_rows]
    today_events = [row for row in events if row.get("trade_date") == today]
    week_events = [row for row in events if _date_between(row.get("trade_date"), week_start, week_end)]
    today_signals = [row for row in signals if trade_date_for(row.get("updated_at")) == today]
    current_actionable = [row for row in signals if row.get("gate_ok") and row.get("direction") and _row_score_passes(row)]
    current_blocked = [
        row for row in signals
        if row.get("gate_ok") is False and row.get("status") not in {"WATCHING", "CLOSED"}
    ]
    current_waiting = [row for row in signals if str(row.get("setup_state") or "").upper() in {"ARMED", "WAITING_M5"} or str(row.get("waiting_for") or "").upper() == "M5_BREAK_OR_RETEST"]
    score_source_today = today_events or [row for row in today_signals if row.get("direction")]
    score_source_week = week_events or [row for row in signals if row.get("direction")]

    rejected = []
    for row in rejected_rows:
        data = dict(row)
        opened = date_detail_for(data.get("opened_at"))
        _add_date_prefix(data, "opened", opened)
        data["score"] = _safe_float(data.get("score"))
        data["reason"] = data.get("rejected_reason") or "order rejected"
        rejected.append(data)

    blockers = []
    for row in current_blocked[:20]:
        blockers.append({
            "sym": row.get("sym"),
            "market": row.get("market"),
            "direction": row.get("direction"),
            "score": row.get("score"),
            "score_band": row.get("score_band"),
            "reason": row.get("public_reason") or row.get("reason") or "No clear setup",
            "engine": row.get("engine"),
            "asset_class": row.get("asset_class"),
            "created_label": row.get("updated_label"),
            "created_at": row.get("updated_at"),
            "source": "scanner",
        })
    for row in [item for item in rejected if item.get("trade_date") == today][:8]:
        blockers.append({
            "sym": row.get("sym"),
            "market": row.get("market"),
            "direction": row.get("direction"),
            "score": row.get("score"),
            "score_band": row.get("score_band"),
            "reason": row.get("reason"),
            "created_label": row.get("opened_label"),
            "created_at": row.get("opened_at"),
            "source": "order",
        })

    def event_actionable_count(rows):
        return sum(1 for row in rows if str(row.get("status") or "").lower() == "actionable")

    actionable_today = event_actionable_count(today_events)
    actionable_week = event_actionable_count(week_events)
    blocked_today = max(0, len(today_events) - actionable_today)
    blocked_week = max(0, len(week_events) - actionable_week)

    today_scores = [_safe_float(row.get("final_score", row.get("score"))) for row in score_source_today]
    week_scores = [_safe_float(row.get("final_score", row.get("score"))) for row in score_source_week]
    latest_signal_at = max((row.get("updated_at") for row in signals if row.get("updated_at")), default="")
    latest_signal_detail = date_detail_for(latest_signal_at)

    return {
        "summary": {
            "today": today,
            "week_start": week_start,
            "week_end": week_end,
            "latest_signal_at": latest_signal_at,
            "latest_signal_label": latest_signal_detail.get("label", ""),
            "scanned_symbols_today": len({row.get("sym") for row in today_signals if row.get("sym")}),
            "tracked_symbols": configured_symbol_count or len({row.get("sym") for row in signals if row.get("sym")}),
            "current_universe_rows": len(signals),
            "missing_recent_symbols": [sym for sym in _priority_symbols_from_env() if _symbol_market_is_open(sym) and sym not in {str(row.get("sym") or "").upper() for row in signals if row.get("updated_at")}],
            "current_actionable": len(current_actionable),
            "current_blocked": len(current_blocked),
            "current_waiting": len(current_waiting),
            "signals_today": actionable_today,
            "signals_week": actionable_week,
            "setups_today": len(today_events),
            "setups_week": len(week_events),
            "blocked_today": blocked_today,
            "blocked_week": blocked_week,
            "actionable_today": actionable_today,
            "actionable_week": actionable_week,
            "rejected_today": sum(1 for row in rejected if row.get("trade_date") == today),
            "rejected_week": len(rejected),
            "avg_score_today": round(sum(today_scores) / len(today_scores), 1) if today_scores else 0,
            "avg_score_week": round(sum(week_scores) / len(week_scores), 1) if week_scores else 0,
        },
        "score_bands_today": _score_band_table(score_source_today),
        "score_bands_week": _score_band_table(score_source_week),
        "current_signals": [_compact_scanner_row(row) for row in signals[:40]],
        "recent_events": [_compact_scanner_event(row) for row in events[:40]],
        "blockers": blockers[:30],
        "rejected_orders": rejected[:30],
        "public_latest_label": "Latest Signals Overview",
    }


def read_replay_trades(sym=None, limit=300):
    conn = get_conn()
    if sym:
        rows = conn.execute(
            'SELECT * FROM trades WHERE sym=? ORDER BY datetime(replace(COALESCE(opened_at, trade_date), "T", " ")) ASC LIMIT ?',
            (str(sym).upper(), max(1, min(int(limit or 300), 1000))),
        ).fetchall()
    else:
        rows = conn.execute(
            'SELECT * FROM trades ORDER BY datetime(replace(COALESCE(opened_at, trade_date), "T", " ")) ASC LIMIT ?',
            (max(1, min(int(limit or 300), 1000)),),
        ).fetchall()
    conn.close()
    return [enrich_trade_dates(dict(row)) for row in rows]


def read_trades(limit=100, today_only=False):
    conn = get_conn()
    if today_only:
        today = trading_today()
        rows = conn.execute(
            """
            SELECT * FROM trades
            WHERE closed_at IS NOT NULL AND trade_date = ?
            ORDER BY closed_at DESC LIMIT ?
            """,
            (today, limit)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM trades WHERE closed_at IS NOT NULL ORDER BY closed_at DESC LIMIT ?",
            (limit,)
        ).fetchall()
    conn.close()
    return [enrich_trade_dates(dict(r)) for r in rows]


def read_todays_pnl():
    """Sum realized P&L from trades whose trade_date is today's trading date."""
    today = trading_today()
    conn = get_conn()
    closed = conn.execute(
        "SELECT realized FROM trades WHERE closed_at IS NOT NULL AND trade_date = ?",
        (today,)
    ).fetchall()
    conn.close()
    realized_sum = sum(r["realized"] for r in closed if r["realized"] is not None)
    return round(realized_sum, 2)

def read_broker_day_pnl():
    """Sum realized P&L by MT5 broker close day, matching the EA reset boundary."""
    today = broker_trading_today()
    conn = get_conn()
    rows = conn.execute(
        "SELECT realized, closed_at, trade_date FROM trades WHERE closed_at IS NOT NULL"
    ).fetchall()
    conn.close()
    total = sum(
        float(row["realized"])
        for row in rows
        if row["realized"] is not None
        and broker_trade_date_for(row["closed_at"], fallback=row["trade_date"]) == today
    )
    return round(total, 2)



def trading_month_range(today_value: str | None = None) -> tuple[str, str]:
    if today_value:
        today = datetime.fromisoformat(today_value).date()
    else:
        today = datetime.fromisoformat(trading_today()).date()
    month_start = today.replace(day=1)
    if month_start.month == 12:
        next_month = month_start.replace(year=month_start.year + 1, month=1)
    else:
        next_month = month_start.replace(month=month_start.month + 1)
    month_end = next_month - timedelta(days=1)
    return month_start.isoformat(), month_end.isoformat()


def _deal_period_range(period: str) -> tuple[str, str, str]:
    clean = str(period or "today").strip().lower()
    today = trading_today()
    if clean in {"today", "daily", "day"}:
        return "today", today, today
    if clean in {"week", "weekly"}:
        start, end = trading_week_range(today)
        return "week", start, end
    if clean in {"month", "monthly"}:
        start, end = trading_month_range(today)
        return "month", start, end
    if clean in {"all", "history"}:
        return "all", "", ""
    return "today", today, today


def _deal_row_payload(row: sqlite3.Row) -> dict:
    data = enrich_trade_dates(dict(row))
    realized = float(data.get("realized") or 0.0)
    data["pnl"] = round(realized, 2)
    data["profit"] = round(realized, 2)
    data["volume"] = float(data.get("qty") or 0.0)
    data["side"] = str(data.get("direction") or "").lower()
    data["symbol"] = data.get("sym") or data.get("symbol") or ""
    return data


def _deals_summary(rows: list[dict], period: str, start: str, end: str) -> dict:
    pnls = [float(row.get("realized") or row.get("pnl") or 0.0) for row in rows]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    breakeven = [p for p in pnls if abs(p) <= 0.000001]
    by_symbol: dict[str, dict] = {}
    for row in rows:
        symbol = str(row.get("sym") or row.get("symbol") or "MT5")
        item = by_symbol.setdefault(symbol, {"symbol": symbol, "trades": 0, "pnl": 0.0, "wins": 0, "losses": 0})
        pnl = float(row.get("realized") or row.get("pnl") or 0.0)
        item["trades"] += 1
        item["pnl"] += pnl
        if pnl > 0:
            item["wins"] += 1
        elif pnl < 0:
            item["losses"] += 1
    symbol_rows = []
    for item in by_symbol.values():
        item["pnl"] = round(float(item["pnl"]), 2)
        symbol_rows.append(item)
    symbol_rows.sort(key=lambda item: abs(float(item["pnl"])), reverse=True)
    total = len(rows)
    gross_profit = sum(wins)
    gross_loss = sum(losses)
    return {
        "period": period,
        "start_date": start,
        "end_date": end,
        "total_trades": total,
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": len(breakeven),
        "win_rate": round((len(wins) / total * 100.0), 1) if total else 0.0,
        "pnl": round(sum(pnls), 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "profit_factor": round(gross_profit / abs(gross_loss), 2) if gross_loss < 0 else (round(gross_profit, 2) if gross_profit > 0 else 0.0),
        "best_trade": round(max(pnls), 2) if pnls else 0.0,
        "worst_trade": round(min(pnls), 2) if pnls else 0.0,
        "by_symbol": symbol_rows,
    }


def sync_mt5_trade_exports(export_root: str | Path = "/opt/cipherfx_mt5/state/systematic_exports", day: str | None = None) -> dict:
    """Rewrite trade CSVs from SQLite so exports cannot drift from the authoritative DB."""
    day = day or trading_today()
    root = Path(export_root) / str(day)
    root.mkdir(parents=True, exist_ok=True)
    conn = get_conn()
    rows = [dict(row) for row in conn.execute("SELECT * FROM trades WHERE trade_date=? ORDER BY datetime(replace(COALESCE(opened_at, \"\"), \"T\", \" \"))", (day,)).fetchall()]
    conn.close()
    fields = ["timestamp", "symbol", "engine", "strategy", "side", "score", "status",
              "block_reason", "spread", "slippage", "cost", "R_result", "profit_usd",
              "session", "population", "regime"]
    def export_row(row: dict) -> dict:
        breakdown = row.get("score_breakdown") or {}
        if isinstance(breakdown, str):
            try:
                breakdown = json.loads(breakdown)
            except Exception:
                breakdown = {}
        if not isinstance(breakdown, dict):
            breakdown = {}
        return {
            "timestamp": row.get("closed_at") or row.get("opened_at") or "",
            "symbol": row.get("sym") or "",
            "engine": breakdown.get("engine") or "",
            "strategy": row.get("strategy") or breakdown.get("strategy_after") or "",
            "side": row.get("direction") or "",
            "score": row.get("score") or "",
            "status": row.get("status") or "",
            "block_reason": row.get("exit_reason") or row.get("rejected_reason") or "",
            "spread": row.get("spread_execution") or row.get("spread_signal") or "",
            "slippage": row.get("slippage") or "",
            "cost": breakdown.get("cost_to_target") or "",
            "R_result": row.get("result_r") if row.get("result_r") is not None else "",
            "profit_usd": row.get("realized") if row.get("realized") is not None else "",
            "session": row.get("session") or "unknown",
            "population": row.get("population") or "direct_strategy",
            "regime": breakdown.get("regime") or "",
        }
    closed = [row for row in rows if row.get("closed_at")]
    executions = [row for row in rows if row.get("opened_at")]
    counts = {}
    for filename, payload in (("closed_trades.csv", closed), ("executions.csv", executions)):
        path = root / filename
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(export_row(row) for row in payload)
        counts[filename] = len(payload)
    return {"day": day, "sqlite_trade_count": len(rows), **counts, "reconciled": True, "generated_at": datetime.now(timezone.utc).isoformat()}


def read_deals_period(period: str = "today", limit: int = 200) -> dict:
    clean, start, end = _deal_period_range(period)
    count = max(1, min(int(limit or 200), 1000))
    conn = get_conn()
    if clean == "all":
        rows = conn.execute(
            "SELECT * FROM trades WHERE closed_at IS NOT NULL ORDER BY datetime(replace(closed_at, 'T', ' ')) DESC LIMIT ?",
            (count,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT * FROM trades
            WHERE closed_at IS NOT NULL AND trade_date >= ? AND trade_date <= ?
            ORDER BY datetime(replace(closed_at, 'T', ' ')) DESC LIMIT ?
            """,
            (start, end, count),
        ).fetchall()
    conn.close()
    deals = [_deal_row_payload(row) for row in rows]
    summary = _deals_summary(deals, clean, start, end)
    public_keys = (
        "id", "trade_id", "ticket", "sym", "symbol", "direction", "side",
        "outcome", "status", "qty", "volume", "entry_price", "entry",
        "exit_price", "exit", "price", "realized", "pnl", "profit",
        "opened_at", "closed_at", "trade_date", "opened_date", "closed_date",
        "opened_day", "closed_day", "opened_month_label", "trade_month_label",
        "closed_month_label", "opened_label", "closed_label", "trade_label",
        "duration_label",
    )
    public_deals = [{key: row.get(key) for key in public_keys if key in row} for row in deals]
    return {
        "period": clean,
        "start_date": start,
        "end_date": end,
        "summary": summary,
        "deals": public_deals,
    }


def _runtime_open_trade_ids():
    path = os.getenv("MT5_STATE_FILE", "")
    if not path:
        return None
    state_path = Path(path)
    if not state_path.exists():
        return None
    try:
        payload = json.loads(state_path.read_text() or "{}")
    except Exception:
        return None
    open_trades = payload.get("open_trades") if isinstance(payload, dict) else None
    if not isinstance(open_trades, dict):
        return set()
    return {
        str(meta.get("trade_id") or "")
        for meta in open_trades.values()
        if isinstance(meta, dict) and meta.get("trade_id")
    }


def read_todays_trade_count():
    """Count accepted bot entries in the current MT5 broker day."""
    today = broker_trading_today()
    runtime_open_trade_ids = _runtime_open_trade_ids()
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT trade_id, opened_at, trade_date, status, closed_at
        FROM trades
        WHERE COALESCE(status, '') NOT IN ('rejected', 'cancelled', 'duplicate', 'reconciled_duplicate')
        """
    ).fetchall()
    conn.close()
    count = 0
    for row in rows:
        trade_id = str(row["trade_id"] or "")
        if trade_id.startswith("mt5_adopted_"):
            continue
        status = str(row["status"] or "").lower()
        if (
            status == "live"
            and not row["closed_at"]
            and runtime_open_trade_ids is not None
            and trade_id not in runtime_open_trade_ids
        ):
            continue
        if broker_trade_date_for(row["opened_at"], fallback=row["trade_date"]) == today:
            count += 1
    return count


def _build_stats(rows):
    rows = [r for r in rows if str(r["outcome"] or "").lower() in {"win", "loss", "breakeven"}]
    realized_rows = [float(r["realized"] or 0) for r in rows]
    wins   = [float(r["realized"] or 0) for r in rows if r["outcome"] == "win"]
    losses = [float(r["realized"] or 0) for r in rows if r["outcome"] == "loss"]
    total  = len(rows)
    return {
        "total_trades": total,
        "wins":         len(wins),
        "losses":       len(losses),
        "win_rate":     round(len(wins) / total * 100, 1) if total else 0,
        "total_pnl":    round(sum(realized_rows), 2),
        "avg_win":      round(sum(wins) / len(wins), 2) if wins else 0,
        "avg_loss":     round(sum(losses) / len(losses), 2) if losses else 0,
    }


def _build_extended_stats(rows):
    base = _build_stats(rows)
    pnls = [float(r["realized"] or 0) for r in rows]
    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss = sum(p for p in pnls if p < 0)
    ordered = sorted(
        rows,
        key=lambda r: (
            str(r["closed_at"] or ""),
            str(r["trade_date"] or ""),
        ),
    )

    streak_type = None
    streak_count = 0
    for row in reversed(ordered):
        outcome = row["outcome"]
        if outcome not in ("win", "loss"):
            continue
        if streak_type is None:
            streak_type = outcome
            streak_count = 1
            continue
        if outcome == streak_type:
            streak_count += 1
            continue
        break

    return {
        **base,
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "best_trade": round(max(pnls), 2) if pnls else 0,
        "worst_trade": round(min(pnls), 2) if pnls else 0,
        "profit_factor": round(gross_profit / abs(gross_loss), 2) if gross_loss < 0 else (round(gross_profit, 2) if gross_profit > 0 else 0),
        "current_streak": {
            "type": streak_type,
            "count": streak_count,
        },
    }


def read_stats():
    conn = get_conn()
    rows = conn.execute(
        "SELECT realized, outcome FROM trades WHERE closed_at IS NOT NULL"
    ).fetchall()
    conn.close()
    return _build_stats(rows)


def read_daily_stats():
    today = trading_today()
    conn  = get_conn()
    rows  = conn.execute(
        "SELECT realized, outcome FROM trades WHERE closed_at IS NOT NULL AND trade_date = ?",
        (today,)
    ).fetchall()
    conn.close()
    return _build_stats(rows)


def read_weekly_stats():
    week_start, week_end = trading_week_range()
    conn       = get_conn()
    rows       = conn.execute(
        """
        SELECT realized, outcome FROM trades
        WHERE closed_at IS NOT NULL AND trade_date >= ? AND trade_date <= ?
        """,
        (week_start, week_end)
    ).fetchall()
    conn.close()
    return _build_stats(rows)


def read_monthly_stats():
    month_start, month_end = trading_month_range()
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT realized, outcome FROM trades
        WHERE closed_at IS NOT NULL AND trade_date >= ? AND trade_date <= ?
        """,
        (month_start, month_end)
    ).fetchall()
    conn.close()
    return _build_stats(rows)


def read_performance_summary():
    today = trading_today()
    week_start, week_end = trading_week_range(today)
    month_start, month_end = trading_month_range(today)
    conn = get_conn()
    all_rows = conn.execute(
        """
        SELECT realized, outcome, closed_at, trade_date
        FROM trades
        WHERE closed_at IS NOT NULL
        """
    ).fetchall()
    day_rows = conn.execute(
        """
        SELECT realized, outcome, closed_at, trade_date
        FROM trades
        WHERE closed_at IS NOT NULL AND trade_date = ?
        """,
        (today,)
    ).fetchall()
    week_rows = conn.execute(
        """
        SELECT realized, outcome, closed_at, trade_date
        FROM trades
        WHERE closed_at IS NOT NULL AND trade_date >= ? AND trade_date <= ?
        """,
        (week_start, week_end)
    ).fetchall()
    month_rows = conn.execute(
        """
        SELECT realized, outcome, closed_at, trade_date
        FROM trades
        WHERE closed_at IS NOT NULL AND trade_date >= ? AND trade_date <= ?
        """,
        (month_start, month_end)
    ).fetchall()
    conn.close()
    return {
        "today": _build_extended_stats(day_rows),
        "week": _build_extended_stats(week_rows),
        "month": _build_extended_stats(month_rows),
        "all_time": _build_extended_stats(all_rows),
        "today_date": today,
        "week_start": week_start,
        "week_end": week_end,
        "month_start": month_start,
        "month_end": month_end,
    }


def _fetch_trade_analytics_rows():
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT *
        FROM trades
        WHERE closed_at IS NOT NULL
           OR status IN ('rejected', 'cancelled')
        ORDER BY COALESCE(closed_at, opened_at) ASC
        """
    ).fetchall()
    conn.close()
    out = []
    for row in rows:
        d = dict(row)
        try:
            d["score_breakdown"] = json.loads(d.get("score_breakdown") or "{}")
        except Exception:
            d["score_breakdown"] = {}
        out.append(d)
    return out


def _median(values):
    vals = sorted(float(v) for v in values)
    if not vals:
        return 0.0
    n = len(vals)
    mid = n // 2
    if n % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2


def _max_drawdown_from_r(rows):
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for row in rows:
        equity += float(row.get("result_r") or 0.0)
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    return abs(max_dd)


def _trade_population(row: dict) -> str:
    raw = str(row.get("population") or "").strip().lower()
    if raw:
        return raw
    mode = str(row.get("sig_mode") or "").lower()
    strategy = str(row.get("strategy") or "").lower()
    if "adopt" in mode or "manual" in mode or "import" in mode:
        return "adopted_imported"
    if "pyramid" in mode or "pyramid" in strategy:
        return "profit_pyramid"
    if "recovery" in mode or "management" in mode:
        return "recovery_management"
    return "direct_strategy"


def _analytics_summary(rows):
    closed = [r for r in rows if r.get("outcome") in ("win", "loss", "breakeven")]
    rs = [float(r.get("result_r") or 0.0) for r in closed]
    wins = [r for r in closed if r.get("outcome") == "win"]
    losses = [r for r in closed if r.get("outcome") == "loss"]
    gross_profit = sum(float(r.get("result_r") or 0.0) for r in wins)
    gross_loss = sum(float(r.get("result_r") or 0.0) for r in losses)
    realized = [float(r.get("result_ccy") if r.get("result_ccy") is not None else r.get("realized") or 0.0) for r in closed]
    costs = [float(r.get("commission") or 0.0) + abs(float(r.get("slippage") or 0.0)) for r in closed]
    return {
        "trades": len(closed),
        "total_trades": len(closed),
        "win_rate": round((len(wins) / len(closed) * 100), 1) if closed else 0.0,
        "avg_r": round(sum(rs) / len(rs), 4) if rs else 0.0,
        "median_r": round(_median(rs), 4) if rs else 0.0,
        "profit_factor": round(gross_profit / abs(gross_loss), 3) if gross_loss < 0 else (round(gross_profit, 3) if gross_profit > 0 else 0.0),
        "expectancy": round(sum(rs) / len(rs), 4) if rs else 0.0,
        "max_drawdown": round(_max_drawdown_from_r(closed), 4),
        "average_spread": round(sum(float(r.get("spread_execution") or r.get("spread_signal") or 0.0) for r in closed) / len(closed), 6) if closed else 0.0,
        "average_slippage": round(sum(abs(float(r.get("slippage") or 0.0)) for r in closed) / len(closed), 6) if closed else 0.0,
        "commission_impact": round(sum(float(r.get("commission") or 0.0) for r in closed), 4),
        "net_pnl_after_costs": round(sum(realized), 4),
        "gross_pnl": round(sum(realized) + sum(float(r.get("commission") or 0.0) for r in closed), 4),
        "enabled": True,
    }


def _group_analytics(rows, key_fn):
    grouped = {}
    for row in rows:
        key = key_fn(row)
        grouped.setdefault(key, []).append(row)
    return {key: _analytics_summary(group) for key, group in grouped.items()}


def _is_scored_trade_row(row):
    strategy = str(row.get("strategy") or "").strip().upper()
    if strategy == "IBKR_IMPORT":
        return False
    score = row.get("score")
    if score is not None:
        try:
            if float(score) > 0:
                return True
        except Exception:
            pass
    for key in (
        "score_band",
        "score_breakdown",
        "bias_1h_score",
        "setup_15m_score",
        "trigger_5m_score",
        "execution_1m_score",
        "smc_score",
        "risk_reward_score",
        "session_score",
        "result_r",
        "execution_1m_result",
    ):
        value = row.get(key)
        if value not in (None, "", "{}", "null"):
            return True
    return False


def read_trade_analytics():
    rows = _fetch_trade_analytics_rows()
    closed = [r for r in rows if r.get("outcome") in ("win", "loss", "breakeven") and _is_scored_trade_row(r)]
    score_bands = _group_analytics(closed, lambda r: r.get("score_band") or score_band(r.get("score")))
    components = {
        "bias_1h_score": _group_analytics(closed, lambda r: int(float(r.get("bias_1h_score") or 0) // 10) * 10),
        "setup_15m_score": _group_analytics(closed, lambda r: int(float(r.get("setup_15m_score") or 0) // 10) * 10),
        "trigger_5m_score": _group_analytics(closed, lambda r: int(float(r.get("trigger_5m_score") or 0) // 10) * 10),
        "execution_1m_score": _group_analytics(closed, lambda r: int(float(r.get("execution_1m_score") or 0) // 10) * 10),
        "smc_score": _group_analytics(closed, lambda r: int(float(r.get("smc_score") or 0) // 10) * 10),
        "risk_reward_score": _group_analytics(closed, lambda r: int(float(r.get("risk_reward_score") or 0) // 10) * 10),
        "session_score": _group_analytics(closed, lambda r: int(float(r.get("session_score") or 0) // 10) * 10),
    }
    comparisons = {
        "entry_path": _group_analytics(closed, lambda r: r.get("comparison_bucket") or ("5m+1m" if (r.get("execution_1m_result") or "").startswith("confirmed") else "5m_direct")),
        "population": _group_analytics(closed, _trade_population),
        "smc_confirmation": _group_analytics(closed, lambda r: "smc_confirmed" if int(r.get("smc_confirmation") or 0) else "smc_not_confirmed"),
        "score_band": score_bands,
        "strategy_module": _group_analytics(closed, lambda r: r.get("strategy") or "unknown"),
        "symbol": _group_analytics(closed, lambda r: r.get("sym") or "unknown"),
        "session": _group_analytics(closed, lambda r: r.get("session_score") if r.get("session_score") is not None else "unknown"),
        "forward_phase": _group_analytics(closed, lambda r: r.get("forward_phase") or "forward_paper"),
    }
    flags = []
    high = score_bands.get("90-100", {})
    mid = score_bands.get("70-79", {})
    if high and mid and float(high.get("avg_r", 0.0)) <= float(mid.get("avg_r", 0.0)):
        flags.append("SCORE_MODEL_NOT_PREDICTIVE")
    smc_yes = comparisons["smc_confirmation"].get("smc_confirmed", {})
    smc_no = comparisons["smc_confirmation"].get("smc_not_confirmed", {})
    if smc_yes and smc_no and float(smc_yes.get("avg_r", 0.0)) <= float(smc_no.get("avg_r", 0.0)):
        flags.append("SMC_WEIGHTING_SHOULD_BE_REDUCED")
    one_min = comparisons["entry_path"].get("5m+1m", {})
    direct = comparisons["entry_path"].get("5m_direct", {})
    if one_min and direct and float(one_min.get("avg_r", 0.0)) <= float(direct.get("avg_r", 0.0)):
        flags.append("ONE_MIN_EXECUTION_NOT_ADDING_EDGE")
    if high and (float(high.get("average_spread", 0.0)) + float(high.get("average_slippage", 0.0))) > max(0.05, abs(float(high.get("avg_r", 0.0))) * 0.5):
        flags.append("COST_FILTER_TOO_LOOSE")

    module_table = []
    for key, stats in comparisons["strategy_module"].items():
        enabled = not (stats["trades"] >= 8 and stats["avg_r"] <= -0.1)
        module_table.append({"strategy": key, **stats, "enabled": enabled, "action": "Enabled" if enabled else "Disabled"})
    symbol_table = []
    for key, stats in comparisons["symbol"].items():
        keep = not (stats["trades"] >= 8 and stats["avg_r"] <= -0.1)
        symbol_table.append({"symbol": key, **stats, "enabled": keep, "action": "Keep" if keep else "Remove"})
    session_table = []
    for key, stats in comparisons["session"].items():
        keep = not (stats["trades"] >= 8 and stats["avg_r"] <= -0.1)
        session_table.append({"session": key, **stats, "enabled": keep, "action": "Keep" if keep else "Avoid"})
    score_band_table = []
    for band in ["0-59", "60-69", "70-79", "80-89", "90-100"]:
        stats = score_bands.get(band, _analytics_summary([]))
        keep = not (stats["trades"] >= 8 and stats["avg_r"] <= -0.1)
        score_band_table.append({"score_band": band, **stats, "enabled": keep, "action": "Keep" if keep else "Disable"})

    return {
        "score_bands": score_bands,
        "components": components,
        "comparisons": comparisons,
        "flags": flags,
        "dashboard_tables": {
            "score_bands": score_band_table,
            "modules": module_table,
            "symbols": symbol_table,
            "sessions": session_table,
        },
    }


def upsert_news(sym, sentiment, score, headlines: list):
    conn = get_conn()
    conn.execute("""
        INSERT INTO news (sym, sentiment, score, headlines, updated_at)
        VALUES (?,?,?,?,?)
        ON CONFLICT(sym) DO UPDATE SET
            sentiment=excluded.sentiment, score=excluded.score,
            headlines=excluded.headlines, updated_at=excluded.updated_at
    """, (sym, sentiment, score, json.dumps(headlines), datetime.now().isoformat()))
    conn.commit(); conn.close()


def read_news():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM news ORDER BY score DESC").fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["headlines"] = json.loads(d["headlines"] or "[]")
        except Exception:
            d["headlines"] = []
        out.append(d)
    return out


def read_candles(sym, limit=120):
    from datetime import datetime, timedelta
    cutoff = (datetime.utcnow() - timedelta(hours=20)).strftime("%Y-%m-%d %H:%M")
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM candles WHERE sym=? AND ts >= ? ORDER BY ts DESC LIMIT ?",
        (sym, cutoff, limit)
    ).fetchall()
    if not rows:
        rows = conn.execute(
            "SELECT * FROM candles WHERE sym=? ORDER BY ts DESC LIMIT ?",
            (sym, limit)
        ).fetchall()
    conn.close()
    return list(reversed([dict(r) for r in rows]))


def read_status(key):
    conn = get_conn()
    row = conn.execute("SELECT value FROM bot_status WHERE key=?", (key,)).fetchone()
    conn.close()
    if row:
        try:
            return json.loads(row["value"])
        except Exception:
            return row["value"]
    return None


def read_last_activity():
    """Return the most recent updated_at across signals and positions as ISO string."""
    conn = get_conn()
    ts_sig = conn.execute("SELECT MAX(updated_at) AS ts FROM signals").fetchone()["ts"]
    ts_pos = conn.execute("SELECT MAX(updated_at) AS ts FROM positions").fetchone()["ts"]
    conn.close()
    candidates = [t for t in (ts_sig, ts_pos) if t]
    return max(candidates) if candidates else None
