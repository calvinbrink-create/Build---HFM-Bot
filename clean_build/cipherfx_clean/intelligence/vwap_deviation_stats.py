"""Historical distributions for session-anchored VWAP distance."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from math import isfinite
from statistics import mean, pstdev

from .session_vwap import SessionVWAPLibrary, SessionVWAPObservation


@dataclass(frozen=True)
class VWAPDeviationObservation:
    symbol: str
    session: str
    session_date: str
    observed_at_utc: str
    current_price: float | None
    vwap: float | None
    signed_deviation: float | None
    normalized_deviation: float | None
    absolute_normalized_deviation: float | None
    historical_sample_size: int
    historical_mean: float | None
    historical_stddev: float | None
    zscore: float | None
    absolute_distance_percentile: float | None
    source_digest: str
    source: str = "COMPLETED_SESSION_VWAP_DEVIATION_DISTRIBUTION"


@dataclass(frozen=True)
class VWAPDeviationDistribution:
    symbol: str
    session: str
    sample_size: int
    signed_mean: float | None
    signed_stddev: float | None
    absolute_distances: tuple[float, ...]
    source_digest: str


@dataclass(frozen=True)
class VWAPDeviationLibrary:
    observed_at: str
    distributions: tuple[VWAPDeviationDistribution, ...]
    observations: tuple[VWAPDeviationObservation, ...]
    source: str = "HISTORICAL_SESSION_VWAP_DEVIATION_LIBRARY"

    def distribution_for(self, symbol: str, session: str) -> VWAPDeviationDistribution | None:
        return next(
            (
                item for item in self.distributions
                if item.symbol == symbol and item.session == session
            ),
            None,
        )

    def current_for_symbol(
        self,
        symbol: str,
        observed_at: datetime,
    ) -> VWAPDeviationObservation | None:
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("VWAP deviation lookup time must be timezone-aware")
        timestamp = observed_at.astimezone(timezone.utc)
        active = tuple(
            item for item in self.observations
            if item.symbol == symbol
            and datetime.fromisoformat(item.observed_at_utc) == timestamp
        )
        priority = {"ASIA": 0, "LONDON": 1, "NEW_YORK": 2}
        return max(active, key=lambda item: priority[item.session], default=None)


def build_vwap_deviation_library(
    session_vwap_library: SessionVWAPLibrary,
    *,
    minimum_sample_size: int = 1,
) -> VWAPDeviationLibrary:
    """Build distributions from completed sessions only, then score current sessions."""

    completed = tuple(
        item for item in session_vwap_library.observations
        if item.complete_session and item.value is not None and item.last_price is not None
    )
    grouped: dict[tuple[str, str], list[tuple[SessionVWAPObservation, float]]] = {}
    for item in completed:
        normalized = (item.last_price - item.value) / item.value if item.value else None
        if normalized is not None and isfinite(normalized):
            grouped.setdefault((item.symbol, item.session), []).append((item, normalized))

    distributions: list[VWAPDeviationDistribution] = []
    for (symbol, session), values in sorted(grouped.items()):
        samples = tuple(value for _, value in values)
        if len(samples) < minimum_sample_size:
            continue
        source = "|".join(item.source_digest for item, _ in values)
        distributions.append(
            VWAPDeviationDistribution(
                symbol=symbol,
                session=session,
                sample_size=len(samples),
                signed_mean=mean(samples),
                signed_stddev=pstdev(samples) if len(samples) > 1 else 0.0,
                absolute_distances=tuple(sorted(abs(value) for value in samples)),
                source_digest=sha256(source.encode("utf-8")).hexdigest(),
            )
        )

    distribution_map = {(item.symbol, item.session): item for item in distributions}
    observations: list[VWAPDeviationObservation] = []
    for item in session_vwap_library.observations:
        if item.complete_session or item.value is None or item.last_price is None:
            continue
        distribution = distribution_map.get((item.symbol, item.session))
        signed = item.last_price - item.value
        normalized = signed / item.value if item.value else None
        absolute = abs(normalized) if normalized is not None else None
        zscore = None
        percentile = None
        if distribution is not None and normalized is not None:
            scale = distribution.signed_stddev or 0.0
            zscore = (normalized - distribution.signed_mean) / scale if scale else 0.0
            lower = sum(value <= absolute for value in distribution.absolute_distances)
            percentile = lower / distribution.sample_size if distribution.sample_size else None
        source = "|".join(
            value for value in (item.source_digest, distribution.source_digest if distribution else "")
            if value
        )
        observations.append(
            VWAPDeviationObservation(
                symbol=item.symbol,
                session=item.session,
                session_date=item.session_date,
                observed_at_utc=item.observed_through_utc,
                current_price=item.last_price,
                vwap=item.value,
                signed_deviation=signed,
                normalized_deviation=normalized,
                absolute_normalized_deviation=absolute,
                historical_sample_size=distribution.sample_size if distribution else 0,
                historical_mean=distribution.signed_mean if distribution else None,
                historical_stddev=distribution.signed_stddev if distribution else None,
                zscore=zscore,
                absolute_distance_percentile=percentile,
                source_digest=sha256(source.encode("utf-8")).hexdigest(),
            )
        )

    return VWAPDeviationLibrary(
        observed_at=session_vwap_library.observed_at,
        distributions=tuple(distributions),
        observations=tuple(observations),
    )
