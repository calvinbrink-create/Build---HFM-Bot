"""Preflight historical coverage before expensive edge research begins.

This module is intentionally research-only.  It does not participate in live
analysis, decision creation, or broker execution.  Its purpose is to prevent a
candidate sweep from being presented as meaningful when the source cannot even
supply the chronological windows required by the configured validator.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Mapping

from .hfm_data import HfmCsvMarketDataAdapter


@dataclass(frozen=True)
class HistoricalCoveragePolicy:
    """Capacity required to create a chronological research population."""

    required_timeframes: tuple[str, ...]
    minimum_bars: Mapping[str, int]
    required_state_windows: int
    lookback_bars: int
    stride_m1_bars: int
    required_fidelity: str = "HFM_EXECUTABLE_TICKS"

    def __post_init__(self) -> None:
        if self.required_state_windows <= 0 or self.lookback_bars <= 0 or self.stride_m1_bars <= 0:
            raise ValueError("state windows, lookback, and stride must be positive")
        if not self.required_timeframes or "M1" not in self.required_timeframes:
            raise ValueError("M1 is required to construct chronological market-state windows")
        if any(self.minimum_bars.get(frame, 0) <= 0 for frame in self.required_timeframes):
            raise ValueError("every required timeframe needs a positive minimum bar count")

    @property
    def required_m1_bars(self) -> int:
        """Minimum M1 observations for the required stride-separated windows."""

        return self.lookback_bars + 1 + (self.required_state_windows - 1) * self.stride_m1_bars


@dataclass(frozen=True)
class FrameCoverage:
    timeframe: str
    bars: int
    required_bars: int
    first_utc: str | None
    last_completed_utc: str | None
    status: str


@dataclass(frozen=True)
class SymbolCoverage:
    symbol: str
    status: str
    maximum_state_windows: int
    required_state_windows: int
    frames: tuple[FrameCoverage, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class HistoricalCoverageReport:
    schema_version: int
    scope: str
    observed_at_utc: str
    status: str
    policy: HistoricalCoveragePolicy
    available_historical_fidelity: str
    symbols: tuple[SymbolCoverage, ...]
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def assess_history_coverage(
    adapter: HfmCsvMarketDataAdapter,
    symbols: tuple[str, ...],
    *,
    observed_at: datetime,
    policy: HistoricalCoveragePolicy,
) -> HistoricalCoverageReport:
    """Measure source capacity without writing research state or running a sweep."""

    symbol_reports: list[SymbolCoverage] = []
    report_reasons: list[str] = []
    for symbol in symbols:
        try:
            dataset = adapter.dataset(
                symbol,
                observed_at=observed_at,
                timeframes=policy.required_timeframes,
                include_latest_tick=False,
                derive_m3_history=True,
                history_mode="combined",
            )
        except (FileNotFoundError, ValueError, OSError) as error:
            reason = f"{symbol}:SOURCE_READ_FAILED:{error}"
            report_reasons.append(reason)
            symbol_reports.append(
                SymbolCoverage(symbol, "BLOCKED_INSUFFICIENT_HISTORICAL_COVERAGE", 0,
                               policy.required_state_windows, (), (reason,))
            )
            continue

        frames: list[FrameCoverage] = []
        reasons: list[str] = []
        for timeframe in policy.required_timeframes:
            rows = dataset.bars.get(timeframe, ())
            required_bars = policy.required_m1_bars if timeframe == "M1" else policy.minimum_bars[timeframe]
            status = "READY" if len(rows) >= required_bars else "INSUFFICIENT_BARS"
            if status != "READY":
                reasons.append(f"{symbol}:{timeframe}:BARS:{len(rows)}<{required_bars}")
            frames.append(
                FrameCoverage(
                    timeframe=timeframe,
                    bars=len(rows),
                    required_bars=required_bars,
                    first_utc=rows[0].start.isoformat() if rows else None,
                    last_completed_utc=rows[-1].end.isoformat() if rows else None,
                    status=status,
                )
            )

        m1_count = len(dataset.bars.get("M1", ()))
        maximum_windows = max(0, (m1_count - policy.lookback_bars + policy.stride_m1_bars - 1) // policy.stride_m1_bars)
        if maximum_windows < policy.required_state_windows:
            reasons.append(
                f"{symbol}:STATE_WINDOWS:{maximum_windows}<{policy.required_state_windows}"
            )
        status = "READY" if not reasons else "BLOCKED_INSUFFICIENT_HISTORICAL_COVERAGE"
        report_reasons.extend(reasons)
        symbol_reports.append(
            SymbolCoverage(symbol, status, maximum_windows, policy.required_state_windows,
                           tuple(frames), tuple(reasons))
        )

    available_fidelity = "BAR_OHLC_NO_HISTORICAL_BID_ASK_TICKS"
    fidelity_reason = (
        f"HISTORICAL_FIDELITY:{available_fidelity}!={policy.required_fidelity}"
        if policy.required_fidelity != available_fidelity
        else None
    )
    if fidelity_reason:
        report_reasons.append(fidelity_reason)
    status = "READY"
    if any(item.status != "READY" for item in symbol_reports):
        status = "BLOCKED_INSUFFICIENT_HISTORICAL_COVERAGE"
    elif fidelity_reason:
        status = "BLOCKED_INSUFFICIENT_HISTORICAL_FIDELITY"
    return HistoricalCoverageReport(
        schema_version=1,
        scope="RESEARCH_INPUT_PRECHECK_ONLY_NO_LIVE_TRADING_EFFECT",
        observed_at_utc=observed_at.isoformat(),
        status=status,
        policy=policy,
        available_historical_fidelity=available_fidelity,
        symbols=tuple(symbol_reports),
        reasons=tuple(report_reasons),
    )
