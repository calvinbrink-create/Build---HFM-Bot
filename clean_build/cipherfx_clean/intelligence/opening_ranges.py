"""Historical first-30-minute opening ranges for active market sessions."""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Mapping, Sequence

from ..market import Candle
from .session_profiles import build_session_profile_library


OPENING_RANGE_SESSIONS = ("ASIA", "LONDON", "NEW_YORK")


@dataclass(frozen=True)
class OpeningRangeRecord:
    symbol: str
    session: str
    session_date: str
    start_utc: str
    end_utc: str
    duration_minutes: int
    candle_count: int
    expected_candle_count: int
    coverage_ratio: float
    complete: bool
    open_price: float | None
    high_price: float | None
    low_price: float | None
    close_price: float | None
    midpoint_price: float | None
    range_value: float | None
    direction: str
    source_digest: str
    source: str = "HFM_M1_FIRST_30_MINUTES_OF_IANA_SESSION"


@dataclass(frozen=True)
class OpeningRangeLibrary:
    observed_at: str
    records: tuple[OpeningRangeRecord, ...]
    duration_minutes: int
    minimum_coverage_ratio: float
    source: str = "HISTORICAL_OPENING_RANGE_LIBRARY"

    def records_for_symbol(self, symbol: str) -> tuple[OpeningRangeRecord, ...]:
        return tuple(item for item in self.records if item.symbol == symbol)


def build_opening_range_library(
    candles_by_symbol: Mapping[str, Sequence[Candle]],
    *,
    observed_at: datetime,
    duration_minutes: int = 30,
    minimum_coverage_ratio: float = 0.80,
) -> OpeningRangeLibrary:
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("opening-range observation time must be timezone-aware")
    if duration_minutes <= 0:
        raise ValueError("opening-range duration must be positive")
    if not 0.0 < minimum_coverage_ratio <= 1.0:
        raise ValueError("opening-range coverage must be in (0, 1]")
    canonical_observed_at = observed_at.astimezone(timezone.utc)
    records: list[OpeningRangeRecord] = []
    for symbol, raw_rows in sorted(candles_by_symbol.items()):
        rows = tuple(sorted(raw_rows, key=lambda item: item.start))
        if any(item.symbol != symbol or item.timeframe != "M1" for item in rows):
            raise ValueError(f"opening-range library requires {symbol} M1 candles only")
        profiles = build_session_profile_library(
            {symbol: rows},
            observed_at=canonical_observed_at,
            minimum_coverage_ratio=minimum_coverage_ratio,
        )
        starts = tuple(item.start for item in rows)
        for session in OPENING_RANGE_SESSIONS:
            for session_observation in profiles.observations_for_symbol_session(symbol, session):
                opening_start = datetime.fromisoformat(session_observation.start_utc)
                opening_end = opening_start + timedelta(minutes=duration_minutes)
                if opening_end > canonical_observed_at:
                    continue
                selected = tuple(
                    rows[bisect_left(starts, opening_start):bisect_left(starts, opening_end)]
                )
                records.append(
                    _record(
                        symbol,
                        session,
                        session_observation.session_date,
                        opening_start,
                        opening_end,
                        selected,
                        duration_minutes,
                        minimum_coverage_ratio,
                    )
                )
    return OpeningRangeLibrary(
        observed_at=canonical_observed_at.isoformat(),
        records=tuple(records),
        duration_minutes=duration_minutes,
        minimum_coverage_ratio=minimum_coverage_ratio,
    )


def _record(
    symbol: str,
    session: str,
    session_date: str,
    start: datetime,
    end: datetime,
    rows: Sequence[Candle],
    expected: int,
    minimum_coverage_ratio: float,
) -> OpeningRangeRecord:
    unique_count = len({item.start for item in rows})
    coverage = unique_count / expected
    complete = bool(rows) and coverage >= minimum_coverage_ratio
    digest = sha256("|".join(item.source_id for item in rows).encode("utf-8")).hexdigest()
    if not rows:
        return OpeningRangeRecord(
            symbol, session, session_date, start.isoformat(), end.isoformat(),
            expected, 0, expected, 0.0, False, None, None, None, None, None,
            None, "NONE", digest,
        )
    opening = rows[0].open
    high = max(item.high for item in rows)
    low = min(item.low for item in rows)
    closing = rows[-1].close
    return OpeningRangeRecord(
        symbol=symbol,
        session=session,
        session_date=session_date,
        start_utc=start.isoformat(),
        end_utc=end.isoformat(),
        duration_minutes=expected,
        candle_count=unique_count,
        expected_candle_count=expected,
        coverage_ratio=coverage,
        complete=complete,
        open_price=opening,
        high_price=high,
        low_price=low,
        close_price=closing,
        midpoint_price=(high + low) / 2.0,
        range_value=high - low,
        direction="UP" if closing > opening else "DOWN" if closing < opening else "FLAT",
        source_digest=digest,
    )
