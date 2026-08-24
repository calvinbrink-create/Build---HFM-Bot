"""Outcome records for detected chart geometry.

Pattern names are descriptive observations.  This module records what the
market did after a completed pattern so a pattern cannot be treated as a
proven edge merely because its textbook name was detected.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Mapping, Sequence

from ..market import Candle
from .patterns import GeometricPattern, detect_geometric_patterns


@dataclass(frozen=True)
class PatternOutcomeRecord:
    outcome_id: str
    pattern_id: str
    symbol: str
    timeframe: str
    observed_at: datetime
    direction: str
    entry_price: float
    terminal_move: float
    max_favourable: float
    max_adverse: float
    outcome_kind: str
    confirmed: bool
    failed: bool
    source_ids: tuple[str, ...] = ()


class PatternOutcomeLibrary:
    def __init__(self, records: Sequence[PatternOutcomeRecord] = ()):
        self._records = tuple(records)
        self._by_id = {record.outcome_id: record for record in self._records}
        if len(self._by_id) != len(self._records):
            raise ValueError("duplicate pattern outcome id")

    def records(self) -> tuple[PatternOutcomeRecord, ...]:
        return self._records

    def records_for_symbol(self, symbol: str) -> tuple[PatternOutcomeRecord, ...]:
        return tuple(item for item in self._records if item.symbol == symbol)

    def failed_for_symbol(self, symbol: str) -> tuple[PatternOutcomeRecord, ...]:
        return tuple(item for item in self.records_for_symbol(symbol) if item.failed)

    def records_for_pattern(self, pattern_id: str) -> tuple[PatternOutcomeRecord, ...]:
        return tuple(item for item in self._records if item.pattern_id == pattern_id)

    def failed_patterns(self) -> tuple[PatternOutcomeRecord, ...]:
        return tuple(item for item in self._records if item.failed)


def build_pattern_outcome_library(
    candles: Mapping[str, Mapping[str, Sequence[Candle]]],
    *,
    observed_at: datetime | None = None,
    lookahead: int = 12,
    max_bars_per_frame: int = 360,
) -> PatternOutcomeLibrary:
    """Build bounded, completed-candle pattern outcomes without future leakage."""

    if lookahead < 1:
        raise ValueError("lookahead must be positive")
    records: list[PatternOutcomeRecord] = []
    for symbol, frames in candles.items():
        for timeframe, source_rows in frames.items():
            rows = tuple(source_rows)[-max_bars_per_frame:]
            if len(rows) <= lookahead + 12:
                continue
            for anchor in range(12, len(rows) - lookahead + 1):
                window_start = max(0, anchor - 60)
                window = rows[window_start:anchor]
                patterns = detect_geometric_patterns(window)
                if not patterns:
                    continue
                future = rows[anchor:anchor + lookahead]
                if not future:
                    continue
                entry = rows[anchor - 1].close
                for pattern_index, pattern in enumerate(patterns):
                    global_end = window_start + pattern.end_index
                    if global_end < anchor - 12:
                        continue
                    moves = _directional_moves(pattern, entry, future)
                    terminal_move = moves[-1]
                    max_favourable = max(0.0, max(moves))
                    max_adverse = min(0.0, min(moves))
                    failed = pattern.failed or (
                        pattern.direction == "BULLISH" and terminal_move < 0
                    ) or (
                        pattern.direction == "BEARISH" and terminal_move > 0
                    )
                    confirmed = pattern.confirmed and not failed
                    outcome_kind = "FAILED_PATTERN" if failed else "CONFIRMED_PATTERN" if confirmed else "OBSERVED_PATTERN"
                    source_ids = tuple(item.source_id for item in (rows[anchor - 1], *future) if item.source_id)
                    seed = "|".join(
                        (symbol, timeframe, pattern.name, str(anchor), str(pattern_index), *source_ids)
                    )
                    outcome_id = sha256(seed.encode("utf-8")).hexdigest()[:24]
                    records.append(
                        PatternOutcomeRecord(
                            outcome_id,
                            pattern.name,
                            symbol,
                            timeframe,
                            rows[anchor - 1].end,
                            pattern.direction,
                            entry,
                            terminal_move,
                            max_favourable,
                            max_adverse,
                            outcome_kind,
                            confirmed,
                            failed,
                            source_ids,
                        )
                    )
    return PatternOutcomeLibrary(records)


def _directional_moves(
    pattern: GeometricPattern,
    entry: float,
    future: Sequence[Candle],
) -> tuple[float, ...]:
    if pattern.direction == "BULLISH":
        return tuple(item.close - entry for item in future)
    if pattern.direction == "BEARISH":
        return tuple(entry - item.close for item in future)
    return tuple(item.close - entry for item in future)
