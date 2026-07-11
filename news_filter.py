"""
News & Event Filter for ScalpBot v4
=====================================
Four filters that plug into scalping_bot_v4.py — no extra API keys needed.

  get_vix_multiplier()   → float  (0.5 / 1.0 / 1.5) based on VIX level
  has_earnings_soon(sym) → bool   yfinance earnings calendar ±2 days
  is_event_blackout()    → bool   FOMC / CPI / NFP ±45 min
  has_negative_news(sym) → bool   yfinance headlines keyword scan
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, date, timedelta, timezone

log = logging.getLogger(__name__)

try:
    import mt5_news_archive as _news_archive
except Exception:
    _news_archive = None

# Suppress yfinance's own noisy HTTP error logger (404s for ETFs etc are expected)
logging.getLogger("yfinance").setLevel(logging.CRITICAL)


# ── VIX regime cache (TTL 5 min) ──────────────────────────────────────────────
_vix_value: float | None = None
_vix_ts: float = 0.0
_VIX_TTL = 300


def _vix_to_mult(vix: float) -> float:
    if vix > 25:
        return 0.5   # fear spike — cut risk
    if vix < 15:
        return 1.5   # calm market — push harder
    return 1.0


def get_vix_multiplier() -> float:
    """Position-size multiplier driven by VIX.
    VIX < 15  → 1.5x   VIX 15-25 → 1.0x   VIX > 25 → 0.5x
    Returns 1.0 on any fetch failure.
    """
    global _vix_value, _vix_ts
    if _vix_value is not None and (time.time() - _vix_ts) < _VIX_TTL:
        return _vix_to_mult(_vix_value)
    try:
        import yfinance as yf
        hist = yf.Ticker("^VIX").history(period="1d", interval="5m")
        if hist is not None and not hist.empty:
            _vix_value = float(hist["Close"].iloc[-1])
            _vix_ts = time.time()
            log.debug(f"[news_filter] VIX={_vix_value:.1f} → mult={_vix_to_mult(_vix_value):.1f}")
            return _vix_to_mult(_vix_value)
    except Exception as exc:
        log.debug(f"[news_filter] VIX fetch failed: {exc}")
    return 1.0


# ── Earnings calendar cache (TTL 60 min) ──────────────────────────────────────
_earnings_cache: dict[str, tuple[bool, float]] = {}
_EARNINGS_TTL = 3600
_EARNINGS_WINDOW_DAYS = 2


def has_earnings_soon(sym: str) -> bool:
    """True if sym has earnings within ±2 calendar days (yfinance calendar).
    Returns False on failure — don't block unnecessarily.
    """
    cached = _earnings_cache.get(sym)
    if cached and (time.time() - cached[1]) < _EARNINGS_TTL:
        return cached[0]

    try:
        import yfinance as yf
        cal = yf.Ticker(sym).calendar
        dates = []
        if isinstance(cal, dict):
            ed = cal.get("Earnings Date") or []
            dates = list(ed) if hasattr(ed, "__iter__") and not isinstance(ed, str) else [ed]
        elif hasattr(cal, "columns") and "Earnings Date" in cal.columns:
            dates = list(cal.loc["Earnings Date"])

        today = date.today()
        for d in dates:
            try:
                if hasattr(d, "date"):
                    d = d.date()
                elif isinstance(d, str):
                    d = datetime.fromisoformat(d[:10]).date()
                if abs((d - today).days) <= _EARNINGS_WINDOW_DAYS:
                    log.info(f"[news_filter] {sym}: earnings {d} — blackout active")
                    _earnings_cache[sym] = (True, time.time())
                    return True
            except Exception:
                continue
        _earnings_cache[sym] = (False, time.time())
        return False
    except Exception as exc:
        log.debug(f"[news_filter] {sym} earnings fetch failed: {exc}")
        _earnings_cache[sym] = (False, time.time())
        return False


# ── Economic calendar — FOMC / CPI / NFP 2026 (ET times) ─────────────────────
# Update this list each January.
# FOMC decision: 2:00 PM ET.  CPI / NFP release: 8:30 AM ET.
_EVENTS_ET: list[tuple[date, int, int, str]] = [
    # FOMC
    (date(2026,  1, 28), 14,  0, "FOMC"),
    (date(2026,  3, 18), 14,  0, "FOMC"),
    (date(2026,  4, 29), 14,  0, "FOMC"),
    (date(2026,  6, 17), 14,  0, "FOMC"),
    (date(2026,  7, 29), 14,  0, "FOMC"),
    (date(2026,  9, 16), 14,  0, "FOMC"),
    (date(2026, 10, 28), 14,  0, "FOMC"),
    (date(2026, 12,  9), 14,  0, "FOMC"),
    # CPI, official BLS 2026 release calendar
    (date(2026,  1, 13),  8, 30, "CPI"),
    (date(2026,  2, 13),  8, 30, "CPI"),
    (date(2026,  3, 11),  8, 30, "CPI"),
    (date(2026,  4, 10),  8, 30, "CPI"),
    (date(2026,  5, 12),  8, 30, "CPI"),
    (date(2026,  6, 10),  8, 30, "CPI"),
    (date(2026,  7, 14),  8, 30, "CPI"),
    (date(2026,  8, 12),  8, 30, "CPI"),
    (date(2026,  9, 11),  8, 30, "CPI"),
    (date(2026, 10, 14),  8, 30, "CPI"),
    (date(2026, 11, 10),  8, 30, "CPI"),
    (date(2026, 12, 10),  8, 30, "CPI"),
    # Employment Situation, official BLS 2026 release calendar
    (date(2026,  1,  9),  8, 30, "NFP"),
    (date(2026,  2, 11),  8, 30, "NFP"),
    (date(2026,  3,  6),  8, 30, "NFP"),
    (date(2026,  4,  3),  8, 30, "NFP"),
    (date(2026,  5,  8),  8, 30, "NFP"),
    (date(2026,  6,  5),  8, 30, "NFP"),
    (date(2026,  7,  2),  8, 30, "NFP"),
    (date(2026,  8,  7),  8, 30, "NFP"),
    (date(2026,  9,  4),  8, 30, "NFP"),
    (date(2026, 10,  2),  8, 30, "NFP"),
    (date(2026, 11,  6),  8, 30, "NFP"),
    (date(2026, 12,  4),  8, 30, "NFP"),
]
_BLACKOUT_MINS = 45


def is_event_blackout(at_utc: datetime | None = None) -> bool:
    """True if now is within ±45 min of a FOMC/CPI/NFP release.
    Converts ET to UTC to avoid DST ambiguity (SA doesn't observe DST).
    EDT = UTC-4 (Mar-Nov), EST = UTC-5 (Nov-Mar).
    """
    if _news_archive is not None:
        try:
            state = _news_archive.blackout_state(at_utc, _BLACKOUT_MINS, _BLACKOUT_MINS)
            if state.get("blackout"):
                log.info(f"[news_filter] archive blackout active — {state.get('reason', '')}")
                return True
            return False
        except Exception as exc:
            log.debug(f"[news_filter] archive blackout check failed: {exc}")

    try:
        now_utc = at_utc or datetime.now(timezone.utc)
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=timezone.utc)
        now_utc = now_utc.astimezone(timezone.utc)
        for evt_date, h_et, m_et, label in _EVENTS_ET:
            et_offset = -4 if 3 <= evt_date.month <= 11 else -5
            evt_utc = datetime(
                evt_date.year, evt_date.month, evt_date.day,
                h_et, m_et, 0, tzinfo=timezone.utc
            ) + timedelta(hours=-et_offset)
            delta_min = abs((now_utc - evt_utc).total_seconds()) / 60
            if delta_min <= _BLACKOUT_MINS:
                log.info(
                    f"[news_filter] {label} blackout — event {evt_date} "
                    f"{h_et:02d}:{m_et:02d} ET, {delta_min:.0f}min away"
                )
                return True
    except Exception as exc:
        log.debug(f"[news_filter] event blackout check failed: {exc}")
    return False


# ── News sentiment — yfinance headlines keyword scan ──────────────────────────
_NEGATIVE_KEYWORDS = {
    "downgrade", "downgrades", "miss", "missed", "misses", "disappoints",
    "disappointing", "cuts guidance", "guidance cut", "lowers outlook",
    "loss", "losses", "layoff", "layoffs", "layoffs announced", "recall",
    "fine", "fined", "fraud", "lawsuit", "investigation", "sec", "doj",
    "breach", "hack", "hacked", "bankruptcy", "bankrupt", "default",
    "warning", "selloff", "sell-off", "crash", "plunge", "plunges",
    "tumbles", "slumps", "sinks", "falls sharply", "revenue miss",
    "earnings miss", "profit warning",
}

_news_cache: dict[str, tuple[bool, float]] = {}
_NEWS_TTL = 900  # 15 min


def has_negative_news(sym: str) -> bool:
    """True if any of the 5 most recent yfinance headlines contain negative keywords.
    Returns False on failure.
    """
    cached = _news_cache.get(sym)
    if cached and (time.time() - cached[1]) < _NEWS_TTL:
        return cached[0]

    try:
        import yfinance as yf
        news = yf.Ticker(sym).news or []
        for item in (news[:5] if news else []):
            title   = (item.get("title")   or "").lower()
            summary = (item.get("summary") or "").lower()
            text    = title + " " + summary
            if any(kw in text for kw in _NEGATIVE_KEYWORDS):
                log.info(
                    f"[news_filter] {sym}: negative headline — "
                    f"'{(item.get('title') or '')[:80]}'"
                )
                _news_cache[sym] = (True, time.time())
                return True
        _news_cache[sym] = (False, time.time())
        return False
    except Exception as exc:
        log.debug(f"[news_filter] {sym} news fetch failed: {exc}")
        _news_cache[sym] = (False, time.time())
        return False
