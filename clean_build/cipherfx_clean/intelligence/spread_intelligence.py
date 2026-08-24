"""Historical symbol/session/regime spread intelligence.

This module describes transaction-cost context.  It does not decide whether a
trade exists and does not turn a percentile into an execution threshold.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from math import isfinite
from statistics import mean, median, pstdev
from typing import Iterable, Sequence

from ..contracts import Candle
from .regime import classify_regime
from .session import session_label


@dataclass(frozen=True)
class HistoricalSpreadSample:
    symbol: str
    observed_at: datetime
    session: str
    regime: str
    spread_price: float
    source_id: str
    source: str = "HFM_MT5_M1_SPREAD_POINTS_X_CONTRACT_POINT"

    def __post_init__(self) -> None:
        if not self.symbol or not self.session or not self.regime or not self.source_id:
            raise ValueError("spread sample identity and source lineage are required")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("spread sample timestamp must be timezone-aware")
        if not isfinite(self.spread_price) or self.spread_price < 0:
            raise ValueError("spread sample must be finite and non-negative")


@dataclass(frozen=True)
class HistoricalSpreadProfile:
    profile_id: str
    symbol: str
    session: str
    regime: str
    sample_size: int
    historical_start: datetime
    historical_end: datetime
    minimum: float
    mean: float
    median: float
    standard_deviation: float
    median_absolute_deviation: float
    p90: float
    p95: float
    p99: float
    maximum: float
    distribution: tuple[float, ...]
    source_digest: str
    status: str


@dataclass(frozen=True)
class SpreadIntelligenceObservation:
    symbol: str
    observed_at: datetime
    session: str
    regime: str
    current_spread: float
    profile_id: str | None
    sample_size: int
    historical_start: datetime | None
    historical_end: datetime | None
    historical_mean: float | None
    historical_median: float | None
    historical_p90: float | None
    historical_p95: float | None
    historical_p99: float | None
    percentile: float | None
    zscore: float | None
    robust_zscore: float | None
    ratio_to_median: float | None
    status: str
    source: str = "MATCHED_SYMBOL_SESSION_REGIME_SPREAD_PROFILE"
    role: str = "DESCRIPTIVE_RESEARCH_EVIDENCE_NOT_EXECUTION_GATE"


@dataclass(frozen=True)
class SpreadProfileBook:
    profiles: tuple[HistoricalSpreadProfile, ...]
    minimum_samples: int = 30

    def __post_init__(self) -> None:
        if self.minimum_samples <= 0:
            raise ValueError("minimum spread-profile sample count must be positive")
        keys = tuple((row.symbol, row.session, row.regime) for row in self.profiles)
        if len(keys) != len(set(keys)):
            raise ValueError("spread profile book contains duplicate keys")

    def profile(
        self,
        symbol: str,
        session: str,
        regime: str,
    ) -> HistoricalSpreadProfile | None:
        return next(
            (
                row
                for row in self.profiles
                if (row.symbol, row.session, row.regime) == (symbol, session, regime)
            ),
            None,
        )

    def observe(
        self,
        *,
        symbol: str,
        observed_at: datetime,
        session: str,
        regime: str,
        current_spread: float,
    ) -> SpreadIntelligenceObservation:
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("current spread timestamp must be timezone-aware")
        if not isfinite(current_spread) or current_spread < 0:
            raise ValueError("current spread must be finite and non-negative")
        profile = self.profile(symbol, session, regime)
        if profile is None:
            return SpreadIntelligenceObservation(
                symbol=symbol,
                observed_at=observed_at,
                session=session,
                regime=regime,
                current_spread=current_spread,
                profile_id=None,
                sample_size=0,
                historical_start=None,
                historical_end=None,
                historical_mean=None,
                historical_median=None,
                historical_p90=None,
                historical_p95=None,
                historical_p99=None,
                percentile=None,
                zscore=None,
                robust_zscore=None,
                ratio_to_median=None,
                status="NO_MATCHING_PROFILE",
            )

        zscore = (
            (current_spread - profile.mean) / profile.standard_deviation
            if profile.standard_deviation > 0
            else 0.0
        )
        robust_scale = 1.4826 * profile.median_absolute_deviation
        robust_zscore = (
            (current_spread - profile.median) / robust_scale
            if robust_scale > 0
            else 0.0
        )
        empirical_percentile = (
            bisect_right(profile.distribution, current_spread) / profile.sample_size
            if profile.sample_size
            else 0.0
        )
        return SpreadIntelligenceObservation(
            symbol=symbol,
            observed_at=observed_at,
            session=session,
            regime=regime,
            current_spread=current_spread,
            profile_id=profile.profile_id,
            sample_size=profile.sample_size,
            historical_start=profile.historical_start,
            historical_end=profile.historical_end,
            historical_mean=profile.mean,
            historical_median=profile.median,
            historical_p90=profile.p90,
            historical_p95=profile.p95,
            historical_p99=profile.p99,
            percentile=empirical_percentile,
            zscore=zscore,
            robust_zscore=robust_zscore,
            ratio_to_median=(
                current_spread / profile.median
                if profile.median > 0
                else 1.0 if current_spread == 0 else None
            ),
            status=(
                "MATCHED"
                if profile.sample_size >= self.minimum_samples
                else "INSUFFICIENT_HISTORY"
            ),
        )


def historical_spread_samples(
    symbol: str,
    candles: Sequence[Candle],
    *,
    point_size: float,
    regime_lookback: int = 20,
) -> Iterable[HistoricalSpreadSample]:
    if not symbol or not isfinite(point_size) or point_size <= 0:
        raise ValueError("symbol and positive broker point size are required")
    if regime_lookback < 3:
        raise ValueError("spread regime lookback must be at least three bars")
    rows = tuple(sorted(candles, key=lambda item: (item.start, item.source_id)))
    for index, row in enumerate(rows):
        if row.symbol != symbol or row.timeframe != "M1" or not row.source_id:
            raise ValueError("spread history requires sourced M1 bars for one symbol")
        regime = classify_regime(
            rows[max(0, index - regime_lookback + 1):index + 1]
        ).label
        yield HistoricalSpreadSample(
            symbol=symbol,
            observed_at=row.end,
            session=session_label(row.end),
            regime=regime,
            spread_price=float(
                Decimal(str(row.spread_points)) * Decimal(str(point_size))
            ),
            source_id=row.source_id,
        )


def build_spread_profile_book(
    samples: Iterable[HistoricalSpreadSample],
    *,
    minimum_samples: int = 30,
) -> SpreadProfileBook:
    if minimum_samples <= 0:
        raise ValueError("minimum spread-profile sample count must be positive")
    grouped: dict[tuple[str, str, str], list[HistoricalSpreadSample]] = {}
    for sample in samples:
        grouped.setdefault((sample.symbol, sample.session, sample.regime), []).append(sample)
    profiles = []
    for key, group in sorted(grouped.items()):
        rows = tuple(sorted(group, key=lambda item: (item.observed_at, item.source_id)))
        values = tuple(sorted(item.spread_price for item in rows))
        center = median(values)
        mad = median(tuple(abs(value - center) for value in values))
        source_digest = sha256(
            "\n".join(item.source_id for item in rows).encode("utf-8")
        ).hexdigest()
        payload = (
            key,
            len(rows),
            rows[0].observed_at.isoformat(),
            rows[-1].observed_at.isoformat(),
            source_digest,
        )
        profile_id = sha256(repr(payload).encode("utf-8")).hexdigest()
        profiles.append(
            HistoricalSpreadProfile(
                profile_id=profile_id,
                symbol=key[0],
                session=key[1],
                regime=key[2],
                sample_size=len(rows),
                historical_start=rows[0].observed_at,
                historical_end=rows[-1].observed_at,
                minimum=values[0],
                mean=mean(values),
                median=center,
                standard_deviation=pstdev(values) if len(values) > 1 else 0.0,
                median_absolute_deviation=mad,
                p90=_percentile(values, 0.90),
                p95=_percentile(values, 0.95),
                p99=_percentile(values, 0.99),
                maximum=values[-1],
                distribution=values,
                source_digest=source_digest,
                status="COMPLETE" if len(rows) >= minimum_samples else "INSUFFICIENT_SAMPLES",
            )
        )
    return SpreadProfileBook(tuple(profiles), minimum_samples)


def _percentile(values: Sequence[float], quantile: float) -> float:
    if not values:
        return 0.0
    position = (len(values) - 1) * quantile
    lower = int(position)
    upper = min(len(values) - 1, lower + 1)
    fraction = position - lower
    interpolated = values[lower] + (values[upper] - values[lower]) * fraction
    return min(values[upper], max(values[lower], interpolated))
