"""Empirical opening-range breakout characteristics from completed M1 sessions."""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from statistics import mean, median
from typing import Mapping, Sequence

from ..market import Candle
from .opening_ranges import OpeningRangeLibrary, build_opening_range_library
from .session_profiles import build_session_profile_library


@dataclass(frozen=True)
class OpeningRangeBreakoutOutcome:
    symbol: str
    session: str
    session_date: str
    opening_range_start_utc: str
    opening_range_end_utc: str
    session_end_utc: str
    opening_high: float
    opening_low: float
    opening_midpoint: float
    opening_range_value: float
    breakout_present: bool
    breakout_direction: str
    breakout_at_utc: str | None
    minutes_after_opening_range: int | None
    breakout_price: float | None
    close_distance: float | None
    close_distance_in_ranges: float | None
    candle_body_fraction: float | None
    activity_ratio: float | None
    maximum_favourable_excursion: float | None
    maximum_adverse_excursion: float | None
    mfe_in_ranges: float | None
    mae_in_ranges: float | None
    session_close_distance: float | None
    session_close_outside_same_side: bool | None
    source_digest: str
    source: str = "HFM_M1_CLOSE_OUTSIDE_FIRST_30_MINUTE_RANGE"


@dataclass(frozen=True)
class OpeningRangeBreakoutStatistics:
    symbol: str
    session: str
    eligible_opening_range_count: int
    breakout_count: int
    no_breakout_count: int
    upward_breakout_count: int
    downward_breakout_count: int
    breakout_rate: float | None
    upward_breakout_rate: float | None
    downward_breakout_rate: float | None
    same_side_session_close_rate: float | None
    median_minutes_to_breakout: float | None
    median_close_distance_in_ranges: float | None
    median_activity_ratio: float | None
    median_mfe_in_ranges: float | None
    median_mae_in_ranges: float | None
    status: str
    source: str = "EMPIRICAL_OPENING_RANGE_BREAKOUT_CHARACTERISTICS"


@dataclass(frozen=True)
class OpeningRangeBreakoutLibrary:
    observed_at: str
    outcomes: tuple[OpeningRangeBreakoutOutcome, ...]
    statistics: Mapping[str, OpeningRangeBreakoutStatistics]
    source: str = "HISTORICAL_OPENING_RANGE_BREAKOUT_LIBRARY"

    def outcomes_for_symbol(self, symbol: str) -> tuple[OpeningRangeBreakoutOutcome, ...]:
        return tuple(item for item in self.outcomes if item.symbol == symbol)

    def statistics_for_symbol(self, symbol: str) -> Mapping[str, OpeningRangeBreakoutStatistics]:
        prefix = f"{symbol}|"
        return {
            key.removeprefix(prefix): value
            for key, value in self.statistics.items()
            if key.startswith(prefix)
        }


def build_opening_range_breakout_library(
    candles_by_symbol: Mapping[str, Sequence[Candle]],
    *,
    observed_at: datetime,
    opening_ranges: OpeningRangeLibrary | None = None,
    minimum_coverage_ratio: float = 0.80,
) -> OpeningRangeBreakoutLibrary:
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("opening-range breakout observation time must be timezone-aware")
    canonical_observed_at = observed_at.astimezone(timezone.utc)
    ranges = opening_ranges or build_opening_range_library(
        candles_by_symbol,
        observed_at=canonical_observed_at,
        minimum_coverage_ratio=minimum_coverage_ratio,
    )
    profiles = build_session_profile_library(
        candles_by_symbol,
        observed_at=canonical_observed_at,
        minimum_coverage_ratio=minimum_coverage_ratio,
    )
    session_windows = {
        (item.symbol, item.session, item.session_date): item
        for item in profiles.observations
        if item.complete and item.session in {"ASIA", "LONDON", "NEW_YORK"}
    }
    outcomes: list[OpeningRangeBreakoutOutcome] = []
    for symbol, raw_rows in sorted(candles_by_symbol.items()):
        rows = tuple(sorted(raw_rows, key=lambda item: item.start))
        starts = tuple(item.start for item in rows)
        for opening in ranges.records_for_symbol(symbol):
            if not opening.complete:
                continue
            window = session_windows.get((symbol, opening.session, opening.session_date))
            if window is None:
                continue
            opening_start = datetime.fromisoformat(opening.start_utc)
            opening_end = datetime.fromisoformat(opening.end_utc)
            session_end = datetime.fromisoformat(window.end_utc)
            opening_rows = rows[
                bisect_left(starts, opening_start):bisect_left(starts, opening_end)
            ]
            following = rows[
                bisect_left(starts, opening_end):bisect_left(starts, session_end)
            ]
            outcomes.append(_outcome(opening, window.end_utc, opening_rows, following))
    statistics: dict[str, OpeningRangeBreakoutStatistics] = {}
    for symbol in sorted(candles_by_symbol):
        for session in ("ASIA", "LONDON", "NEW_YORK"):
            selected = tuple(
                item for item in outcomes
                if item.symbol == symbol and item.session == session
            )
            statistics[f"{symbol}|{session}"] = _statistics(symbol, session, selected)
    return OpeningRangeBreakoutLibrary(
        observed_at=canonical_observed_at.isoformat(),
        outcomes=tuple(outcomes),
        statistics=statistics,
    )


def _outcome(opening, session_end_utc: str, opening_rows, following) -> OpeningRangeBreakoutOutcome:
    high = float(opening.high_price)
    low = float(opening.low_price)
    midpoint = float(opening.midpoint_price)
    width = float(opening.range_value)
    opening_activity = mean(item.tick_volume for item in opening_rows) if opening_rows else 0.0
    breakout_index = None
    direction = "NONE"
    for index, candle in enumerate(following):
        if candle.close > high:
            breakout_index, direction = index, "UP"
            break
        if candle.close < low:
            breakout_index, direction = index, "DOWN"
            break
    digest_rows = tuple(opening_rows) + tuple(following)
    digest = sha256("|".join(item.source_id for item in digest_rows).encode("utf-8")).hexdigest()
    if breakout_index is None:
        return OpeningRangeBreakoutOutcome(
            opening.symbol, opening.session, opening.session_date,
            opening.start_utc, opening.end_utc, session_end_utc,
            high, low, midpoint, width, False, "NONE", None, None, None,
            None, None, None, None, None, None, None, None, None, None, digest,
        )
    breakout = following[breakout_index]
    remaining = following[breakout_index:]
    level = high if direction == "UP" else low
    signed = breakout.close - level if direction == "UP" else level - breakout.close
    candle_range = breakout.high - breakout.low
    entry = breakout.close
    if direction == "UP":
        mfe = max(item.high for item in remaining) - entry
        mae = entry - min(item.low for item in remaining)
        session_distance = remaining[-1].close - level
        closes_same_side = remaining[-1].close > high
    else:
        mfe = entry - min(item.low for item in remaining)
        mae = max(item.high for item in remaining) - entry
        session_distance = level - remaining[-1].close
        closes_same_side = remaining[-1].close < low
    opening_end = datetime.fromisoformat(opening.end_utc)
    return OpeningRangeBreakoutOutcome(
        symbol=opening.symbol,
        session=opening.session,
        session_date=opening.session_date,
        opening_range_start_utc=opening.start_utc,
        opening_range_end_utc=opening.end_utc,
        session_end_utc=session_end_utc,
        opening_high=high,
        opening_low=low,
        opening_midpoint=midpoint,
        opening_range_value=width,
        breakout_present=True,
        breakout_direction=direction,
        breakout_at_utc=breakout.end.isoformat(),
        minutes_after_opening_range=int((breakout.end - opening_end).total_seconds() // 60),
        breakout_price=entry,
        close_distance=signed,
        close_distance_in_ranges=signed / width if width else None,
        candle_body_fraction=abs(breakout.close - breakout.open) / candle_range if candle_range else 0.0,
        activity_ratio=breakout.tick_volume / opening_activity if opening_activity else None,
        maximum_favourable_excursion=max(0.0, mfe),
        maximum_adverse_excursion=max(0.0, mae),
        mfe_in_ranges=max(0.0, mfe) / width if width else None,
        mae_in_ranges=max(0.0, mae) / width if width else None,
        session_close_distance=session_distance,
        session_close_outside_same_side=closes_same_side,
        source_digest=digest,
    )


def _statistics(symbol: str, session: str, outcomes) -> OpeningRangeBreakoutStatistics:
    events = tuple(item for item in outcomes if item.breakout_present)
    size = len(outcomes)
    event_count = len(events)
    return OpeningRangeBreakoutStatistics(
        symbol=symbol,
        session=session,
        eligible_opening_range_count=size,
        breakout_count=event_count,
        no_breakout_count=size - event_count,
        upward_breakout_count=sum(item.breakout_direction == "UP" for item in events),
        downward_breakout_count=sum(item.breakout_direction == "DOWN" for item in events),
        breakout_rate=event_count / size if size else None,
        upward_breakout_rate=sum(item.breakout_direction == "UP" for item in events) / size if size else None,
        downward_breakout_rate=sum(item.breakout_direction == "DOWN" for item in events) / size if size else None,
        same_side_session_close_rate=(
            sum(item.session_close_outside_same_side is True for item in events) / event_count
            if event_count else None
        ),
        median_minutes_to_breakout=_median(item.minutes_after_opening_range for item in events),
        median_close_distance_in_ranges=_median(item.close_distance_in_ranges for item in events),
        median_activity_ratio=_median(item.activity_ratio for item in events),
        median_mfe_in_ranges=_median(item.mfe_in_ranges for item in events),
        median_mae_in_ranges=_median(item.mae_in_ranges for item in events),
        status="OBSERVED" if size else "NO_COMPLETE_HISTORY",
    )


def _median(values) -> float | None:
    selected = tuple(float(value) for value in values if value is not None)
    return median(selected) if selected else None
