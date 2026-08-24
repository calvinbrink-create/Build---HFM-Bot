"""Read-only reconciliation of HFM deal exports into broker trade lifecycles."""

from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class HfmDeal:
    deal_id: int
    position_id: int
    order_id: int
    symbol: str
    deal_type: str
    entry_type: str
    reason: str
    magic: int
    comment: str
    volume: float
    price: float
    profit: float
    swap: float
    commission: float
    net_profit: float
    broker_time: datetime
    utc_time: datetime
    broker_utc_offset_seconds: int
    source_id: str


@dataclass(frozen=True)
class TradeLifecycle:
    lifecycle_id: str
    position_id: int
    symbol: str
    side: Literal["BUY", "SELL", "UNKNOWN"]
    status: Literal["OPEN", "CLOSED", "ORPHAN_CLOSE", "INVALID"]
    opened_at: datetime | None
    closed_at: datetime | None
    open_volume: float
    close_volume: float
    entry_price: float | None
    exit_price: float | None
    gross_profit: float
    commission: float
    swap: float
    net_profit: float
    holding_seconds: float | None
    deal_ids: tuple[int, ...]
    source_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class HistoryReconciliation:
    report_id: str
    source_path: str
    source_digest: str
    deal_count: int
    lifecycle_count: int
    closed_count: int
    open_count: int
    orphan_close_count: int
    invalid_count: int
    duplicate_deal_ids: tuple[int, ...]
    lifecycle_ids: tuple[str, ...]


def import_hfm_history(path: str | Path) -> tuple[tuple[HfmDeal, ...], tuple[int, ...]]:
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(source)
    deals = []
    seen: set[int] = set()
    duplicates: set[int] = set()
    with source.open(newline="", encoding="utf-8-sig") as handle:
        for raw in csv.DictReader(handle):
            deal_id = int(raw["deal"])
            if deal_id in seen:
                duplicates.add(deal_id)
                continue
            seen.add(deal_id)
            broker_epoch = int(raw["time_broker"])
            utc_epoch = int(raw["time_utc"])
            offset = int(raw["broker_utc_offset_seconds"])
            if broker_epoch - utc_epoch != offset:
                raise ValueError(f"broker/UTC offset mismatch for deal {deal_id}")
            source_id = _digest((source.name, tuple(sorted(raw.items()))))
            deals.append(HfmDeal(
                deal_id,
                int(raw.get("position_id") or 0),
                int(raw.get("order") or 0),
                raw.get("symbol", ""),
                raw.get("deal_type", ""),
                raw.get("deal_entry", ""),
                raw.get("deal_reason", ""),
                int(raw.get("magic") or 0),
                raw.get("comment", ""),
                float(raw.get("volume") or 0.0),
                float(raw.get("price") or 0.0),
                float(raw.get("profit") or 0.0),
                float(raw.get("swap") or 0.0),
                float(raw.get("commission") or 0.0),
                float(raw.get("net_profit") or 0.0),
                datetime.fromtimestamp(broker_epoch, timezone.utc),
                datetime.fromtimestamp(utc_epoch, timezone.utc),
                offset,
                source_id,
            ))
    return tuple(sorted(deals, key=lambda row: (row.utc_time, row.deal_id))), tuple(sorted(duplicates))


def reconcile_hfm_history(path: str | Path) -> tuple[HistoryReconciliation, tuple[HfmDeal, ...], tuple[TradeLifecycle, ...]]:
    source = Path(path)
    deals, duplicates = import_hfm_history(source)
    grouped: dict[int, list[HfmDeal]] = {}
    for deal in deals:
        if deal.position_id <= 0 or not deal.symbol:
            continue
        grouped.setdefault(deal.position_id, []).append(deal)
    lifecycles = tuple(_lifecycle(position_id, tuple(rows)) for position_id, rows in sorted(grouped.items()))
    digest = sha256(source.read_bytes()).hexdigest()
    report_payload = (
        str(source), digest, tuple(row.source_id for row in deals),
        tuple(row.lifecycle_id for row in lifecycles), duplicates,
    )
    report = HistoryReconciliation(
        _digest(report_payload),
        str(source),
        digest,
        len(deals),
        len(lifecycles),
        sum(row.status == "CLOSED" for row in lifecycles),
        sum(row.status == "OPEN" for row in lifecycles),
        sum(row.status == "ORPHAN_CLOSE" for row in lifecycles),
        sum(row.status == "INVALID" for row in lifecycles),
        duplicates,
        tuple(row.lifecycle_id for row in lifecycles),
    )
    return report, deals, lifecycles


def _lifecycle(position_id: int, deals: tuple[HfmDeal, ...]) -> TradeLifecycle:
    rows = tuple(sorted(deals, key=lambda row: (row.utc_time, row.deal_id)))
    opens = tuple(row for row in rows if row.entry_type in ("DEAL_ENTRY_IN", "DEAL_ENTRY_INOUT"))
    closes = tuple(row for row in rows if row.entry_type in ("DEAL_ENTRY_OUT", "DEAL_ENTRY_OUT_BY", "DEAL_ENTRY_INOUT"))
    reasons = []
    if not opens and closes:
        status = "ORPHAN_CLOSE"
        reasons.append("MISSING_OPEN_DEAL")
    elif not opens:
        status = "INVALID"
        reasons.append("NO_OPEN_OR_CLOSE_DEALS")
    else:
        open_volume = sum(row.volume for row in opens)
        close_volume = sum(row.volume for row in closes)
        status = "CLOSED" if close_volume + 1e-9 >= open_volume else "OPEN"
        if close_volume > open_volume + 1e-9:
            status = "INVALID"
            reasons.append("CLOSE_VOLUME_EXCEEDS_OPEN_VOLUME")
    open_volume = sum(row.volume for row in opens)
    close_volume = sum(row.volume for row in closes)
    side_types = {row.deal_type for row in opens}
    side: Literal["BUY", "SELL", "UNKNOWN"] = (
        "BUY" if side_types == {"DEAL_TYPE_BUY"}
        else "SELL" if side_types == {"DEAL_TYPE_SELL"}
        else "UNKNOWN"
    )
    if opens and side == "UNKNOWN":
        reasons.append("AMBIGUOUS_OPEN_DIRECTION")
        status = "INVALID"
    opened_at = opens[0].utc_time if opens else None
    closed_at = closes[-1].utc_time if closes and status in ("CLOSED", "ORPHAN_CLOSE", "INVALID") else None
    entry = _weighted_price(opens)
    exit_price = _weighted_price(closes)
    symbol = next((row.symbol for row in rows if row.symbol), "")
    lifecycle_id = _digest((position_id, tuple(row.source_id for row in rows)))
    return TradeLifecycle(
        lifecycle_id,
        position_id,
        symbol,
        side,
        status,
        opened_at,
        closed_at,
        open_volume,
        close_volume,
        entry,
        exit_price,
        sum(row.profit for row in rows),
        sum(row.commission for row in rows),
        sum(row.swap for row in rows),
        sum(row.net_profit for row in rows),
        (closed_at - opened_at).total_seconds() if opened_at and closed_at else None,
        tuple(row.deal_id for row in rows),
        tuple(row.source_id for row in rows),
        tuple(sorted(set(reasons))),
    )


def _weighted_price(rows: tuple[HfmDeal, ...]) -> float | None:
    volume = sum(row.volume for row in rows)
    return sum(row.price * row.volume for row in rows) / volume if volume else None


def _digest(value: object) -> str:
    return sha256(json.dumps(value, default=str, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--deals", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report, deals, lifecycles = reconcile_hfm_history(args.deals)
    from .store import EvidenceStore

    store = EvidenceStore(args.database)
    try:
        store.write_hfm_history(report, deals, lifecycles)
        store.checkpoint()
    finally:
        store.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({
        "report": asdict(report),
        "status_counts": {
            status: sum(row.status == status for row in lifecycles)
            for status in ("OPEN", "CLOSED", "ORPHAN_CLOSE", "INVALID")
        },
        "symbols": {
            symbol: {
                "lifecycles": sum(row.symbol == symbol for row in lifecycles),
                "closed": sum(row.symbol == symbol and row.status == "CLOSED" for row in lifecycles),
                "net_profit": sum(row.net_profit for row in lifecycles if row.symbol == symbol),
            }
            for symbol in sorted(set(row.symbol for row in lifecycles))
        },
    }, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    return 0 if not report.duplicate_deal_ids and report.invalid_count == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
