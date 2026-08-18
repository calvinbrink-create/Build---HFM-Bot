"""Entry-timing guard shared by all three learning engines.

Backtested 2026-07-19 against the 115 real closed trades the current
engines would still take (exact historical MarketSnapshot replay, zero
lookahead). The engines' trend/momentum/structure checks select a valid
direction but fire at the moment everything aligns - which is usually
right after a short-term spike, at a poor entry price. These four
industry-standard checks separate that population cleanly:

    baseline (all 115):                 -$2,339 total, 37% win rate
    guard-passing subset (23 trades):     +$545 total, 65% win rate
    guard-blocked subset (92 trades):   -$2,884 total, 30% win rate

Rules (combo "F" from the factor lab):
  1. Bollinger stretch  - block BUY when the M15 close is more than +2
     standard deviations above its 20-bar mean (SELL mirrored). Entering
     beyond 2 sigma chases an already-extended short-term move.
  2. RSI(2) chase       - block BUY when M15 RSI(2) > 90 (SELL < 10).
     The most-researched short-term mean-reversion rule: 2-bar
     overbought entries systematically revert first.
  3. IBS bar extreme    - block BUY when the current H1 bar closed in
     its top 10% (IBS > 0.9; SELL mirrored below 0.1) - buying the very
     top of the hour's range.
  4. H1 momentum        - require the 12-bar H1 return to agree with the
     trade side (time-series momentum, the CTA-industry standard).

A check that cannot be computed (insufficient candles) passes rather
than blocks - the guard only acts on positive evidence of a bad entry.
"""
from __future__ import annotations

import math
import os

from .contracts import MarketSnapshot


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def enabled() -> bool:
    return str(os.getenv("MT5_ENTRY_GUARD_ENABLED", "1")).strip().lower() in {"1", "true", "yes", "on"}


def boll_z(frame, period: int = 20) -> float | None:
    values = [float(c.close) for c in frame.candles][-period:]
    if len(values) < period:
        return None
    mean = sum(values) / period
    std = math.sqrt(sum((x - mean) ** 2 for x in values) / period)
    return (values[-1] - mean) / std if std > 0 else 0.0


def rsi2(frame, period: int = 2) -> float | None:
    values = [float(c.close) for c in frame.candles]
    if len(values) < period + 2:
        return None
    gains = losses = 0.0
    for i in range(-period, 0):
        change = values[i] - values[i - 1]
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    if gains + losses == 0:
        return 50.0
    return 100.0 * gains / (gains + losses)


def ibs(frame) -> float | None:
    if not frame.candles:
        return None
    c = frame.candles[-1]
    if c.high == c.low:
        return 0.5
    return (c.close - c.low) / (c.high - c.low)


def tsmom(frame, lookback: int = 12) -> float | None:
    values = [float(c.close) for c in frame.candles]
    if len(values) < lookback + 1:
        return None
    prior = values[-1 - lookback]
    return (values[-1] - prior) / prior if prior else None


def evaluate(snapshot: MarketSnapshot, side: str) -> dict:
    """Return {"blocked": bool, "reasons": [...], "values": {...}}."""
    result = {"blocked": False, "reasons": [], "values": {}}
    if not enabled():
        result["values"]["enabled"] = False
        return result
    m15 = snapshot.frames.get("M15")
    h1 = snapshot.frames.get("H1")
    if m15 is None or h1 is None:
        return result
    buy = side == "BUY"

    z_limit = _env_float("MT5_ENTRY_GUARD_BOLL_Z", 2.0)
    z = boll_z(m15)
    result["values"]["boll_z_m15"] = round(z, 3) if z is not None else None
    if z is not None and ((z > z_limit) if buy else (z < -z_limit)):
        result["reasons"].append("ENTRY_STRETCHED_BOLLINGER")

    rsi_high = _env_float("MT5_ENTRY_GUARD_RSI2_HIGH", 90.0)
    r = rsi2(m15)
    result["values"]["rsi2_m15"] = round(r, 1) if r is not None else None
    if r is not None and ((r > rsi_high) if buy else (r < 100.0 - rsi_high)):
        result["reasons"].append("ENTRY_CHASE_RSI2")

    ibs_high = _env_float("MT5_ENTRY_GUARD_IBS_HIGH", 0.9)
    b = ibs(h1)
    result["values"]["ibs_h1"] = round(b, 3) if b is not None else None
    if b is not None and ((b > ibs_high) if buy else (b < 1.0 - ibs_high)):
        result["reasons"].append("ENTRY_BAR_EXTREME_IBS")

    lookback = int(_env_float("MT5_ENTRY_GUARD_TSMOM_BARS", 12))
    momentum = tsmom(h1, lookback)
    result["values"]["tsmom_h1"] = round(momentum, 6) if momentum is not None else None
    if momentum is not None and (momentum > 0) != buy:
        result["reasons"].append("H1_MOMENTUM_MISALIGNED")

    result["blocked"] = bool(result["reasons"])
    return result


def _adx_proxy(candles, period: int = 14):
    """Trend-strength proxy = avg |close-to-close move| / avg bar range, x100.
    Used only as an entry-timing diagnostic; the active asset engines do not
    import or call this helper. High values mean the recent move is large relative to
    bar ranges - an already-extended/exhausted push."""
    if len(candles) < period + 2:
        return None
    sample = candles[-period:]
    moves = [abs(candles[i].close - candles[i - 1].close)
             for i in range(len(candles) - period, len(candles))]
    ranges = [max(c.high - c.low, 1e-12) for c in sample]
    if not moves or not ranges:
        return None
    return min(100.0, 100.0 * (sum(moves) / len(moves)) / (sum(ranges) / len(ranges)))


def adx_overextended(h1_frame, ceiling=None) -> bool:
    """True if the H1 trend is already so extended a fresh entry is chasing.
    Added 2026-07-24 from a zero-lookahead snapshot-replay backtest across all
    9 active symbols: fresh breakouts taken when the H1 ADX-proxy was >=45 lost
    in BOTH halves of the sample, while skipping them lifted win rate 42->59%
    (half 1) and 33->71% (half 2). Only entry filter that improved out-of-sample
    in both halves - the false-breakout / "in loss right after open" trades
    cluster at very high trend strength. Default ceiling 50;
    MT5_ENTRY_ADX_CEILING=0 disables it. Instant pass/skip - never delays an
    allowed entry."""
    if ceiling is None:
        ceiling = _env_float("MT5_ENTRY_ADX_CEILING", 50.0)
    if ceiling <= 0:
        return False
    if h1_frame is None or not getattr(h1_frame, "candles", None):
        return False
    adx = _adx_proxy(list(h1_frame.candles))
    return adx is not None and adx >= ceiling
