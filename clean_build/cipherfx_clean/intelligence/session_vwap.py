"""Canonical session-anchored VWAP from broker M1 activity weights."""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from typing import Mapping, Sequence

from ..market import Candle
from .session import session_intervals
from .session_profiles import build_session_profile_library


VWAP_SESSIONS = ("ASIA", "LONDON", "NEW_YORK")


@dataclass(frozen=True)
class SessionVWAPObservation:
    symbol: str
    session: str
    session_date: str
    session_start_utc: str
    session_end_utc: str
    observed_through_utc: str
    complete_session: bool
    candle_count: int
    expected_candle_count: int
    coverage_ratio: float
    activity_weight: float
    value: float | None
    last_price: float | None
    deviation: float | None
    position: str
    source_digest: str
    source: str = "HFM_M1_TICK_VOLUME_WEIGHTED_TYPICAL_PRICE_IANA_SESSION"


@dataclass(frozen=True)
class SessionVWAPLibrary:
    observed_at: str
    observations: tuple[SessionVWAPObservation, ...]
    source: str = "HISTORICAL_AND_CURRENT_SESSION_VWAP_LIBRARY"

    def observations_for_symbol(self, symbol: str) -> tuple[SessionVWAPObservation, ...]:
        return tuple(item for item in self.observations if item.symbol == symbol)

    def current_for_symbol(
        self,
        symbol: str,
        observed_at: datetime,
    ) -> SessionVWAPObservation | None:
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("VWAP lookup time must be timezone-aware")
        timestamp = observed_at.astimezone(timezone.utc)
        active = tuple(
            item for item in self.observations_for_symbol(symbol)
            if datetime.fromisoformat(item.session_start_utc) <= timestamp
            < datetime.fromisoformat(item.session_end_utc)
        )
        priority = {"ASIA": 0, "LONDON": 1, "NEW_YORK": 2}
        return max(active, key=lambda item: priority[item.session], default=None)


def build_session_vwap_library(
    candles_by_symbol: Mapping[str, Sequence[Candle]],
    *,
    observed_at: datetime,
    minimum_coverage_ratio: float = 0.80,
) -> SessionVWAPLibrary:
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("session VWAP observation time must be timezone-aware")
    canonical_observed_at = observed_at.astimezone(timezone.utc)
    profiles = build_session_profile_library(
        candles_by_symbol,
        observed_at=canonical_observed_at,
        minimum_coverage_ratio=minimum_coverage_ratio,
    )
    observations: list[SessionVWAPObservation] = []
    for symbol, raw_rows in sorted(candles_by_symbol.items()):
        rows = tuple(sorted(raw_rows, key=lambda item: item.start))
        starts = tuple(item.start for item in rows)
        completed_windows = tuple(
            item for item in profiles.observations
            if item.symbol == symbol and item.session in VWAP_SESSIONS and item.complete
        )
        for window in completed_windows:
            start = datetime.fromisoformat(window.start_utc)
            end = datetime.fromisoformat(window.end_utc)
            selected = rows[bisect_left(starts, start):bisect_left(starts, end)]
            observations.append(
                _observation(symbol, window.session, window.session_date, start, end, end, selected, True)
            )
        intervals = {item.session_id: item for item in session_intervals(canonical_observed_at)}
        for session in VWAP_SESSIONS:
            window = intervals[session]
            if not window.start_utc <= canonical_observed_at < window.end_utc:
                continue
            selected = rows[
                bisect_left(starts, window.start_utc):bisect_left(starts, canonical_observed_at)
            ]
            observations.append(
                _observation(
                    symbol,
                    session,
                    canonical_observed_at.date().isoformat(),
                    window.start_utc,
                    window.end_utc,
                    canonical_observed_at,
                    selected,
                    False,
                )
            )
    return SessionVWAPLibrary(canonical_observed_at.isoformat(), tuple(observations))


def _observation(symbol, session, session_date, start, end, observed_through, rows, complete):
    expected_end = end if complete else observed_through
    expected = max(0, int((expected_end - start).total_seconds() // 60))
    unique_count = len({item.start for item in rows})
    coverage = unique_count / expected if expected else 0.0
    activity = sum(max(0.0, item.tick_volume) for item in rows)
    last = rows[-1].close if rows else None
    value = None
    if activity > 0:
        value = sum(
            ((item.high + item.low + item.close) / 3.0) * max(0.0, item.tick_volume)
            for item in rows
        ) / activity
    deviation = last - value if last is not None and value is not None else None
    position = "ABOVE" if deviation is not None and deviation > 0 else "BELOW" if deviation is not None and deviation < 0 else "AT" if deviation == 0 else "UNAVAILABLE"
    digest = sha256("|".join(item.source_id for item in rows).encode("utf-8")).hexdigest()
    return SessionVWAPObservation(
        symbol=symbol,
        session=session,
        session_date=session_date,
        session_start_utc=start.isoformat(),
        session_end_utc=end.isoformat(),
        observed_through_utc=observed_through.isoformat(),
        complete_session=complete,
        candle_count=unique_count,
        expected_candle_count=expected,
        coverage_ratio=coverage,
        activity_weight=activity,
        value=value,
        last_price=last,
        deviation=deviation,
        position=position,
        source_digest=digest,
    )
