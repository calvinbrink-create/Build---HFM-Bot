"""Failed opening-range breakouts measured by exact boundary reclaim."""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from statistics import median
from typing import Mapping, Sequence

from ..market import Candle
from .opening_range_breakouts import OpeningRangeBreakoutLibrary


@dataclass(frozen=True)
class FalseOpeningRangeBreakoutOutcome:
    symbol: str
    session: str
    session_date: str
    breakout_direction: str
    breakout_at_utc: str
    broken_level: float
    opposite_level: float
    opening_range_value: float
    reclaimed: bool
    reclaimed_at_utc: str | None
    minutes_to_reclaim: int | None
    reclaim_close: float | None
    reclaim_depth: float | None
    reclaim_depth_in_ranges: float | None
    maximum_extension_before_reclaim: float
    maximum_extension_in_ranges: float | None
    opposite_boundary_broken: bool
    held_outside_at_session_close: bool
    source_digest: str
    source: str = "HFM_M1_CLOSE_RECLAIM_OF_BROKEN_OPENING_RANGE_BOUNDARY"


@dataclass(frozen=True)
class FalseOpeningRangeBreakoutStatistics:
    symbol: str
    session: str
    eligible_breakout_count: int
    false_breakout_count: int
    unreclaimed_breakout_count: int
    upward_false_breakout_count: int
    downward_false_breakout_count: int
    opposite_boundary_break_count: int
    false_breakout_rate: float | None
    unreclaimed_breakout_rate: float | None
    opposite_boundary_break_rate: float | None
    median_minutes_to_reclaim: float | None
    median_reclaim_depth_in_ranges: float | None
    median_maximum_extension_in_ranges: float | None
    status: str
    source: str = "EMPIRICAL_FALSE_OPENING_RANGE_BREAKOUT_STATISTICS"


@dataclass(frozen=True)
class FalseOpeningRangeBreakoutLibrary:
    observed_at: str
    outcomes: tuple[FalseOpeningRangeBreakoutOutcome, ...]
    statistics: Mapping[str, FalseOpeningRangeBreakoutStatistics]
    source: str = "HISTORICAL_FALSE_OPENING_RANGE_BREAKOUT_LIBRARY"

    def outcomes_for_symbol(self, symbol: str) -> tuple[FalseOpeningRangeBreakoutOutcome, ...]:
        return tuple(item for item in self.outcomes if item.symbol == symbol)

    def statistics_for_symbol(self, symbol: str) -> Mapping[str, FalseOpeningRangeBreakoutStatistics]:
        prefix = f"{symbol}|"
        return {
            key.removeprefix(prefix): value
            for key, value in self.statistics.items()
            if key.startswith(prefix)
        }


def build_false_opening_range_breakout_library(
    candles_by_symbol: Mapping[str, Sequence[Candle]],
    *,
    observed_at: datetime,
    breakouts: OpeningRangeBreakoutLibrary,
) -> FalseOpeningRangeBreakoutLibrary:
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("false-ORB observation time must be timezone-aware")
    canonical_observed_at = observed_at.astimezone(timezone.utc)
    outcomes: list[FalseOpeningRangeBreakoutOutcome] = []
    for symbol, raw_rows in sorted(candles_by_symbol.items()):
        rows = tuple(sorted(raw_rows, key=lambda item: item.start))
        starts = tuple(item.start for item in rows)
        for breakout in breakouts.outcomes_for_symbol(symbol):
            if not breakout.breakout_present:
                continue
            breakout_at = datetime.fromisoformat(str(breakout.breakout_at_utc))
            session_end = datetime.fromisoformat(breakout.session_end_utc)
            following = rows[
                bisect_left(starts, breakout_at):bisect_left(starts, session_end)
            ]
            outcomes.append(_outcome(breakout, breakout_at, following))
    statistics: dict[str, FalseOpeningRangeBreakoutStatistics] = {}
    for symbol in sorted(candles_by_symbol):
        for session in ("ASIA", "LONDON", "NEW_YORK"):
            selected = tuple(
                item for item in outcomes
                if item.symbol == symbol and item.session == session
            )
            statistics[f"{symbol}|{session}"] = _statistics(symbol, session, selected)
    return FalseOpeningRangeBreakoutLibrary(
        observed_at=canonical_observed_at.isoformat(),
        outcomes=tuple(outcomes),
        statistics=statistics,
    )


def _outcome(breakout, breakout_at: datetime, following) -> FalseOpeningRangeBreakoutOutcome:
    up = breakout.breakout_direction == "UP"
    broken_level = breakout.opening_high if up else breakout.opening_low
    opposite_level = breakout.opening_low if up else breakout.opening_high
    reclaim_index = next(
        (
            index for index, item in enumerate(following)
            if (up and item.close <= broken_level) or (not up and item.close >= broken_level)
        ),
        None,
    )
    extension_rows = following[:reclaim_index + 1] if reclaim_index is not None else following
    if up:
        maximum_extension = max(
            (item.high - broken_level for item in extension_rows),
            default=max(0.0, float(breakout.close_distance or 0.0)),
        )
        opposite_broken = any(item.close < opposite_level for item in following)
        held = bool(following and following[-1].close > broken_level)
    else:
        maximum_extension = max(
            (broken_level - item.low for item in extension_rows),
            default=max(0.0, float(breakout.close_distance or 0.0)),
        )
        opposite_broken = any(item.close > opposite_level for item in following)
        held = bool(following and following[-1].close < broken_level)
    reclaim = following[reclaim_index] if reclaim_index is not None else None
    reclaim_depth = None
    if reclaim is not None:
        reclaim_depth = broken_level - reclaim.close if up else reclaim.close - broken_level
    digest = sha256(
        (breakout.source_digest + "|" + "|".join(item.source_id for item in following)).encode("utf-8")
    ).hexdigest()
    width = breakout.opening_range_value
    return FalseOpeningRangeBreakoutOutcome(
        symbol=breakout.symbol,
        session=breakout.session,
        session_date=breakout.session_date,
        breakout_direction=breakout.breakout_direction,
        breakout_at_utc=str(breakout.breakout_at_utc),
        broken_level=broken_level,
        opposite_level=opposite_level,
        opening_range_value=width,
        reclaimed=reclaim is not None,
        reclaimed_at_utc=reclaim.end.isoformat() if reclaim is not None else None,
        minutes_to_reclaim=(
            int((reclaim.end - breakout_at).total_seconds() // 60)
            if reclaim is not None else None
        ),
        reclaim_close=reclaim.close if reclaim is not None else None,
        reclaim_depth=reclaim_depth,
        reclaim_depth_in_ranges=reclaim_depth / width if reclaim_depth is not None and width else None,
        maximum_extension_before_reclaim=max(0.0, maximum_extension),
        maximum_extension_in_ranges=max(0.0, maximum_extension) / width if width else None,
        opposite_boundary_broken=opposite_broken,
        held_outside_at_session_close=held,
        source_digest=digest,
    )


def _statistics(symbol: str, session: str, outcomes) -> FalseOpeningRangeBreakoutStatistics:
    reclaimed = tuple(item for item in outcomes if item.reclaimed)
    size = len(outcomes)
    false_count = len(reclaimed)
    return FalseOpeningRangeBreakoutStatistics(
        symbol=symbol,
        session=session,
        eligible_breakout_count=size,
        false_breakout_count=false_count,
        unreclaimed_breakout_count=size - false_count,
        upward_false_breakout_count=sum(
            item.reclaimed and item.breakout_direction == "UP" for item in outcomes
        ),
        downward_false_breakout_count=sum(
            item.reclaimed and item.breakout_direction == "DOWN" for item in outcomes
        ),
        opposite_boundary_break_count=sum(item.opposite_boundary_broken for item in outcomes),
        false_breakout_rate=false_count / size if size else None,
        unreclaimed_breakout_rate=(size - false_count) / size if size else None,
        opposite_boundary_break_rate=(
            sum(item.opposite_boundary_broken for item in outcomes) / size if size else None
        ),
        median_minutes_to_reclaim=_median(item.minutes_to_reclaim for item in reclaimed),
        median_reclaim_depth_in_ranges=_median(item.reclaim_depth_in_ranges for item in reclaimed),
        median_maximum_extension_in_ranges=_median(
            item.maximum_extension_in_ranges for item in outcomes
        ),
        status="OBSERVED" if size else "NO_BREAKOUT_HISTORY",
    )


def _median(values) -> float | None:
    selected = tuple(float(value) for value in values if value is not None)
    return median(selected) if selected else None
