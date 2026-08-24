"""Immutable requested-versus-filled broker evidence.

Slippage is measured only from the exact price sent in the broker request and
the exact price returned for that broker deal.  Proposal, candle, quote, and
chart prices are deliberately not accepted as substitutes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from math import isclose, isfinite
from typing import Literal, Mapping


Side = Literal["BUY", "SELL"]


@dataclass(frozen=True)
class BrokerFill:
    """Exact execution evidence returned by the MT5 execution adapter."""

    order_ticket: str
    deal_ticket: str
    requested_price: float
    fill_price: float
    volume: float
    filled_at: datetime
    source_id: str
    requested_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.order_ticket or not self.deal_ticket or not self.source_id:
            raise ValueError("broker order, deal, and source identities are required")
        if not all(isfinite(value) for value in (self.requested_price, self.fill_price, self.volume)):
            raise ValueError("broker fill values must be finite")
        if self.requested_price <= 0 or self.fill_price <= 0 or self.volume <= 0:
            raise ValueError("broker fill prices and volume must be positive")
        if self.filled_at.tzinfo is None or self.filled_at.utcoffset() is None:
            raise ValueError("broker fill time must be timezone-aware")
        if self.requested_at is not None:
            if self.requested_at.tzinfo is None or self.requested_at.utcoffset() is None:
                raise ValueError("broker request time must be timezone-aware")
            if self.filled_at < self.requested_at:
                raise ValueError("broker fill time cannot precede request time")

    def payload(self) -> Mapping[str, object]:
        return {
            "order_ticket": self.order_ticket,
            "deal_ticket": self.deal_ticket,
            "requested_price": self.requested_price,
            "fill_price": self.fill_price,
            "volume": self.volume,
            "filled_at": self.filled_at.isoformat(),
            "source_id": self.source_id,
            "requested_at": self.requested_at.isoformat() if self.requested_at else None,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "BrokerFill":
        requested_at = payload.get("requested_at")
        return cls(
            order_ticket=str(payload["order_ticket"]),
            deal_ticket=str(payload["deal_ticket"]),
            requested_price=float(payload["requested_price"]),
            fill_price=float(payload["fill_price"]),
            volume=float(payload["volume"]),
            filled_at=datetime.fromisoformat(str(payload["filled_at"])),
            source_id=str(payload["source_id"]),
            requested_at=(
                datetime.fromisoformat(str(requested_at))
                if requested_at not in (None, "")
                else None
            ),
        )


@dataclass(frozen=True)
class SlippageObservation:
    """One immutable measurement for one MT5 deal fill."""

    observation_id: str
    decision_id: str
    request_hash: str
    edge_id: str
    symbol: str
    side: Side
    order_ticket: str
    deal_ticket: str
    requested_price: float
    fill_price: float
    volume: float
    filled_at: datetime
    signed_price_difference: float
    side_adjusted_price_difference: float
    adverse_slippage: float
    favorable_slippage: float
    side_adjusted_slippage_bps: float
    source_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        identities = (
            self.observation_id,
            self.decision_id,
            self.request_hash,
            self.edge_id,
            self.symbol,
            self.order_ticket,
            self.deal_ticket,
        )
        if not all(identities) or not self.source_ids:
            raise ValueError("complete slippage identity and lineage are required")
        if self.side not in ("BUY", "SELL"):
            raise ValueError("slippage side must be BUY or SELL")
        numeric = (
            self.requested_price,
            self.fill_price,
            self.volume,
            self.signed_price_difference,
            self.side_adjusted_price_difference,
            self.adverse_slippage,
            self.favorable_slippage,
            self.side_adjusted_slippage_bps,
        )
        if not all(isfinite(value) for value in numeric):
            raise ValueError("slippage values must be finite")
        if self.requested_price <= 0 or self.fill_price <= 0 or self.volume <= 0:
            raise ValueError("slippage prices and volume must be positive")
        if self.filled_at.tzinfo is None or self.filled_at.utcoffset() is None:
            raise ValueError("slippage fill time must be timezone-aware")
        signed = self.fill_price - self.requested_price
        adjusted = signed if self.side == "BUY" else -signed
        expected = (
            signed,
            adjusted,
            max(0.0, adjusted),
            max(0.0, -adjusted),
            adjusted / self.requested_price * 10_000.0,
        )
        observed = (
            self.signed_price_difference,
            self.side_adjusted_price_difference,
            self.adverse_slippage,
            self.favorable_slippage,
            self.side_adjusted_slippage_bps,
        )
        if not all(isclose(left, right, rel_tol=0.0, abs_tol=1e-12) for left, right in zip(observed, expected)):
            raise ValueError("slippage measurements do not reconcile with requested and fill prices")

    def payload(self) -> Mapping[str, object]:
        return {
            "observation_id": self.observation_id,
            "decision_id": self.decision_id,
            "request_hash": self.request_hash,
            "edge_id": self.edge_id,
            "symbol": self.symbol,
            "side": self.side,
            "order_ticket": self.order_ticket,
            "deal_ticket": self.deal_ticket,
            "requested_price": self.requested_price,
            "fill_price": self.fill_price,
            "volume": self.volume,
            "filled_at": self.filled_at.isoformat(),
            "signed_price_difference": self.signed_price_difference,
            "side_adjusted_price_difference": self.side_adjusted_price_difference,
            "adverse_slippage": self.adverse_slippage,
            "favorable_slippage": self.favorable_slippage,
            "side_adjusted_slippage_bps": self.side_adjusted_slippage_bps,
            "source_ids": self.source_ids,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "SlippageObservation":
        return cls(
            observation_id=str(payload["observation_id"]),
            decision_id=str(payload["decision_id"]),
            request_hash=str(payload["request_hash"]),
            edge_id=str(payload["edge_id"]),
            symbol=str(payload["symbol"]),
            side=str(payload["side"]),
            order_ticket=str(payload["order_ticket"]),
            deal_ticket=str(payload["deal_ticket"]),
            requested_price=float(payload["requested_price"]),
            fill_price=float(payload["fill_price"]),
            volume=float(payload["volume"]),
            filled_at=datetime.fromisoformat(str(payload["filled_at"])),
            signed_price_difference=float(payload["signed_price_difference"]),
            side_adjusted_price_difference=float(payload["side_adjusted_price_difference"]),
            adverse_slippage=float(payload["adverse_slippage"]),
            favorable_slippage=float(payload["favorable_slippage"]),
            side_adjusted_slippage_bps=float(payload["side_adjusted_slippage_bps"]),
            source_ids=tuple(str(value) for value in payload["source_ids"]),
        )


def build_slippage_observation(
    *,
    decision_id: str,
    request_hash: str,
    edge_id: str,
    symbol: str,
    side: Side,
    fill: BrokerFill,
) -> SlippageObservation:
    signed = fill.fill_price - fill.requested_price
    adjusted = signed if side == "BUY" else -signed
    identity = {
        "decision_id": decision_id,
        "request_hash": request_hash,
        "symbol": symbol,
        "side": side,
        "order_ticket": fill.order_ticket,
        "deal_ticket": fill.deal_ticket,
        "requested_price": fill.requested_price,
        "fill_price": fill.fill_price,
        "volume": fill.volume,
        "filled_at": fill.filled_at.isoformat(),
        "source_id": fill.source_id,
    }
    observation_id = sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()
    return SlippageObservation(
        observation_id=observation_id,
        decision_id=decision_id,
        request_hash=request_hash,
        edge_id=edge_id,
        symbol=symbol,
        side=side,
        order_ticket=fill.order_ticket,
        deal_ticket=fill.deal_ticket,
        requested_price=fill.requested_price,
        fill_price=fill.fill_price,
        volume=fill.volume,
        filled_at=fill.filled_at,
        signed_price_difference=signed,
        side_adjusted_price_difference=adjusted,
        adverse_slippage=max(0.0, adjusted),
        favorable_slippage=max(0.0, -adjusted),
        side_adjusted_slippage_bps=adjusted / fill.requested_price * 10_000.0,
        source_ids=(
            f"instruction:{request_hash}",
            f"mt5-order:{fill.order_ticket}",
            f"mt5-deal:{fill.deal_ticket}",
            fill.source_id,
        ),
    )
