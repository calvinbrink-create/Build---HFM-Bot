"""Historical London interaction with completed Asia-session liquidity ranges."""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from statistics import median
from typing import Mapping, Sequence

from ..market import Candle
from .session_profiles import build_session_profile_library


@dataclass(frozen=True)
class LondonSweepOutcome:
    symbol: str
    session_date: str
    asia_high: float
    asia_low: float
    asia_range: float
    high_breached: bool
    high_reclaimed: bool
    high_breach_at: str | None
    high_depth_ranges: float | None
    high_minutes_to_breach: float | None
    high_held_outside_at_close: bool
    low_breached: bool
    low_reclaimed: bool
    low_breach_at: str | None
    low_depth_ranges: float | None
    low_minutes_to_breach: float | None
    low_held_outside_at_close: bool
    two_sided_breach: bool
    london_close: float
    source_digest: str
    source: str = "HFM_M1_LONDON_VS_COMPLETED_ASIA_RANGE"


@dataclass(frozen=True)
class LondonSweepStatistics:
    symbol: str
    eligible_session_count: int
    no_breach_session_count: int
    any_breach_session_count: int
    high_breach_count: int
    low_breach_count: int
    two_sided_breach_count: int
    breach_event_count: int
    confirmed_sweep_count: int
    held_outside_count: int
    reclaim_rate: float | None
    held_outside_rate: float | None
    median_depth_ranges: float | None
    median_minutes_to_breach: float | None
    source: str = "EMPIRICAL_LONDON_ASIA_LIQUIDITY_INTERACTION"


@dataclass(frozen=True)
class LondonSweepLibrary:
    observed_at: str
    outcomes: tuple[LondonSweepOutcome, ...]
    statistics: Mapping[str, LondonSweepStatistics]
    source: str = "HISTORICAL_LONDON_SWEEP_LIBRARY"

    def outcomes_for_symbol(self, symbol: str) -> tuple[LondonSweepOutcome, ...]:
        return tuple(item for item in self.outcomes if item.symbol == symbol)


def build_london_sweep_library(
    candles_by_symbol: Mapping[str, Sequence[Candle]],
    *,
    observed_at: datetime,
    minimum_coverage_ratio: float = 0.80,
) -> LondonSweepLibrary:
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("London sweep observation time must be timezone-aware")
    canonical_observed_at = observed_at.astimezone(timezone.utc)
    outcomes: list[LondonSweepOutcome] = []
    statistics: dict[str, LondonSweepStatistics] = {}
    for symbol, raw_rows in sorted(candles_by_symbol.items()):
        rows = tuple(sorted(raw_rows, key=lambda item: item.start))
        if any(item.symbol != symbol or item.timeframe != "M1" for item in rows):
            raise ValueError(f"London sweep library requires {symbol} M1 candles only")
        profiles = build_session_profile_library(
            {symbol: rows},
            observed_at=canonical_observed_at,
            minimum_coverage_ratio=minimum_coverage_ratio,
        )
        asia = {
            item.session_date: item
            for item in profiles.observations_for_symbol_session(symbol, "ASIA")
        }
        london = {
            item.session_date: item
            for item in profiles.observations_for_symbol_session(symbol, "LONDON")
        }
        starts = tuple(item.start for item in rows)
        symbol_outcomes = []
        for session_date in sorted(set(asia) & set(london)):
            asia_range = asia[session_date]
            london_range = london[session_date]
            if (
                asia_range.high_price is None
                or asia_range.low_price is None
                or asia_range.range_value is None
                or asia_range.range_value <= 0
            ):
                continue
            london_start = datetime.fromisoformat(london_range.start_utc)
            london_end = datetime.fromisoformat(london_range.end_utc)
            start_index = bisect_left(starts, london_start)
            end_index = bisect_left(starts, london_end)
            london_rows = tuple(rows[start_index:end_index])
            if not london_rows:
                continue
            outcome = _outcome(
                symbol,
                session_date,
                asia_range.high_price,
                asia_range.low_price,
                asia_range.range_value,
                london_start,
                london_rows,
                asia_range.source_digest,
            )
            symbol_outcomes.append(outcome)
            outcomes.append(outcome)
        statistics[symbol] = london_sweep_statistics(symbol, symbol_outcomes)
    return LondonSweepLibrary(
        observed_at=canonical_observed_at.isoformat(),
        outcomes=tuple(outcomes),
        statistics=statistics,
    )


def _outcome(
    symbol: str,
    session_date: str,
    asia_high: float,
    asia_low: float,
    asia_range: float,
    london_start: datetime,
    rows: Sequence[Candle],
    asia_source_digest: str,
) -> LondonSweepOutcome:
    high_index = next((index for index, item in enumerate(rows) if item.high > asia_high), None)
    low_index = next((index for index, item in enumerate(rows) if item.low < asia_low), None)
    high_breached = high_index is not None
    low_breached = low_index is not None
    high_reclaimed = high_breached and any(item.close <= asia_high for item in rows[high_index:])
    low_reclaimed = low_breached and any(item.close >= asia_low for item in rows[low_index:])
    high_depth = (
        (max(item.high for item in rows[high_index:]) - asia_high) / asia_range
        if high_breached else None
    )
    low_depth = (
        (asia_low - min(item.low for item in rows[low_index:])) / asia_range
        if low_breached else None
    )
    high_at = rows[high_index].start if high_breached else None
    low_at = rows[low_index].start if low_breached else None
    london_close = rows[-1].close
    digest = sha256(
        (asia_source_digest + "|" + "|".join(item.source_id for item in rows)).encode("utf-8")
    ).hexdigest()
    return LondonSweepOutcome(
        symbol=symbol,
        session_date=session_date,
        asia_high=asia_high,
        asia_low=asia_low,
        asia_range=asia_range,
        high_breached=high_breached,
        high_reclaimed=bool(high_reclaimed),
        high_breach_at=high_at.isoformat() if high_at else None,
        high_depth_ranges=high_depth,
        high_minutes_to_breach=(high_at - london_start).total_seconds() / 60.0 if high_at else None,
        high_held_outside_at_close=high_breached and london_close > asia_high,
        low_breached=low_breached,
        low_reclaimed=bool(low_reclaimed),
        low_breach_at=low_at.isoformat() if low_at else None,
        low_depth_ranges=low_depth,
        low_minutes_to_breach=(low_at - london_start).total_seconds() / 60.0 if low_at else None,
        low_held_outside_at_close=low_breached and london_close < asia_low,
        two_sided_breach=high_breached and low_breached,
        london_close=london_close,
        source_digest=digest,
    )


def london_sweep_statistics(
    symbol: str,
    outcomes: Sequence[LondonSweepOutcome],
) -> LondonSweepStatistics:
    rows = tuple(outcomes)
    high_breaches = tuple(item for item in rows if item.high_breached)
    low_breaches = tuple(item for item in rows if item.low_breached)
    event_count = len(high_breaches) + len(low_breaches)
    confirmed = sum(item.high_reclaimed for item in high_breaches) + sum(item.low_reclaimed for item in low_breaches)
    held = sum(item.high_held_outside_at_close for item in high_breaches) + sum(item.low_held_outside_at_close for item in low_breaches)
    depths = tuple(
        value
        for item in rows
        for value in (item.high_depth_ranges, item.low_depth_ranges)
        if value is not None
    )
    times = tuple(
        value
        for item in rows
        for value in (item.high_minutes_to_breach, item.low_minutes_to_breach)
        if value is not None
    )
    any_breach = sum(item.high_breached or item.low_breached for item in rows)
    return LondonSweepStatistics(
        symbol=symbol,
        eligible_session_count=len(rows),
        no_breach_session_count=len(rows) - any_breach,
        any_breach_session_count=any_breach,
        high_breach_count=len(high_breaches),
        low_breach_count=len(low_breaches),
        two_sided_breach_count=sum(item.two_sided_breach for item in rows),
        breach_event_count=event_count,
        confirmed_sweep_count=confirmed,
        held_outside_count=held,
        reclaim_rate=confirmed / event_count if event_count else None,
        held_outside_rate=held / event_count if event_count else None,
        median_depth_ranges=median(depths) if depths else None,
        median_minutes_to_breach=median(times) if times else None,
    )
