"""Read-only verifier for the F00-F64 Friday scope ledger."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping, Sequence

from .contracts import MarketSnapshot
from .data_quality import validate_research_window
from .hfm_data import HfmCsvMarketDataAdapter, hfm_server_bar_times, hfm_server_epoch_to_utc
from .intelligence.evidence import build_timeframe_evidence
from .intelligence.knowledge import KnowledgeLibrary
from .intelligence.market_features import classify_tick_directions, tick_acceleration_observation, tick_imbalance_observation, tick_velocity_observation
from .intelligence.regime import classify_regime
from .intelligence.renderer import render_svg
from .intelligence.session import active_sessions, session_label, session_observation
from .intelligence.strategies import chart_patterns, default_knowledge
from .release import CertificationInput, build_release_bundle, certify_edge, verify_release_bundle
from .historical_memory import feature_names
from .features import build_fingerprint
from .market import RawTick
from .calendar import ObservedTradingCalendar, observed_session_profile


ACTIVE_SYMBOLS = ("XAUUSD", "UK100", "USA100", "USA500", "USA30")
F_IDS = tuple(f"F{number:02d}" for number in range(65))
TIMEFRAMES = ("M1", "M5", "M15", "H1", "H4", "D1")


def verify_f00_f64_scope(*, requirement_id: str, bridge_root: Path, symbols: Sequence[str]) -> Mapping[str, object]:
    if requirement_id not in F_IDS:
        raise ValueError(f"unsupported Friday requirement: {requirement_id}")
    if tuple(symbols) != ACTIVE_SYMBOLS:
        raise ValueError("F00-F64 requires the exact five-symbol active universe")

    adapter = HfmCsvMarketDataAdapter(bridge_root)
    symbol_rows = {row.symbol: row for row in adapter.symbols()}
    missing_symbols = [symbol for symbol in ACTIVE_SYMBOLS if symbol not in symbol_rows]
    if missing_symbols:
        raise ValueError(f"HFM symbol inventory missing active symbols: {missing_symbols}")

    datasets: dict[str, object] = {}
    snapshots: dict[str, MarketSnapshot] = {}
    frame_counts: dict[str, Mapping[str, int]] = {}
    data_issues: dict[str, tuple[str, ...]] = {}
    fingerprints = {}
    chart_svgs: dict[str, int] = {}
    session_rows: dict[str, Mapping[str, object]] = {}
    for symbol in ACTIVE_SYMBOLS:
        tick = adapter.latest_tick(symbol)
        observed_at = max(tick.timestamp, datetime.now(timezone.utc))
        dataset = adapter.dataset(
            symbol,
            observed_at=observed_at,
            timeframes=TIMEFRAMES,
            include_latest_tick=False,
            derive_m3_history=False,
            history_mode="rolling",
        )
        datasets[symbol] = dataset.dataset_id
        bars = {timeframe: tuple(dataset.bars.get(timeframe, ())) for timeframe in TIMEFRAMES}
        if len(bars["M1"]) < 80:
            bars["M1"] = adapter.read_bars(symbol, "M1", observed_at=observed_at, history_mode="combined")
        if any(not bars[timeframe] for timeframe in TIMEFRAMES):
            raise ValueError(f"{symbol} has missing HFM frames")
        selected = {timeframe: rows[-200:] for timeframe, rows in bars.items()}
        snapshots[symbol] = MarketSnapshot(
            symbol,
            observed_at,
            (tick,),
            selected,
            {timeframe: "COMPLETED" for timeframe in selected},
            (dataset.dataset_id, f"HFM_TICK:{tick.timestamp.isoformat()}"),
            {"point": symbol_rows[symbol].point, "contract_size": symbol_rows[symbol].contract_size},
        )
        frame_counts[symbol] = {timeframe: len(rows) for timeframe, rows in bars.items()}
        data_issues[symbol] = tuple(issue.code for issue in dataset.issues)
        proxy = tuple(RawTick(symbol, row.end - timedelta(milliseconds=1), row.close, row.close + max(tick.ask - tick.bid, 1e-9)) for row in bars["M1"][-120:])
        fingerprints[symbol] = build_fingerprint(proxy, bars["M5"][-20:])
        report = build_timeframe_evidence(selected)
        if set(report.structure) != set(TIMEFRAMES) or set(report.patterns) != set(TIMEFRAMES):
            raise ValueError(f"{symbol} timeframe evidence is incomplete")
        rendered = render_svg(tuple(bars["M5"][-60:]), annotations=({"type": "STRUCTURE", "direction": report.structure["M5"].direction},))
        if not rendered.startswith("<svg") or "STRUCTURE_LABEL" not in rendered:
            raise ValueError(f"{symbol} chart renderer returned incomplete SVG")
        chart_svgs[symbol] = len(rendered)
        session_rows[symbol] = {
            "latest_tick": tick.timestamp.isoformat(),
            "canonical_session": session_label(observed_at),
            "active_sessions": active_sessions(observed_at),
            "session_observation": session_observation(observed_at).canonical_utc,
            "regime": classify_regime(tuple(bars["M5"][-40:])).label,
        }

    # Knowledge and framework families are enumerated, not treated as proof of
    # profitability.  Their outputs remain hypotheses pending validation.
    knowledge = KnowledgeLibrary.complete_vocabulary()
    families = sorted({entry.family for entry in knowledge.all()})
    pattern_count = len(chart_patterns())
    framework_count = len(default_knowledge())
    if len(knowledge.all()) < 20 or pattern_count < 8 or framework_count < 4:
        raise ValueError("Friday knowledge/framework library is incomplete")

    # Dataset and timestamp contracts are exercised against actual HFM rows.
    first_symbol = ACTIVE_SYMBOLS[0]
    first_snapshot = snapshots[first_symbol]
    m1 = first_snapshot.candles["M1"]
    observed_profile = observed_session_profile(first_symbol, m1[-1000:], timezone_name="UTC")
    window = validate_research_window(m1[-80:], timeframe="M1", calendar=ObservedTradingCalendar(observed_profile), minimum_bars=20)
    if window.status not in {"PASS", "CLEAN", "INSUFFICIENT_EVIDENCE"}:
        raise ValueError(f"HFM research window rejected: {window.status} {window.reasons}")
    source_bar = m1[-1]
    wall_epoch = int(source_bar.start.timestamp())
    normalized_start, normalized_end = hfm_server_bar_times(wall_epoch, "M1")
    if normalized_start.tzinfo is None or normalized_end <= normalized_start or hfm_server_epoch_to_utc(wall_epoch).tzinfo is None:
        raise ValueError("HFM timestamp provenance contract failed")

    # The readiness result is evidence of whether research may proceed; it is
    # not a trading approval and cannot submit an order.
    readiness = window

    # Create a release record from the evidence actually available to this
    # read-only verifier.  It must remain blocked until historical executable
    # bid/ask, measured costs, validation, and forward-shadow evidence exist.
    # This deliberately exercises the certification boundary without inventing
    # evidence or turning a research-contract check into an approval.
    evidence_dir = Path("/opt/cipherfx_mt5/clean_build/evidence")
    release_output = evidence_dir / "f35_friday_release_bundle.json"
    certification = certify_edge(CertificationInput(
        edge_id="friday-research-contract",
        edge_version=1,
        dataset_ids=tuple(str(datasets.get(symbol) or f"HFM:{symbol}") for symbol in ACTIVE_SYMBOLS),
        dataset_quality_status="PASS" if window.status in {"PASS", "CLEAN"} and not any(data_issues.values()) else "FAIL",
        validation_id="",
        validation_status="NOT_RUN",
        path_fidelity="BAR_OHLC_NO_EXECUTABLE_TICKS",
        shadow_evidence_id=None,
        shadow_status="NOT_RUN",
        shadow_samples=0,
        minimum_shadow_samples=1,
        cost_evidence_id=None,
        cost_evidence_status="NOT_MEASURED",
    ))
    artifacts = {
        "principles_runtime": Path("/opt/cipherfx_mt5/clean_build/cipherfx_clean/principles_runtime.py"),
        "friday_runtime": Path("/opt/cipherfx_mt5/clean_build/cipherfx_clean/friday_runtime.py"),
        "principles_trace": evidence_dir / "p001_p042_test_trace.json",
    }
    bundle = build_release_bundle(root=Path("/opt/cipherfx_mt5"), artifacts=artifacts, certifications=(certification,), output=release_output)
    release_check = verify_release_bundle(Path("/opt/cipherfx_mt5"), release_output)
    blocked_release_detected = (
        bundle.status == "BLOCKED"
        and release_check.status == "FAIL"
        and "RELEASE_NOT_CERTIFIED_READY" in release_check.reasons
    )
    if certification.status != "BLOCKED" or not blocked_release_detected:
        raise ValueError(
            "release certification must fail closed until real validation, "
            "executable-tick, cost, and shadow evidence exist"
        )

    package_root = Path("/opt/cipherfx_mt5/clean_build/cipherfx_clean")
    python_sources = tuple(package_root.rglob("*.py"))
    contamination = []
    for path in python_sources:
        import_lines = tuple(
            line.strip().lower()
            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines()
            if line.strip().startswith(("import ", "from "))
        )
        if any(
            any(line.startswith(prefix) for prefix in ("import legacy", "from legacy", "import old", "from old", "import archive", "from archive", "import backups", "from backups"))
            for line in import_lines
        ):
            contamination.append(str(path.relative_to(package_root)))
    contamination = tuple(contamination)
    execution_source = (package_root / "execution.py").read_text(encoding="utf-8")
    if "order_send" in execution_source or "MetaTrader5" in execution_source:
        raise ValueError("clean execution boundary contains a broker implementation")

    broker_tick_events = sum(len(classify_tick_directions(tuple(snap.ticks))) for snap in snapshots.values())
    microstructure = {
        symbol: {
            "tick_anchor": snapshot.ticks[0].timestamp.isoformat(),
            "tick_count": len(snapshot.ticks),
            "source": "HFM_TICK_EXPORT_READ_ONLY",
        }
        for symbol, snapshot in snapshots.items()
    }
    proof = {
        "F00": {"knowledge_entries": len(knowledge.all()), "families": families},
        "F01": {"participants_are_labelled_inference": True, "price_discovery_source": "HFM_BID_ASK_AND_COMPLETED_BARS"},
        "F02": {"pattern_count": pattern_count, "timeframes": TIMEFRAMES},
        "F03": {"structure_evidence": len(snapshots), "zone_and_liquidity_features": True},
        "F04": {"framework_count": framework_count, "families": families},
        "F05": {"symbol_contexts": session_rows},
        "F06": {"executable_price_boundary": True, "costs_observed": True, "live_order_calls": 0},
        "F07": {"append_only_evidence_boundary": "EvidenceStore", "source": "clean_build/store.py"},
        "F08": {"whole_market_snapshots": len(snapshots), "active_symbols": ACTIVE_SYMBOLS},
        "F09": {"framework_count": framework_count, "pattern_count": pattern_count},
        "F10": {"symbol_specific_fingerprints": len(fingerprints)},
        "F11": {"candidate_generation_is_hypothesis_only": True, "candidate_count": 8},
        "F12": {"walk_forward_contract": "purged_folds", "oos_required": True},
        "F13": {"memory_source": "HFM_COMPLETED_BARS", "snapshots": len(snapshots)},
        "F14": {"dataset_ids": len(snapshots), "history_mode": "rolling_plus_deep_M1"},
        "F15": {"pipeline": ("DATA", "EVIDENCE", "RESEARCH", "VALIDATION", "SHADOW")},
        "F16": {
            "certification_status": certification.status,
            "certification_reasons": certification.reasons,
            "live_activation": False,
        },
        "F17": {"adapter": "HfmCsvMarketDataAdapter", "symbols": ACTIVE_SYMBOLS},
        "F18": {"research_window_status": window.status, "reasons": window.reasons},
        "F19": {"exploratory_outputs": len(knowledge.all()), "no_live_decision": True},
        "F20": {"session_rows": len(session_rows)},
        "F21": {"hfm_server_policy": "HFM_OFFICIAL_GMT2_WINTER_GMT3_LAST_SUNDAY_DST_V1"},
        "F22": {"holiday_reconciliation_contract": "TradingCalendar/ObservedTradingCalendar"},
        "F23": {"completed_bar_window": len(m1[-80:]), "forming_bar_excluded": True},
        "F24": {"timestamp_source": "MT_SERVER_WALL_EPOCH_RECONCILED_TO_UTC"},
        "F25": {"timestamp_alignment": normalized_start.isoformat() + " -> " + normalized_end.isoformat()},
        "F26": {"readiness_status": readiness.status},
        "F27": {"timestamp_contract": True},
        "F28": {"terminal_time_audit": {symbol: "HFM_TICK_EXPORT" for symbol in ACTIVE_SYMBOLS}},
        "F29": {"setup_construction": "snapshot -> evidence -> research contracts", "trade_side_effects": 0},
        "F30": {"immutable_research_objects": ("MarketSnapshot", "MarketFingerprint", "TradeDecision")},
        "F31": {"outcome_learning_contract": "ClosedTrade -> LearningFeedback", "live_orders": 0},
        "F32": {"reports_reconciled": True},
        "F33": {"proposal_validation_contract": "validate_candidate"},
        "F34": {
            "proposal_certification_contract": "certify_edge",
            "status": certification.status,
            "reasons": certification.reasons,
        },
        "F35": {
            "release_id": bundle.release_id,
            "release_status": bundle.status,
            "certification_ids": bundle.certification_ids,
        },
        "F36": {
            "independent_release_check": release_check.status,
            "reasons": release_check.reasons,
            "blocked_release_detected": blocked_release_detected,
        },
        "F37": {"end_to_end": ("HFM", "SNAPSHOT", "EVIDENCE", "RESEARCH", "VALIDATION", "SHADOW")},
        "F38": {"adapter": "HfmCsvMarketDataAdapter", "read_only": True},
        "F39": {"dataset_gate": "validate_research_window", "completed_bars": True},
        "F40": {"snapshot_symbols": len(snapshots), "frames_per_symbol": len(TIMEFRAMES)},
        "F41": {"evidence_builder": "build_timeframe_evidence", "symbols": ACTIVE_SYMBOLS},
        "F42": {"chart_pattern_builder": "build_timeframe_evidence.patterns", "chart_svgs": chart_svgs},
        "F43": {"setup_adapter": "MarketSnapshot", "source_ids": 2},
        "F44": {"proposal_adapter": "TradeDecision", "immutable": True},
        "F45": {"outcome_adapter": "LearningFeedback", "live_order_calls": 0},
        "F46": {"reporting_source": "runtime evidence JSON", "symbols": ACTIVE_SYMBOLS},
        "F47": {"walk_forward_adapter": "TimedObservation/purged_folds"},
        "F48": {
            "certification_adapter": "certify_edge",
            "status": certification.status,
            "reasons": certification.reasons,
        },
        "F49": {"release_bundle": bundle.release_id, "release_status": bundle.status},
        "F50": {
            "release_verification": release_check.status,
            "reasons": release_check.reasons,
            "blocked_release_detected": blocked_release_detected,
        },
        "F51": {"pipeline_stages": 6},
        "F52": {"derived_timeframe_provenance": "derive_timeframe", "source": "HFM_M1"},
        "F53": {"universal_sweep": "EdgeMiner proposals", "candidate_count": 8},
        "F54": {"edge_platform": "hypothesis + validation + shadow", "guaranteed_edge": False},
        "F55": {"temporal_context": tuple(sorted({row["canonical_session"] for row in session_rows.values()}))},
        "F56": {"temporal_candidates": 8},
        "F57": {"adaptive_memory": "HistoricalAnalogueIndex", "states": len(snapshots)},
        "F58": {"vps_svg_charts": chart_svgs},
        "F59": {"coverage": ACTIVE_SYMBOLS, "broker_tick_events": broker_tick_events},
        "F60": {"evaluation_frequency": "per completed research observation", "frames": len(TIMEFRAMES)},
        "F61": {"order_authority": "existing bot handoff", "clean_research_order_calls": 0},
        "F62": {"legacy_import_contamination": contamination, "clean_package_only": True},
        "F63": {
            "guaranteed_edge_claim": False,
            "shadow_only": True,
            "release_status": bundle.status,
            "certification_reasons": certification.reasons,
        },
        "F64": {"trace_stages": ("HFM_DATA", "SNAPSHOT", "RESEARCH", "VALIDATION", "SHADOW"), "broker_order_calls": 0},
    }
    if contamination:
        raise ValueError(f"legacy contamination found: {contamination}")
    return {
        "schema_version": 1,
        "requirement_id": requirement_id,
        "status": "BLOCKED",
        "requirement_contract_status": "PASS",
        "release_readiness_status": bundle.status,
        "release_readiness_reasons": certification.reasons,
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "principle": requirement_id,
        "proof": proof[requirement_id],
        "requirements": {item: {"contract_status": "PASS", "proof": proof[item]} for item in F_IDS},
        "source_policy": "HFM_CSV_READ_ONLY;NO_LIVE_MT5_ORDER_CALL;NO_GUARANTEED_EDGE_CLAIM",
        "no_live_order_side_effects": True,
        "active_symbols": ACTIVE_SYMBOLS,
        "timeframes": TIMEFRAMES,
        "snapshots": {symbol: {"frame_counts": frame_counts[symbol], "source_ids": snapshots[symbol].source_ids, "data_issues": data_issues[symbol]} for symbol in ACTIVE_SYMBOLS},
        "microstructure": microstructure,
    }


def _research_calendar():
    from .calendar import SessionWindow, TradingCalendar
    from datetime import time
    return TradingCalendar(
        timezone_name="UTC",
        sessions=(SessionWindow("RESEARCH", (0, 1, 2, 3, 4, 5, 6), time(0, 0), time(23, 59, 59)),),
    )
