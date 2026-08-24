"""Empirical New York continuation/reversal after pre-NY London direction."""

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
class NYTransitionOutcome:
    symbol: str
    session_date: str
    london_start_utc: str
    london_reference_end_utc: str
    new_york_start_utc: str
    new_york_end_utc: str
    london_direction: str
    new_york_direction: str
    london_return: float
    new_york_return: float
    london_range_fraction: float
    new_york_range_fraction: float
    continuation: bool
    reversal: bool
    neutral: bool
    london_candle_count: int
    new_york_candle_count: int
    source_digest: str
    source: str = "PRE_NY_LONDON_PATH_THEN_NEW_YORK_SESSION"


@dataclass(frozen=True)
class NYTransitionStatistics:
    symbol: str
    eligible_session_count: int
    continuation_count: int
    reversal_count: int
    neutral_count: int
    continuation_rate: float | None
    reversal_rate: float | None
    london_up_count: int
    london_up_continuation_rate: float | None
    london_up_reversal_rate: float | None
    london_down_count: int
    london_down_continuation_rate: float | None
    london_down_reversal_rate: float | None
    median_absolute_london_return: float | None
    median_absolute_new_york_return: float | None
    source: str = "EMPIRICAL_NY_DIRECTION_GIVEN_PRE_NY_LONDON_DIRECTION"


@dataclass(frozen=True)
class NYTransitionLibrary:
    observed_at: str
    outcomes: tuple[NYTransitionOutcome, ...]
    statistics: Mapping[str, NYTransitionStatistics]
    source: str = "HISTORICAL_NY_TRANSITION_LIBRARY"

    def outcomes_for_symbol(self, symbol: str) -> tuple[NYTransitionOutcome, ...]:
        return tuple(item for item in self.outcomes if item.symbol == symbol)


def build_ny_transition_library(
    candles_by_symbol: Mapping[str, Sequence[Candle]],
    *,
    observed_at: datetime,
    minimum_coverage_ratio: float = 0.80,
) -> NYTransitionLibrary:
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("NY transition observation time must be timezone-aware")
    canonical_observed_at = observed_at.astimezone(timezone.utc)
    all_outcomes: list[NYTransitionOutcome] = []
    statistics: dict[str, NYTransitionStatistics] = {}
    for symbol, raw_rows in sorted(candles_by_symbol.items()):
        rows = tuple(sorted(raw_rows, key=lambda item: item.start))
        if any(item.symbol != symbol or item.timeframe != "M1" for item in rows):
            raise ValueError(f"NY transition library requires {symbol} M1 candles only")
        profiles = build_session_profile_library(
            {symbol: rows},
            observed_at=canonical_observed_at,
            minimum_coverage_ratio=minimum_coverage_ratio,
        )
        london = {
            item.session_date: item
            for item in profiles.observations_for_symbol_session(symbol, "LONDON")
        }
        new_york = {
            item.session_date: item
            for item in profiles.observations_for_symbol_session(symbol, "NEW_YORK")
        }
        starts = tuple(item.start for item in rows)
        symbol_outcomes = []
        for session_date in sorted(set(london) & set(new_york)):
            london_start = datetime.fromisoformat(london[session_date].start_utc)
            new_york_start = datetime.fromisoformat(new_york[session_date].start_utc)
            new_york_end = datetime.fromisoformat(new_york[session_date].end_utc)
            london_rows = _window(rows, starts, london_start, new_york_start)
            new_york_rows = _window(rows, starts, new_york_start, new_york_end)
            expected_london = int((new_york_start - london_start).total_seconds() // 60)
            expected_new_york = int((new_york_end - new_york_start).total_seconds() // 60)
            if (
                not london_rows
                or not new_york_rows
                or len({item.start for item in london_rows}) / expected_london < minimum_coverage_ratio
                or len({item.start for item in new_york_rows}) / expected_new_york < minimum_coverage_ratio
            ):
                continue
            outcome = _outcome(
                symbol,
                session_date,
                london_start,
                new_york_start,
                new_york_end,
                london_rows,
                new_york_rows,
            )
            symbol_outcomes.append(outcome)
            all_outcomes.append(outcome)
        statistics[symbol] = ny_transition_statistics(symbol, symbol_outcomes)
    return NYTransitionLibrary(
        observed_at=canonical_observed_at.isoformat(),
        outcomes=tuple(all_outcomes),
        statistics=statistics,
    )


def _window(
    rows: Sequence[Candle],
    starts: Sequence[datetime],
    start: datetime,
    end: datetime,
) -> tuple[Candle, ...]:
    return tuple(rows[bisect_left(starts, start):bisect_left(starts, end)])


def _direction(open_price: float, close_price: float) -> str:
    return "UP" if close_price > open_price else "DOWN" if close_price < open_price else "FLAT"


def _path_values(rows: Sequence[Candle]) -> tuple[float, float, float, str]:
    opening = rows[0].open
    closing = rows[-1].close
    path_range = max(item.high for item in rows) - min(item.low for item in rows)
    return (
        (closing - opening) / opening if opening else 0.0,
        path_range / opening if opening else 0.0,
        closing,
        _direction(opening, closing),
    )


def _outcome(
    symbol: str,
    session_date: str,
    london_start: datetime,
    new_york_start: datetime,
    new_york_end: datetime,
    london_rows: Sequence[Candle],
    new_york_rows: Sequence[Candle],
) -> NYTransitionOutcome:
    london_return, london_range, _, london_direction = _path_values(london_rows)
    new_york_return, new_york_range, _, new_york_direction = _path_values(new_york_rows)
    directional = london_direction in {"UP", "DOWN"} and new_york_direction in {"UP", "DOWN"}
    continuation = directional and london_direction == new_york_direction
    reversal = directional and london_direction != new_york_direction
    digest = sha256(
        "|".join(item.source_id for item in (*london_rows, *new_york_rows)).encode("utf-8")
    ).hexdigest()
    return NYTransitionOutcome(
        symbol=symbol,
        session_date=session_date,
        london_start_utc=london_start.isoformat(),
        london_reference_end_utc=new_york_start.isoformat(),
        new_york_start_utc=new_york_start.isoformat(),
        new_york_end_utc=new_york_end.isoformat(),
        london_direction=london_direction,
        new_york_direction=new_york_direction,
        london_return=london_return,
        new_york_return=new_york_return,
        london_range_fraction=london_range,
        new_york_range_fraction=new_york_range,
        continuation=continuation,
        reversal=reversal,
        neutral=not continuation and not reversal,
        london_candle_count=len(london_rows),
        new_york_candle_count=len(new_york_rows),
        source_digest=digest,
    )


def ny_transition_statistics(
    symbol: str,
    outcomes: Sequence[NYTransitionOutcome],
) -> NYTransitionStatistics:
    rows = tuple(outcomes)
    continuation_count = sum(item.continuation for item in rows)
    reversal_count = sum(item.reversal for item in rows)
    neutral_count = sum(item.neutral for item in rows)
    london_up = tuple(item for item in rows if item.london_direction == "UP")
    london_down = tuple(item for item in rows if item.london_direction == "DOWN")
    size = len(rows)
    return NYTransitionStatistics(
        symbol=symbol,
        eligible_session_count=size,
        continuation_count=continuation_count,
        reversal_count=reversal_count,
        neutral_count=neutral_count,
        continuation_rate=continuation_count / size if size else None,
        reversal_rate=reversal_count / size if size else None,
        london_up_count=len(london_up),
        london_up_continuation_rate=sum(item.continuation for item in london_up) / len(london_up) if london_up else None,
        london_up_reversal_rate=sum(item.reversal for item in london_up) / len(london_up) if london_up else None,
        london_down_count=len(london_down),
        london_down_continuation_rate=sum(item.continuation for item in london_down) / len(london_down) if london_down else None,
        london_down_reversal_rate=sum(item.reversal for item in london_down) / len(london_down) if london_down else None,
        median_absolute_london_return=median(abs(item.london_return) for item in rows) if rows else None,
        median_absolute_new_york_return=median(abs(item.new_york_return) for item in rows) if rows else None,
    )
