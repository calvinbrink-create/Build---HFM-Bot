"""Machine-readable research vocabulary; entries are hypotheses, not truth."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class KnowledgeEntry:
    name: str
    family: str
    observation: str
    testable: bool = True
    version: int = 1
    epistemic_type: Literal["OBSERVABLE", "INFERENCE", "HYPOTHESIS"] = "HYPOTHESIS"
    evidence_required: tuple[str, ...] = ()


class KnowledgeLibrary:
    def __init__(self, entries: tuple[KnowledgeEntry, ...] = ()):
        self._entries = entries

    def add(self, entry: KnowledgeEntry) -> "KnowledgeLibrary":
        return KnowledgeLibrary(self._entries + (entry,))

    def by_family(self, family: str) -> tuple[KnowledgeEntry, ...]:
        return tuple(entry for entry in self._entries if entry.family == family)

    def all(self) -> tuple[KnowledgeEntry, ...]:
        return self._entries

    @classmethod
    def complete_vocabulary(cls) -> "KnowledgeLibrary":
        from .strategies import chart_patterns, default_knowledge

        mechanics = (
            KnowledgeEntry("price_discovery", "MARKET_MECHANICS", "changes in executable bid and ask over time", True, 1, "OBSERVABLE", ("bid", "ask", "timestamp")),
            KnowledgeEntry("two_sided_auction", "MARKET_MECHANICS", "accepted and rejected traded or quoted price areas", True, 1, "HYPOTHESIS", ("price_path", "activity_proxy")),
            KnowledgeEntry("spread", "MARKET_MECHANICS", "ask minus bid", True, 1, "OBSERVABLE", ("bid", "ask")),
            KnowledgeEntry("gap", "MARKET_MECHANICS", "interval with no observed bar or non-overlapping price range", True, 1, "OBSERVABLE", ("source_timestamps", "ohlc")),
            KnowledgeEntry("volatility", "MARKET_MECHANICS", "distribution of price changes and ranges", True, 1, "OBSERVABLE", ("returns", "ranges")),
            KnowledgeEntry("trend_state", "MARKET_STATE", "slope, fit, persistence and swing sequence", True, 1, "OBSERVABLE", ("completed_candles",)),
            KnowledgeEntry("range_state", "MARKET_STATE", "bounded price acceptance without directional swing progression", True, 1, "HYPOTHESIS", ("completed_candles", "boundaries")),
            KnowledgeEntry("participant_intent", "PARTICIPANTS", "buyer, seller or institutional intent cannot be directly observed from price alone", False, 1, "INFERENCE", ("must_be_labelled_inference",)),
            KnowledgeEntry("liquidity_location", "PARTICIPANTS", "visible prior prices where orders may exist; actual resting size is unknown without depth", True, 1, "INFERENCE", ("prior_levels", "optional_depth")),
            KnowledgeEntry("broker_activity_proxy", "MARKET_MECHANICS", "MT5 tick volume counts quote activity and is not centralized traded volume", True, 1, "OBSERVABLE", ("tick_volume", "broker_source")),
        )
        entries = tuple(
            KnowledgeEntry(item.strategy_id, item.family, ",".join(item.conditions), True, item.version, "HYPOTHESIS", item.required_observations)
            for item in default_knowledge()
        )
        pattern_entries = tuple(
            KnowledgeEntry(item.pattern_id, "PATTERN", ",".join(item.geometry), True, 1, "HYPOTHESIS", ("completed_candles", "future_outcome"))
            for item in chart_patterns()
        )
        return cls(mechanics + entries + pattern_entries)
