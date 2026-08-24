"""Empirical per-instrument behavior grouped by canonical exchange session."""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from statistics import mean, median, pstdev
from typing import Mapping, Sequence

from ..market import Candle
from .session import session_intervals


PROFILE_SESSIONS = ("ASIA", "LONDON", "NEW_YORK", "LONDON_NY_OVERLAP")


@dataclass(frozen=True)
class SessionBehaviorObservation:
    symbol: str
    session: str
    session_date: str
    start_utc: str
    end_utc: str
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
    return_fraction: float | None
    range_fraction: float | None
    direction: str
    tick_activity: float | None
    mean_spread_points: float | None
    close_location: float | None
    source_digest: str


@dataclass(frozen=True)
class InstrumentSessionProfile:
    symbol: str
    session: str
    sample_size: int
    excluded_incomplete_sessions: int
    mean_return: float | None
    median_return: float | None
    return_stddev: float | None
    positive_rate: float | None
    negative_rate: float | None
    flat_rate: float | None
    mean_range_fraction: float | None
    median_range_fraction: float | None
    range_p25: float | None
    range_p75: float | None
    range_p95: float | None
    mean_tick_activity: float | None
    mean_spread_points: float | None
    mean_close_location: float | None
    first_session_date: str | None
    last_session_date: str | None
    status: str
    source: str = "HFM_MT5_M1_COMPLETED_SESSIONS_IANA_EXCHANGE_CLOCKS"


@dataclass(frozen=True)
class SessionProfileLibrary:
    observed_at: str
    profiles: Mapping[str, InstrumentSessionProfile]
    observations: tuple[SessionBehaviorObservation, ...]
    minimum_coverage_ratio: float
    source: str = "HISTORICAL_INSTRUMENT_SESSION_BEHAVIOR"

    def profiles_for_symbol(self, symbol: str) -> Mapping[str, InstrumentSessionProfile]:
        return {
            session: self.profiles[f"{symbol}|{session}"]
            for session in PROFILE_SESSIONS
            if f"{symbol}|{session}" in self.profiles
        }

    def observations_for_symbol_session(
        self,
        symbol: str,
        session: str,
        *,
        complete_only: bool = True,
    ) -> tuple[SessionBehaviorObservation, ...]:
        if session not in PROFILE_SESSIONS:
            raise ValueError(f"unknown profile session: {session}")
        return tuple(
            item
            for item in self.observations
            if item.symbol == symbol
            and item.session == session
            and (item.complete or not complete_only)
        )


def build_session_profile_library(
    candles_by_symbol: Mapping[str, Sequence[Candle]],
    *,
    observed_at: datetime,
    minimum_coverage_ratio: float = 0.80,
) -> SessionProfileLibrary:
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("session profile observation time must be timezone-aware")
    if not 0.0 < minimum_coverage_ratio <= 1.0:
        raise ValueError("minimum session coverage must be in (0, 1]")
    canonical_observed_at = observed_at.astimezone(timezone.utc)
    observations: list[SessionBehaviorObservation] = []
    profiles: dict[str, InstrumentSessionProfile] = {}
    for symbol, raw_rows in sorted(candles_by_symbol.items()):
        rows = tuple(sorted(raw_rows, key=lambda item: item.start))
        if any(item.symbol != symbol or item.timeframe != "M1" for item in rows):
            raise ValueError(f"session profiles require {symbol} M1 candles only")
        if any(item.start.tzinfo is None or item.end.tzinfo is None for item in rows):
            raise ValueError("session profile candles must be timezone-aware")
        symbol_observations = _session_observations(
            symbol,
            rows,
            canonical_observed_at,
            minimum_coverage_ratio,
        )
        observations.extend(symbol_observations)
        for session in PROFILE_SESSIONS:
            selected = tuple(item for item in symbol_observations if item.session == session)
            complete = tuple(item for item in selected if item.complete)
            profiles[f"{symbol}|{session}"] = _profile(symbol, session, complete, len(selected) - len(complete))
    return SessionProfileLibrary(
        observed_at=canonical_observed_at.isoformat(),
        profiles=profiles,
        observations=tuple(observations),
        minimum_coverage_ratio=minimum_coverage_ratio,
    )


def _session_observations(
    symbol: str,
    rows: Sequence[Candle],
    observed_at: datetime,
    minimum_coverage_ratio: float,
) -> tuple[SessionBehaviorObservation, ...]:
    if not rows:
        return ()
    starts = tuple(item.start for item in rows)
    first_date = rows[0].start.astimezone(timezone.utc).date()
    last_date = min(rows[-1].end, observed_at).astimezone(timezone.utc).date()
    output: list[SessionBehaviorObservation] = []
    current_date = first_date
    while current_date <= last_date:
        anchor = datetime.combine(current_date, datetime.min.time(), timezone.utc) + timedelta(hours=12)
        intervals = {item.session_id: item for item in session_intervals(anchor)}
        london = intervals["LONDON"]
        new_york = intervals["NEW_YORK"]
        windows = {
            **{key: intervals[key] for key in ("ASIA", "LONDON", "NEW_YORK")},
            "LONDON_NY_OVERLAP": type(london)(
                "LONDON_NY_OVERLAP",
                max(london.start_utc, new_york.start_utc),
                min(london.end_utc, new_york.end_utc),
                "Europe/London+America/New_York",
            ),
        }
        if current_date.weekday() < 5:
            for session in PROFILE_SESSIONS:
                window = windows[session]
                if window.end_utc <= observed_at:
                    start_index = bisect_left(starts, window.start_utc)
                    end_index = bisect_left(starts, window.end_utc)
                    selected = tuple(rows[start_index:end_index])
                    output.append(
                        _observation(
                            symbol,
                            session,
                            current_date.isoformat(),
                            window.start_utc,
                            window.end_utc,
                            selected,
                            minimum_coverage_ratio,
                        )
                    )
        current_date += timedelta(days=1)
    return tuple(output)


def _observation(
    symbol: str,
    session: str,
    session_date: str,
    start: datetime,
    end: datetime,
    rows: Sequence[Candle],
    minimum_coverage_ratio: float,
) -> SessionBehaviorObservation:
    expected = int((end - start).total_seconds() // 60)
    unique_minutes = len({item.start for item in rows})
    coverage = unique_minutes / expected if expected else 0.0
    complete = bool(rows) and coverage >= minimum_coverage_ratio
    if not rows:
        return SessionBehaviorObservation(
            symbol, session, session_date, start.isoformat(), end.isoformat(), 0,
            expected, coverage, False, None, None, None, None, None, None, None, None,
            "NONE", None, None, None, sha256(b"").hexdigest(),
        )
    opening = rows[0].open
    high = max(item.high for item in rows)
    low = min(item.low for item in rows)
    closing = rows[-1].close
    session_range = high - low
    price_return = (closing - opening) / opening if opening else 0.0
    range_fraction = session_range / opening if opening else 0.0
    close_location = (closing - low) / session_range if session_range else 0.5
    activity = sum(item.tick_volume for item in rows)
    spreads = tuple(item.spread_points for item in rows if item.spread_points >= 0)
    digest = sha256("|".join(item.source_id for item in rows).encode("utf-8")).hexdigest()
    return SessionBehaviorObservation(
        symbol=symbol,
        session=session,
        session_date=session_date,
        start_utc=start.isoformat(),
        end_utc=end.isoformat(),
        candle_count=unique_minutes,
        expected_candle_count=expected,
        coverage_ratio=coverage,
        complete=complete,
        open_price=opening,
        high_price=high,
        low_price=low,
        close_price=closing,
        midpoint_price=(high + low) / 2.0,
        range_value=session_range,
        return_fraction=price_return,
        range_fraction=range_fraction,
        direction="UP" if price_return > 0 else "DOWN" if price_return < 0 else "FLAT",
        tick_activity=activity,
        mean_spread_points=mean(spreads) if spreads else None,
        close_location=close_location,
        source_digest=digest,
    )


def _profile(
    symbol: str,
    session: str,
    observations: Sequence[SessionBehaviorObservation],
    excluded: int,
) -> InstrumentSessionProfile:
    returns = tuple(float(item.return_fraction) for item in observations if item.return_fraction is not None)
    ranges = tuple(float(item.range_fraction) for item in observations if item.range_fraction is not None)
    activities = tuple(float(item.tick_activity) for item in observations if item.tick_activity is not None)
    spreads = tuple(float(item.mean_spread_points) for item in observations if item.mean_spread_points is not None)
    close_locations = tuple(float(item.close_location) for item in observations if item.close_location is not None)
    size = len(observations)
    return InstrumentSessionProfile(
        symbol=symbol,
        session=session,
        sample_size=size,
        excluded_incomplete_sessions=excluded,
        mean_return=mean(returns) if returns else None,
        median_return=median(returns) if returns else None,
        return_stddev=pstdev(returns) if len(returns) > 1 else 0.0 if returns else None,
        positive_rate=sum(value > 0 for value in returns) / size if size else None,
        negative_rate=sum(value < 0 for value in returns) / size if size else None,
        flat_rate=sum(value == 0 for value in returns) / size if size else None,
        mean_range_fraction=mean(ranges) if ranges else None,
        median_range_fraction=median(ranges) if ranges else None,
        range_p25=_quantile(ranges, 0.25),
        range_p75=_quantile(ranges, 0.75),
        range_p95=_quantile(ranges, 0.95),
        mean_tick_activity=mean(activities) if activities else None,
        mean_spread_points=mean(spreads) if spreads else None,
        mean_close_location=mean(close_locations) if close_locations else None,
        first_session_date=observations[0].session_date if observations else None,
        last_session_date=observations[-1].session_date if observations else None,
        status="OBSERVED" if observations else "NO_COMPLETE_HISTORY",
    )


def _quantile(values: Sequence[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight
