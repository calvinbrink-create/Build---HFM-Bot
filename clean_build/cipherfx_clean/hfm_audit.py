"""Generate deterministic HFM export evidence without modifying MT5."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from .hfm_data import HfmCsvMarketDataAdapter, NATIVE_TIMEFRAMES
from .calendar import ObservedTradingCalendar, observed_session_profile
from .data_quality import DataQualityPolicy, validate_dataset


QUALITY_POLICY = DataQualityPolicy(
    required_timeframes=NATIVE_TIMEFRAMES,
    minimum_bars={frame: (50 if frame == "MN1" else 100) for frame in NATIVE_TIMEFRAMES},
    maximum_age={
        "M1": timedelta(minutes=5),
        "M3": timedelta(minutes=10),
        "M5": timedelta(minutes=15),
        "M15": timedelta(minutes=30),
        "M30": timedelta(hours=1),
        "H1": timedelta(hours=2),
        "H4": timedelta(hours=8),
        "D1": timedelta(days=3),
        "W1": timedelta(days=14),
        "MN1": timedelta(days=62),
    },
    maximum_tick_age=timedelta(minutes=1),
)


def build_report(root: Path, symbols: tuple[str, ...], observed_at: datetime) -> dict[str, object]:
    adapter = HfmCsvMarketDataAdapter(root)
    inventory = {item.symbol: asdict(item) for item in adapter.symbols()}
    results: dict[str, object] = {}
    for symbol in symbols:
        dataset = adapter.dataset(symbol, observed_at=observed_at, include_latest_tick=True)
        session_profile = observed_session_profile(symbol, dataset.bars.get("M1", ()), timezone_name="UTC")
        quality = validate_dataset(
            dataset,
            observed_at=observed_at,
            policy=QUALITY_POLICY,
            calendar=ObservedTradingCalendar(session_profile),
        )
        results[symbol] = {
            "dataset_id": dataset.dataset_id,
            "valid": dataset.valid,
            "coverage_start_utc": dataset.start.isoformat(),
            "coverage_end_utc": dataset.end.isoformat(),
            "frames": {
                frame: {
                    "bars": len(rows),
                    "first_utc": rows[0].start.isoformat() if rows else None,
                    "last_completed_utc": rows[-1].end.isoformat() if rows else None,
                    "source_hash": dataset.source_hashes.get(frame),
                }
                for frame, rows in dataset.bars.items()
            },
            "issues": [asdict(issue) for issue in dataset.issues],
            "quality": asdict(quality),
            "observed_session_profile": asdict(session_profile),
            "latest_tick_utc": dataset.ticks[-1].timestamp.isoformat() if dataset.ticks else None,
            "latest_tick_age_seconds": (observed_at - dataset.ticks[-1].timestamp).total_seconds() if dataset.ticks else None,
            "broker_symbol": inventory.get(symbol),
        }
    return {
        "schema_version": 1,
        "source": "HFM_MT5_CSV_READ_ONLY",
        "observed_at_utc": observed_at.astimezone(timezone.utc).isoformat(),
        "required_timeframes": NATIVE_TIMEFRAMES,
        "symbols": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    observed_at = datetime.now(timezone.utc)
    report = build_report(args.root, tuple(args.symbols), observed_at)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
