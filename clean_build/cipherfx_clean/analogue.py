"""Historical-state analogue search with explicit, inspectable distance."""

from __future__ import annotations

from dataclasses import dataclass

from .features import MarketFingerprint


@dataclass(frozen=True)
class Analogue:
    fingerprint: MarketFingerprint
    distance: float
    outcome_ids: tuple[str, ...] = ()


class AnalogueLibrary:
    def __init__(self):
        self._items: list[Analogue] = []

    def add(self, fingerprint: MarketFingerprint, outcome_ids: tuple[str, ...] = ()) -> None:
        self._items.append(Analogue(fingerprint, 0.0, outcome_ids))

    def search(self, current: MarketFingerprint, limit: int = 20) -> tuple[Analogue, ...]:
        ranked = [
            Analogue(item.fingerprint, _distance(current, item.fingerprint), item.outcome_ids)
            for item in self._items
        ]
        return tuple(sorted(ranked, key=lambda item: item.distance)[:limit])


def _distance(left: MarketFingerprint, right: MarketFingerprint) -> float:
    values = (
        (left.spread_mean, right.spread_mean),
        (left.spread_max, right.spread_max),
        (left.return_change, right.return_change),
        (left.range_change, right.range_change),
        (left.directional_persistence, right.directional_persistence),
        (left.tick_rate_per_second, right.tick_rate_per_second),
        (float(left.candle_direction), float(right.candle_direction)),
        (left.candle_range, right.candle_range),
    )
    total = 0.0
    for current, historical in values:
        scale = max(abs(current), abs(historical), 1e-9)
        total += ((current - historical) / scale) ** 2
    return total ** 0.5
