"""Measured broker-cost evidence used by research and post-trade review.

The module never invents transaction costs. A profile is complete only when
enough immutable broker samples contain quote, fill, fee, and timing evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from math import isfinite
from statistics import median
from typing import Iterable, Literal


Side = Literal["BUY", "SELL"]


@dataclass(frozen=True)
class ExecutionCostSample:
    sample_id: str
    decision_id: str
    symbol: str
    side: Side
    observed_at: datetime
    reference_bid: float
    reference_ask: float
    requested_price: float
    fill_price: float
    volume: float
    contract_size: float
    commission_account: float
    swap_account: float
    submitted_at: datetime
    filled_at: datetime
    source_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        numeric = (
            self.reference_bid, self.reference_ask, self.requested_price,
            self.fill_price, self.volume, self.contract_size,
            self.commission_account, self.swap_account,
        )
        if not self.sample_id or not self.decision_id or not self.symbol or not self.source_ids:
            raise ValueError("cost sample identity and source evidence are required")
        if not all(isfinite(value) for value in numeric):
            raise ValueError("cost sample values must be finite")
        if self.reference_bid <= 0 or self.reference_ask < self.reference_bid:
            raise ValueError("valid reference bid/ask are required")
        if self.requested_price <= 0 or self.fill_price <= 0:
            raise ValueError("requested and fill prices must be positive")
        if self.volume <= 0 or self.contract_size <= 0:
            raise ValueError("volume and contract size must be positive")
        if self.filled_at < self.submitted_at:
            raise ValueError("fill time cannot precede submission")

    @property
    def reference_mid(self) -> float:
        return (self.reference_bid + self.reference_ask) / 2.0

    @property
    def spread_return(self) -> float:
        return (self.reference_ask - self.reference_bid) / self.reference_mid

    @property
    def adverse_slippage_return(self) -> float:
        signed = self.fill_price - self.requested_price
        adverse = signed if self.side == "BUY" else -signed
        return max(0.0, adverse) / self.requested_price

    @property
    def fee_return(self) -> float:
        notional = self.fill_price * self.volume * self.contract_size
        return (abs(self.commission_account) + abs(self.swap_account)) / notional

    @property
    def latency_seconds(self) -> float:
        return (self.filled_at - self.submitted_at).total_seconds()

    @property
    def observed_cost_return(self) -> float:
        return self.spread_return + self.adverse_slippage_return + self.fee_return


@dataclass(frozen=True)
class CostProfile:
    profile_id: str
    symbol: str
    sample_count: int
    median_spread_return: float
    median_slippage_return: float
    median_fee_return: float
    median_latency_seconds: float
    conservative_cost_return: float
    status: Literal["COMPLETE", "INSUFFICIENT_SAMPLES"]
    sample_ids: tuple[str, ...]


def build_cost_profile(
    symbol: str,
    samples: Iterable[ExecutionCostSample],
    *,
    minimum_samples: int = 30,
    conservative_percentile: float = 0.90,
) -> CostProfile:
    if minimum_samples <= 0 or not 0 < conservative_percentile <= 1:
        raise ValueError("valid sample threshold and percentile are required")
    rows = tuple(sorted((row for row in samples if row.symbol == symbol), key=lambda row: (row.observed_at, row.sample_id)))
    costs = sorted(row.observed_cost_return for row in rows)
    conservative = _percentile(costs, conservative_percentile) if costs else 0.0
    status = "COMPLETE" if len(rows) >= minimum_samples else "INSUFFICIENT_SAMPLES"
    payload = (
        symbol, tuple(row.sample_id for row in rows), minimum_samples,
        conservative_percentile, conservative,
    )
    profile_id = sha256(json.dumps(payload, separators=(",", ":"), default=str).encode()).hexdigest()
    return CostProfile(
        profile_id,
        symbol,
        len(rows),
        median(tuple(row.spread_return for row in rows)) if rows else 0.0,
        median(tuple(row.adverse_slippage_return for row in rows)) if rows else 0.0,
        median(tuple(row.fee_return for row in rows)) if rows else 0.0,
        median(tuple(row.latency_seconds for row in rows)) if rows else 0.0,
        conservative,
        status,
        tuple(row.sample_id for row in rows),
    )


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    index = min(len(values) - 1, max(0, int((len(values) - 1) * percentile)))
    return values[index]
