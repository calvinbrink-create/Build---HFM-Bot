"""Historical outcomes after session VWAP reclaim and rejection events."""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from statistics import mean, median
from typing import Mapping, Sequence

from ..market import Candle
from .session_vwap import SessionVWAPLibrary


EVENT_TYPES = ("RECLAIM_UP", "RECLAIM_DOWN", "REJECTION_UP", "REJECTION_DOWN")


@dataclass(frozen=True)
class VWAPReclaimRejectionOutcome:
    symbol: str
    session: str
    session_date: str
    event_type: str
    event_at_utc: str
    event_price: float
    vwap_at_event: float
    direction: str
    forward_bars: int
    forward_price: float
    signed_forward_return: float
    maximum_favourable_excursion: float
    maximum_adverse_excursion: float
    favourable: bool
    source_digest: str
    source: str = "HFM_M1_EVOLVING_SESSION_VWAP_RECLAIM_REJECTION_OUTCOME"


@dataclass(frozen=True)
class VWAPReclaimRejectionStatistics:
    symbol: str
    session: str
    event_type: str
    sample_count: int
    favourable_count: int
    favourable_rate: float | None
    mean_signed_forward_return: float | None
    median_signed_forward_return: float | None
    mean_favourable_excursion: float | None
    mean_adverse_excursion: float | None
    status: str
    source_digest: str
    source: str = "EMPIRICAL_VWAP_RECLAIM_REJECTION_STATISTICS"


@dataclass(frozen=True)
class VWAPReclaimRejectionLibrary:
    observed_at: str
    outcomes: tuple[VWAPReclaimRejectionOutcome, ...]
    statistics: Mapping[str, VWAPReclaimRejectionStatistics]
    horizon_bars: int
    source: str = "HISTORICAL_VWAP_RECLAIM_REJECTION_LIBRARY"

    def outcomes_for_symbol(self, symbol: str) -> tuple[VWAPReclaimRejectionOutcome, ...]:
        return tuple(item for item in self.outcomes if item.symbol == symbol)

    def statistics_for_symbol(self, symbol: str) -> Mapping[str, VWAPReclaimRejectionStatistics]:
        prefix = f"{symbol}|"
        return {
            key.removeprefix(prefix): value
            for key, value in self.statistics.items()
            if key.startswith(prefix)
        }


def build_vwap_reclaim_rejection_library(
    candles_by_symbol: Mapping[str, Sequence[Candle]],
    *,
    observed_at: datetime,
    session_vwap_library: SessionVWAPLibrary,
    horizon_bars: int = 15,
) -> VWAPReclaimRejectionLibrary:
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("VWAP reclaim/rejection observation time must be timezone-aware")
    if horizon_bars <= 0:
        raise ValueError("VWAP reclaim/rejection horizon must be positive")
    canonical_observed_at = observed_at.astimezone(timezone.utc)
    outcomes: list[VWAPReclaimRejectionOutcome] = []
    for symbol, raw_rows in sorted(candles_by_symbol.items()):
        rows = tuple(sorted(raw_rows, key=lambda item: item.start))
        starts = tuple(item.start for item in rows)
        completed_sessions = tuple(
            item for item in session_vwap_library.observations
            if item.symbol == symbol and item.complete_session
        )
        for session in completed_sessions:
            start = datetime.fromisoformat(session.session_start_utc)
            end = datetime.fromisoformat(session.session_end_utc)
            selected = rows[bisect_left(starts, start):bisect_left(starts, end)]
            outcomes.extend(
                _session_outcomes(
                    symbol,
                    session.session,
                    session.session_date,
                    selected,
                    horizon_bars,
                )
            )
    statistics: dict[str, VWAPReclaimRejectionStatistics] = {}
    for symbol in sorted(candles_by_symbol):
        for session in ("ASIA", "LONDON", "NEW_YORK"):
            for event_type in EVENT_TYPES:
                selected = tuple(
                    item for item in outcomes
                    if item.symbol == symbol
                    and item.session == session
                    and item.event_type == event_type
                )
                statistics[f"{symbol}|{session}|{event_type}"] = _statistics(
                    symbol, session, event_type, selected
                )
    return VWAPReclaimRejectionLibrary(
        observed_at=canonical_observed_at.isoformat(),
        outcomes=tuple(outcomes),
        statistics=statistics,
        horizon_bars=horizon_bars,
    )


def _session_outcomes(symbol, session, session_date, rows, horizon_bars):
    if len(rows) <= horizon_bars + 1:
        return ()
    cumulative_volume = 0.0
    cumulative_value = 0.0
    values: list[tuple[Candle, float]] = []
    for row in rows:
        volume = max(0.0, float(row.tick_volume))
        cumulative_volume += volume
        cumulative_value += ((row.high + row.low + row.close) / 3.0) * volume
        value = cumulative_value / cumulative_volume if cumulative_volume else row.close
        values.append((row, value))
    events: list[tuple[int, str, float]] = []
    for index in range(1, len(values)):
        prior, prior_vwap = values[index - 1]
        current, current_vwap = values[index]
        event_type = _event_type(prior, prior_vwap, current, current_vwap)
        if event_type is not None:
            events.append((index, event_type, current_vwap))
    output = []
    for index, event_type, event_vwap in events:
        following = rows[index + 1:index + 1 + horizon_bars]
        if len(following) < horizon_bars:
            continue
        current = rows[index]
        direction = "UP" if event_type.endswith("UP") else "DOWN"
        signed = following[-1].close - current.close
        if direction == "DOWN":
            signed = -signed
            mfe = current.close - min(item.low for item in following)
            mae = max(item.high for item in following) - current.close
        else:
            mfe = max(item.high for item in following) - current.close
            mae = current.close - min(item.low for item in following)
        digest = sha256(
            "|".join(item.source_id for item in (rows[index], *following)).encode("utf-8")
        ).hexdigest()
        output.append(
            VWAPReclaimRejectionOutcome(
                symbol=symbol,
                session=session,
                session_date=session_date,
                event_type=event_type,
                event_at_utc=current.end.isoformat(),
                event_price=current.close,
                vwap_at_event=event_vwap,
                direction=direction,
                forward_bars=len(following),
                forward_price=following[-1].close,
                signed_forward_return=signed,
                maximum_favourable_excursion=max(0.0, mfe),
                maximum_adverse_excursion=max(0.0, mae),
                favourable=signed > 0.0,
                source_digest=digest,
            )
        )
    return tuple(output)


def _event_type(prior, prior_vwap, current, current_vwap):
    if prior.close <= prior_vwap and current.close > current_vwap:
        return "RECLAIM_UP"
    if prior.close >= prior_vwap and current.close < current_vwap:
        return "RECLAIM_DOWN"
    if current.open >= current_vwap and current.high >= current_vwap and current.close < current_vwap:
        return "REJECTION_DOWN"
    if current.open <= current_vwap and current.low <= current_vwap and current.close > current_vwap:
        return "REJECTION_UP"
    return None


def _statistics(symbol, session, event_type, outcomes):
    size = len(outcomes)
    source = "|".join(item.source_digest for item in outcomes)
    return VWAPReclaimRejectionStatistics(
        symbol=symbol,
        session=session,
        event_type=event_type,
        sample_count=size,
        favourable_count=sum(item.favourable for item in outcomes),
        favourable_rate=sum(item.favourable for item in outcomes) / size if size else None,
        mean_signed_forward_return=mean(item.signed_forward_return for item in outcomes) if outcomes else None,
        median_signed_forward_return=median(item.signed_forward_return for item in outcomes) if outcomes else None,
        mean_favourable_excursion=mean(item.maximum_favourable_excursion for item in outcomes) if outcomes else None,
        mean_adverse_excursion=mean(item.maximum_adverse_excursion for item in outcomes) if outcomes else None,
        status="OBSERVED" if size else "NO_EVENTS",
        source_digest=sha256(source.encode("utf-8")).hexdigest(),
    )
