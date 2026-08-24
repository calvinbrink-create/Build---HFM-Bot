"""Run a deterministic research batch over the persistent clean memory."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path

from .research_pipeline import ResearchPolicy, default_condition_sets, run_research, sweep_specs
from .store import EvidenceStore


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--timeframes", nargs="+", required=True)
    parser.add_argument("--sessions", nargs="+", default=("ANY",))
    parser.add_argument("--regimes", nargs="+", default=("ANY",))
    parser.add_argument("--horizons", nargs="+", type=int, required=True)
    parser.add_argument("--cost-return", type=float, required=True)
    parser.add_argument("--cost-evidence-status", default="INCOMPLETE_UNMEASURED")
    parser.add_argument("--cost-source-id", action="append", default=[])
    parser.add_argument("--train-size", type=int, default=200)
    parser.add_argument("--test-size", type=int, default=50)
    parser.add_argument("--holdout-size", type=int, default=50)
    parser.add_argument("--embargo-seconds", type=int, default=900)
    parser.add_argument("--minimum-oos", type=int, default=100)
    parser.add_argument("--minimum-holdout", type=int, default=50)
    args = parser.parse_args()

    store = EvidenceStore(args.database)
    try:
        cases = tuple(
            case
            for symbol in args.symbols
            for case in store.load_historical_cases(symbol)
        )
        specs = tuple(
            spec
            for timeframe in args.timeframes
            for spec in sweep_specs(
                families=("RAW_MARKET_STATE",),
                symbols=tuple(args.symbols),
                directions=("BUY", "SELL"),
                timeframes=(timeframe,),
                sessions=tuple(args.sessions),
                regimes=tuple(args.regimes),
                horizons=tuple(args.horizons),
                condition_sets=default_condition_sets(timeframe),
                cost_return=args.cost_return,
                cost_evidence_status=args.cost_evidence_status,
                cost_source_ids=tuple(args.cost_source_id),
            )
        )
        policy = ResearchPolicy(
            args.train_size,
            args.test_size,
            args.holdout_size,
            args.embargo_seconds,
            args.minimum_oos,
            args.minimum_holdout,
        )
        report = run_research(store=store, specs=specs, cases=cases, policy=policy)
        result = {
            "report": asdict(report),
            "policy": asdict(policy),
            "symbols": args.symbols,
            "timeframes": args.timeframes,
            "sessions": args.sessions,
            "regimes": args.regimes,
            "horizons": args.horizons,
            "case_count": len(cases),
            "cost_return": args.cost_return,
            "cost_evidence_status": args.cost_evidence_status,
            "cost_source_ids": args.cost_source_id,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        store.checkpoint()
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
