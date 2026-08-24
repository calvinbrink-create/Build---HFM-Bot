"""Dataset quality evidence; quality findings never become strategy rules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Mapping, Sequence

from .calendar import TradingCalendar
from .contracts import Candle
from .hfm_data import DataIssue, SourceDataset, timeframe_end, valid_hfm_normalized_bar_duration


@dataclass(frozen=True)
class DataQualityPolicy:
    required_timeframes: tuple[str, ...]
    minimum_bars: Mapping[str, int]
    maximum_age: Mapping[str, timedelta]
    maximum_tick_age: timedelta


@dataclass(frozen=True)
class FrameQuality:
    timeframe: str
    status: str
    bars: int
    latest_completed_at: str | None
    age_seconds: float | None
    duplicates: int
    malformed: int
    unexplained_gaps: int
    source_kinds: tuple[str, ...]


@dataclass(frozen=True)
class DatasetQualityReport:
    dataset_id: str
    symbol: str
    observed_at: str
    status: str
    frames: Mapping[str, FrameQuality]
    issues: tuple[DataIssue, ...]
    market_state: str


@dataclass(frozen=True)
class ResearchWindowQuality:
    status: str
    timeframe: str
    bars: int
    reasons: tuple[str, ...]


def validate_dataset(
    dataset: SourceDataset,
    *,
    observed_at: datetime,
    policy: DataQualityPolicy,
    calendar: TradingCalendar,
) -> DatasetQualityReport:
    observed = observed_at.astimezone(timezone.utc)
    market_state = calendar.state(observed).status
    issues: list[DataIssue] = list(dataset.issues)
    frames: dict[str, FrameQuality] = {}
    for timeframe in policy.required_timeframes:
        rows = tuple(dataset.bars.get(timeframe, ()))
        duplicates = len(rows) - len({row.start for row in rows})
        malformed = sum(not _valid_bar(row, timeframe, observed) for row in rows)
        gaps = _unexplained_gaps(rows, timeframe, calendar)
        latest = rows[-1].end if rows else None
        age = (observed - latest).total_seconds() if latest is not None else None
        status = "VALID"
        if not rows:
            status = "MISSING"
            issues.append(DataIssue("MISSING_BARS", "no completed bars", dataset.symbol, timeframe))
        elif len(rows) < policy.minimum_bars.get(timeframe, 1):
            status = "INSUFFICIENT"
            issues.append(DataIssue("INSUFFICIENT_BARS", str(len(rows)), dataset.symbol, timeframe))
        elif malformed:
            status = "MALFORMED"
            issues.append(DataIssue("MALFORMED_BARS", str(malformed), dataset.symbol, timeframe))
        elif duplicates:
            status = "DUPLICATE"
            issues.append(DataIssue("DUPLICATE_BARS", str(duplicates), dataset.symbol, timeframe))
        elif gaps:
            status = "VALID_WITH_GAPS"
            issues.append(DataIssue("OBSERVED_BAR_GAPS", str(gaps), dataset.symbol, timeframe))
        elif market_state == "OPEN" and age is not None and age > policy.maximum_age[timeframe].total_seconds():
            status = "STALE"
            issues.append(DataIssue("STALE_BARS", str(age), dataset.symbol, timeframe))
        elif market_state == "CLOSED" and age is not None and age > policy.maximum_age[timeframe].total_seconds():
            status = "MARKET_CLOSED"
        frames[timeframe] = FrameQuality(
            timeframe,
            status,
            len(rows),
            latest.isoformat() if latest else None,
            age,
            duplicates,
            malformed,
            gaps,
            tuple(sorted({row.source for row in rows})),
        )
    if dataset.ticks:
        tick_age = (observed - dataset.ticks[-1].timestamp).total_seconds()
        if market_state == "OPEN" and tick_age > policy.maximum_tick_age.total_seconds():
            issues.append(DataIssue("STALE_TICK", str(tick_age), dataset.symbol, "TICK"))
    accepted = ("VALID", "VALID_WITH_GAPS", "MARKET_CLOSED")
    status = "PASS" if all(row.status in accepted for row in frames.values()) and not _fatal(issues) else "FAIL"
    return DatasetQualityReport(dataset.dataset_id, dataset.symbol, observed.isoformat(), status, frames, tuple(_unique(issues)), market_state)


def validate_research_window(
    rows: Sequence[Candle],
    *,
    timeframe: str,
    calendar: TradingCalendar,
    minimum_bars: int,
) -> ResearchWindowQuality:
    reasons: list[str] = []
    if len(rows) < minimum_bars:
        reasons.append("INSUFFICIENT_BARS")
    if len(rows) != len({row.start for row in rows}):
        reasons.append("DUPLICATE_BARS")
    if rows:
        observed = rows[-1].end
        if any(not _valid_bar(row, timeframe, observed) for row in rows):
            reasons.append("MALFORMED_OR_FORMING_BAR")
        if _unexplained_gaps(rows, timeframe, calendar):
            reasons.append("OBSERVED_BAR_GAP")
    return ResearchWindowQuality("CLEAN" if not reasons else "EXCLUDE", timeframe, len(rows), tuple(reasons))


def _valid_bar(row: Candle, timeframe: str, observed: datetime) -> bool:
    normalized_hfm = row.source.startswith("HFM_MT5_CSV_UTC_NORMALIZED")
    valid_duration = (
        valid_hfm_normalized_bar_duration(row.start, row.end, timeframe)
        if normalized_hfm
        else row.end == timeframe_end(row.start, timeframe)
    )
    return (
        row.symbol != ""
        and row.timeframe == timeframe
        and row.start.tzinfo is not None
        and row.start.utcoffset() == timedelta(0)
        and valid_duration
        and row.end <= observed
        and min(row.open, row.high, row.low, row.close) > 0
        and row.low <= min(row.open, row.close) <= max(row.open, row.close) <= row.high
    )


def _unexplained_gaps(rows: Sequence[Candle], timeframe: str, calendar: TradingCalendar) -> int:
    if timeframe in ("D1", "W1", "MN1"):
        return 0
    gaps = 0
    for previous, current in zip(rows, rows[1:]):
        expected = (
            previous.end
            if previous.source.startswith("HFM_MT5_CSV_UTC_NORMALIZED")
            else timeframe_end(previous.start, timeframe)
        )
        if current.start <= expected:
            continue
        if calendar.expected_open(expected, current.start, step=min(current.start - expected, timedelta(minutes=15))):
            gaps += 1
    return gaps


def _fatal(issues: Sequence[DataIssue]) -> bool:
    fatal = {
        "IMPORT_ERROR", "MISSING_EXPORT", "MISSING_BARS", "INVALID_OHLC", "NON_UTC_BAR",
        "DUPLICATE_BAR", "NON_INCREASING_BAR", "INVALID_TICK", "NON_INCREASING_TICK",
        "INSUFFICIENT_BARS", "MALFORMED_BARS", "DUPLICATE_BARS",
        "STALE_BARS", "STALE_TICK",
    }
    return any(item.code in fatal for item in issues)


def _unique(issues: Sequence[DataIssue]) -> tuple[DataIssue, ...]:
    return tuple(dict.fromkeys(issues))
