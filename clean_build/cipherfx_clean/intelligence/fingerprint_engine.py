"""Standardized numeric and semantic fingerprints for market states."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Mapping, Sequence

from ..contracts import MarketSnapshot
from ..historical_memory import HistoricalState, feature_names, feature_vector


@dataclass(frozen=True)
class SetupFingerprint:
    state: HistoricalState
    feature_names: tuple[str, ...]
    semantic_labels: tuple[str, ...]
    source_digest: str

    @property
    def values(self) -> tuple[float, ...]:
        return self.state.vector

    def as_mapping(self) -> Mapping[str, float]:
        return dict(zip(self.feature_names, self.values))


class SetupFingerprintEngine:
    """Builds reproducible descriptors without making a trade decision."""

    def build(
        self,
        snapshot: MarketSnapshot,
        timeframes: Sequence[str],
        *,
        cross_asset_context: Mapping[str, float] | None = None,
    ) -> SetupFingerprint:
        state = feature_vector(
            snapshot,
            timeframes,
            cross_asset_context=cross_asset_context,
        )
        names = feature_names(timeframes)
        if len(names) != len(state.vector):
            raise ValueError("fingerprint feature schema and vector dimensions differ")
        source_digest = sha256(
            "|".join(state.source_ids).encode("utf-8")
        ).hexdigest()
        return SetupFingerprint(state, names, state.labels, source_digest)
