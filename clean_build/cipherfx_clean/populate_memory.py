"""Populate persistent clean memory from read-only HFM terminal exports."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path

from .calendar import ObservedTradingCalendar, observed_session_profile
from .hfm_data import HfmCsvMarketDataAdapter
from .historical_memory import (
    FEATURE_SCHEMA_VERSION,
    feature_names,
    feature_vector,
    path_outcome,
    synchronized_snapshots,
)
from .intelligence.cross_asset import CrossAssetContextIndex
from .snapshot import RESEARCH_TIMEFRAMES
from .store import EvidenceStore


MINIMUM_BARS = {
    "MN1": 24,
    "W1": 52,
    "D1": 100,
    "H4": 100,
    "H1": 100,
    "M30": 100,
    "M15": 100,
    "M5": 100,
    "M3": 100,
    "M1": 100,
}


def populate(
    *,
    export_root: Path,
    database: Path,
    symbols: tuple[str, ...],
    observed_at: datetime,
    stride: int,
    lookback: int,
) -> dict[str, object]:
    adapter = HfmCsvMarketDataAdapter(export_root)
    store = EvidenceStore(database)
    report = {}
    try:
        datasets = {
            symbol: adapter.dataset(symbol, observed_at=observed_at, include_latest_tick=False)
            for symbol in symbols
        }
        correlation_frames = {
            symbol: dataset.bars.get("M5", ())
            for symbol, dataset in datasets.items()
        }
        correlation_index = CrossAssetContextIndex(correlation_frames)
        context_cache: dict[datetime, dict[str, dict[str, float]]] = {}
        for symbol in symbols:
            dataset = datasets[symbol]
            store.write_dataset(dataset)
            calendars = {
                frame: ObservedTradingCalendar(observed_session_profile(symbol, dataset.bars[frame], timezone_name="UTC"))
                for frame in RESEARCH_TIMEFRAMES
            }
            anchor = dataset.bars["M1"]
            anchor_ends = tuple(row.end for row in anchor)
            candidate_count = max(0, (len(anchor) - lookback + stride - 1) // stride)
            cases = []
            for snapshot in synchronized_snapshots(
                dataset,
                timeframes=RESEARCH_TIMEFRAMES,
                anchor_timeframe="M1",
                lookback=lookback,
                warmup=lookback,
                stride=stride,
                window_calendars=calendars,
                exclude_contaminated=True,
                minimum_bars=MINIMUM_BARS,
            ):
                if snapshot.observed_at not in context_cache:
                    context_cache[snapshot.observed_at] = dict(
                        correlation_index.at(snapshot.observed_at)
                    )
                state = feature_vector(
                    snapshot,
                    RESEARCH_TIMEFRAMES,
                    cross_asset_context=context_cache[snapshot.observed_at][symbol],
                )
                path = path_outcome(state, anchor, anchor_ends=anchor_ends)
                if path.outcomes:
                    cases.append((state, path))
            changes = store.write_historical_cases(cases)
            report[symbol] = {
                "dataset_id": dataset.dataset_id,
                "candidate_windows": candidate_count,
                "clean_states": len(cases),
                "excluded_windows": candidate_count - len(cases),
                "database_changes": changes,
                "path_fidelity": "BAR_OHLC_NO_EXECUTABLE_TICKS",
                "subminute_outcomes": "UNAVAILABLE_NO_HISTORICAL_HFM_TICK_EXPORT",
            }
        result = {
            "schema_version": 1,
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "observed_at_utc": observed_at.astimezone(timezone.utc).isoformat(),
            "timeframes": RESEARCH_TIMEFRAMES,
            "feature_names": feature_names(RESEARCH_TIMEFRAMES),
            "stride_m1_bars": stride,
            "lookback_bars": lookback,
            "symbols": report,
            "database_integrity": store.integrity(),
        }
        store.checkpoint()
        return result
    finally:
        store.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--stride", type=int, default=60)
    parser.add_argument("--lookback", type=int, default=100)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = populate(
        export_root=args.root,
        database=args.database,
        symbols=tuple(args.symbols),
        observed_at=datetime.now(timezone.utc),
        stride=args.stride,
        lookback=args.lookback,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
