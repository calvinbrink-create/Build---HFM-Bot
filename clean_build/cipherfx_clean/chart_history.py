"""Immutable persistence contract for rendered charts and their source bars."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from typing import Mapping, Sequence

from .contracts import Candle, TradeDecision
from .intelligence.model import ChartPack


@dataclass(frozen=True)
class PersistentChart:
    chart_id: str
    symbol: str
    observed_at: datetime
    payload: Mapping[str, object]


def build_persistent_chart(
    chart_pack: ChartPack,
    decision: TradeDecision,
    timeframe: str,
    candles: Sequence[Candle],
    svg: str,
) -> PersistentChart:
    if decision.symbol != chart_pack.symbol:
        raise ValueError("chart decision symbol does not match chart pack")
    if timeframe not in chart_pack.timeframes:
        raise ValueError("chart timeframe does not belong to chart pack")
    if not candles:
        raise ValueError("persistent chart requires rendered candles")
    candle_rows = tuple(_candle_record(candle) for candle in candles)
    if any(row["symbol"] != chart_pack.symbol or row["timeframe"] != timeframe for row in candle_rows):
        raise ValueError("rendered candles do not match chart identity")
    chart_hash = _digest_text(svg)
    candle_hash = _digest(candle_rows)
    chart_id = _digest((chart_pack.pack_id, decision.decision_id, timeframe, chart_hash))
    payload = {
        "state_id": chart_pack.pack_id,
        "decision_id": decision.decision_id,
        "pack_id": chart_pack.pack_id,
        "timeframe": timeframe,
        "chart_hash": chart_hash,
        "candle_hash": candle_hash,
        "candle_count": len(candle_rows),
        "candles": candle_rows,
        "source_ids": tuple(chart_pack.source_ids),
        "annotations": tuple(dict(item) for item in chart_pack.annotations.get(timeframe, ())),
        "svg": svg,
    }
    validate_persistent_chart(payload, symbol=chart_pack.symbol, observed_at=chart_pack.observed_at)
    return PersistentChart(
        chart_id=chart_id,
        symbol=chart_pack.symbol,
        observed_at=datetime.fromisoformat(chart_pack.observed_at),
        payload=payload,
    )


def validate_persistent_chart(
    payload: Mapping[str, object],
    *,
    symbol: str,
    observed_at: str,
) -> None:
    required = {
        "state_id",
        "decision_id",
        "pack_id",
        "timeframe",
        "chart_hash",
        "candle_hash",
        "candle_count",
        "candles",
        "source_ids",
        "annotations",
        "svg",
    }
    missing = sorted(required - set(payload))
    if missing:
        raise ValueError(f"persistent chart missing fields: {','.join(missing)}")
    candles = payload["candles"]
    if not isinstance(candles, (tuple, list)) or not candles:
        raise ValueError("persistent chart has no underlying candles")
    if int(payload["candle_count"]) != len(candles):
        raise ValueError("persistent chart candle count mismatch")
    timeframe = str(payload["timeframe"])
    prior_end: datetime | None = None
    observation = datetime.fromisoformat(observed_at)
    for row in candles:
        if not isinstance(row, Mapping):
            raise ValueError("persistent chart candle is malformed")
        if row.get("symbol") != symbol or row.get("timeframe") != timeframe:
            raise ValueError("persistent chart candle identity mismatch")
        start = datetime.fromisoformat(str(row["start"]))
        end = datetime.fromisoformat(str(row["end"]))
        if start >= end or end > observation or (prior_end is not None and start < prior_end):
            raise ValueError("persistent chart candles are not completed and ordered")
        prior_end = end
    if str(payload["chart_hash"]) != _digest_text(str(payload["svg"])):
        raise ValueError("persistent chart SVG hash mismatch")
    if str(payload["candle_hash"]) != _digest(candles):
        raise ValueError("persistent chart candle hash mismatch")


def _candle_record(candle: Candle) -> Mapping[str, object]:
    return {
        "symbol": candle.symbol,
        "timeframe": candle.timeframe,
        "start": candle.start.isoformat(),
        "end": candle.end.isoformat(),
        "open": candle.open,
        "high": candle.high,
        "low": candle.low,
        "close": candle.close,
        "tick_count": candle.tick_count,
        "tick_volume": candle.tick_volume,
        "real_volume": candle.real_volume,
        "spread_points": candle.spread_points,
        "source": candle.source,
        "source_id": candle.source_id,
    }


def _digest(value: object) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _digest_text(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()
