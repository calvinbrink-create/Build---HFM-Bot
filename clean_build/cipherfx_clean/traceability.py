"""Explicit governing-requirement ownership for the clean architecture.

Bindings identify where a requirement must be implemented and exercised.
They are planning and audit metadata, never completion evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping


@dataclass(frozen=True)
class RequirementBinding:
    requirement_id: str
    modules: tuple[str, ...]
    stages: tuple[str, ...]


def requirement_bindings() -> Mapping[str, RequirementBinding]:
    rows: dict[str, RequirementBinding] = {}

    def bind(ids: str, modules: tuple[str, ...], stages: tuple[str, ...]) -> None:
        for requirement_id in ids.split():
            if requirement_id in rows:
                raise ValueError(f"duplicate requirement binding: {requirement_id}")
            rows[requirement_id] = RequirementBinding(requirement_id, modules, stages)

    # Component matrix: market -> evidence -> research -> decision -> bot -> feedback.
    bind("C001", ("cipherfx_clean.runtime", "cipherfx_clean.execution"), ("ARCHITECTURE", "DECISION", "EXECUTION"))
    bind("C002", ("cipherfx_clean.execution",), ("EXECUTION",))
    bind("C003", ("cipherfx_clean.mt5_boundary", "cipherfx_clean.operations", "cipherfx_clean.execution"), ("BROKER", "EXECUTION"))
    bind("C004", ("cipherfx_clean.operations",), ("RISK", "EXECUTION"))
    bind("C005", ("cipherfx_clean.management",), ("MANAGEMENT",))
    bind("C006", ("cipherfx_clean.market", "cipherfx_clean.store"), ("DATA",))
    bind("C007", ("cipherfx_clean.market", "cipherfx_clean.data_quality", "cipherfx_clean.observation"), ("DATA", "VALIDATION"))
    bind("C008", ("cipherfx_clean.market", "cipherfx_clean.hfm_data"), ("DATA", "SNAPSHOT"))
    bind("C009 C010", ("cipherfx_clean.contracts", "cipherfx_clean.snapshot"), ("SNAPSHOT",))
    bind("C011 C012 C013", ("cipherfx_clean.charts", "cipherfx_clean.intelligence.chart", "cipherfx_clean.intelligence.renderer"), ("CHARTS", "INTELLIGENCE"))
    bind("C014", ("cipherfx_clean.store", "cipherfx_clean.intelligence.renderer"), ("CHARTS", "DATA"))
    bind("C015 C016", ("cipherfx_clean.intelligence.structure", "cipherfx_clean.intelligence.evidence"), ("INTELLIGENCE",))
    bind("C017 C018", ("cipherfx_clean.intelligence.zones", "cipherfx_clean.intelligence.evidence"), ("INTELLIGENCE",))
    bind("C019", ("cipherfx_clean.intelligence.advanced", "cipherfx_clean.intelligence.zones"), ("INTELLIGENCE", "RESEARCH"))
    bind("C020 C021", ("cipherfx_clean.intelligence.liquidity", "cipherfx_clean.intelligence.market_features"), ("INTELLIGENCE",))
    bind("C022", ("cipherfx_clean.intelligence.outcomes", "cipherfx_clean.store"), ("MEMORY", "FEEDBACK"))
    bind("C023 C024 C025", ("cipherfx_clean.intelligence.market_features", "cipherfx_clean.intelligence.evidence"), ("INTELLIGENCE",))
    bind("C026 C027 C028 C029", ("cipherfx_clean.intelligence.market_features",), ("DATA", "INTELLIGENCE"))
    bind("C030", ("cipherfx_clean.intelligence.microstructure",), ("INTELLIGENCE",))
    bind("C031", ("cipherfx_clean.intelligence.microstructure", "cipherfx_clean.cost_evidence"), ("INTELLIGENCE", "COSTS"))
    bind(
        "C032",
        ("cipherfx_clean.slippage", "cipherfx_clean.execution", "cipherfx_clean.store"),
        ("BROKER", "COSTS", "FEEDBACK"),
    )
    bind(
        "C033",
        (
            "cipherfx_clean.intelligence.latency",
            "cipherfx_clean.runtime",
            "cipherfx_clean.execution",
            "cipherfx_clean.store",
        ),
        ("BROKER", "COSTS", "FEEDBACK"),
    )
    bind("C034", ("cipherfx_clean.intelligence.advanced", "cipherfx_clean.intelligence.market_features"), ("INTELLIGENCE",))
    bind("C035", ("cipherfx_clean.intelligence.market_features", "cipherfx_clean.intelligence.advanced"), ("INTELLIGENCE", "RESEARCH"))
    bind("C036", ("cipherfx_clean.intelligence.atr", "cipherfx_clean.intelligence.market_features"), ("INTELLIGENCE", "RESEARCH"))
    bind("C037", ("cipherfx_clean.intelligence.volatility_regime",), ("INTELLIGENCE", "RESEARCH"))
    bind("C038", ("cipherfx_clean.intelligence.volatility_dynamics",), ("INTELLIGENCE", "RESEARCH"))
    bind("C039", ("cipherfx_clean.intelligence.volatility_dynamics",), ("INTELLIGENCE", "RESEARCH"))
    bind("C040", ("cipherfx_clean.intelligence.volatility_dynamics",), ("INTELLIGENCE", "RESEARCH"))
    bind(
        "C041",
        ("cipherfx_clean.hfm_data", "cipherfx_clean.intelligence.session", "cipherfx_clean.intelligence.engine"),
        ("BROKER", "CALENDAR", "INTELLIGENCE"),
    )
    bind(
        "C042",
        ("cipherfx_clean.intelligence.session_profiles", "cipherfx_clean.intelligence.engine"),
        ("DATA", "INTELLIGENCE", "RESEARCH"),
    )
    bind(
        "C043",
        ("cipherfx_clean.intelligence.session_profiles", "cipherfx_clean.intelligence.engine"),
        ("DATA", "INTELLIGENCE", "RESEARCH"),
    )
    bind(
        "C044",
        ("cipherfx_clean.intelligence.london_sweeps", "cipherfx_clean.intelligence.session_profiles", "cipherfx_clean.intelligence.engine"),
        ("DATA", "INTELLIGENCE", "RESEARCH"),
    )
    bind(
        "C045 C046",
        ("cipherfx_clean.intelligence.ny_transitions", "cipherfx_clean.intelligence.session_profiles", "cipherfx_clean.intelligence.engine"),
        ("DATA", "INTELLIGENCE", "RESEARCH"),
    )
    bind(
        "C047",
        ("cipherfx_clean.intelligence.opening_ranges", "cipherfx_clean.intelligence.session_profiles", "cipherfx_clean.intelligence.engine"),
        ("DATA", "INTELLIGENCE", "RESEARCH"),
    )
    bind(
        "C048",
        ("cipherfx_clean.intelligence.opening_range_breakouts", "cipherfx_clean.intelligence.opening_ranges", "cipherfx_clean.intelligence.engine"),
        ("DATA", "INTELLIGENCE", "RESEARCH"),
    )
    bind(
        "C049",
        ("cipherfx_clean.intelligence.false_opening_range_breakouts", "cipherfx_clean.intelligence.opening_range_breakouts", "cipherfx_clean.intelligence.engine"),
        ("DATA", "INTELLIGENCE", "RESEARCH"),
    )
    bind(
        "C050",
        ("cipherfx_clean.intelligence.session_vwap", "cipherfx_clean.intelligence.session", "cipherfx_clean.intelligence.engine"),
        ("DATA", "INTELLIGENCE", "RESEARCH"),
    )
    bind(
        "C051",
        ("cipherfx_clean.intelligence.vwap_deviation_stats", "cipherfx_clean.intelligence.session_vwap", "cipherfx_clean.intelligence.engine"),
        ("DATA", "INTELLIGENCE", "RESEARCH"),
    )
    bind("C052", ("cipherfx_clean.intelligence.vwap_reclaim_rejection", "cipherfx_clean.intelligence.session_vwap", "cipherfx_clean.intelligence.engine"), ("DATA", "INTELLIGENCE", "RESEARCH"))
    bind("C053", ("cipherfx_clean.intelligence.tick_volume", "cipherfx_clean.intelligence.engine", "cipherfx_clean.hfm_data"), ("DATA", "INTELLIGENCE"))
    bind("C054", ("cipherfx_clean.intelligence.relative_volume", "cipherfx_clean.intelligence.session_vwap", "cipherfx_clean.intelligence.engine"), ("DATA", "INTELLIGENCE", "RESEARCH"))
    bind("C055", ("cipherfx_clean.intelligence.trend_engine", "cipherfx_clean.intelligence.advanced", "cipherfx_clean.intelligence.engine"), ("INTELLIGENCE", "RESEARCH"))
    bind("C056", ("cipherfx_clean.intelligence.trend_persistence", "cipherfx_clean.intelligence.trend_engine", "cipherfx_clean.intelligence.engine"), ("INTELLIGENCE", "RESEARCH", "MEMORY"))
    bind("C057", ("cipherfx_clean.intelligence.mean_reversion_engine", "cipherfx_clean.intelligence.advanced", "cipherfx_clean.intelligence.engine"), ("INTELLIGENCE", "RESEARCH", "MEMORY"))
    bind("C058", ("cipherfx_clean.intelligence.breakout_engine", "cipherfx_clean.intelligence.advanced", "cipherfx_clean.intelligence.engine"), ("INTELLIGENCE", "RESEARCH"))
    bind("C059", ("cipherfx_clean.intelligence.breakout_quality_engine", "cipherfx_clean.intelligence.breakout_engine", "cipherfx_clean.intelligence.engine"), ("INTELLIGENCE", "RESEARCH", "MEMORY"))
    bind("C060", ("cipherfx_clean.intelligence.failed_breakout", "cipherfx_clean.intelligence.breakout_quality_engine", "cipherfx_clean.intelligence.engine"), ("INTELLIGENCE", "RESEARCH", "MEMORY"))
    bind("C061", ("cipherfx_clean.intelligence.reversal_engine", "cipherfx_clean.intelligence.liquidity", "cipherfx_clean.intelligence.engine"), ("INTELLIGENCE", "RESEARCH", "MEMORY"))
    bind("C062", ("cipherfx_clean.intelligence.continuation_engine", "cipherfx_clean.intelligence.advanced", "cipherfx_clean.intelligence.engine"), ("INTELLIGENCE", "RESEARCH", "MEMORY"))
    bind("C063 C064 C065", ("cipherfx_clean.intelligence.cross_asset", "cipherfx_clean.intelligence.engine"), ("DATA", "INTELLIGENCE", "RESEARCH"))
    bind("C066", ("cipherfx_clean.intelligence.market_regime_engine", "cipherfx_clean.intelligence.engine"), ("INTELLIGENCE", "RESEARCH"))
    bind("C067", ("cipherfx_clean.intelligence.regime_probability_engine", "cipherfx_clean.intelligence.engine"), ("INTELLIGENCE", "RESEARCH"))
    bind("C068", ("cipherfx_clean.intelligence.router",), ("INTELLIGENCE", "DECISION"))
    bind("C069 C070 C071 C072 C073 C074 C075", ("cipherfx_clean.intelligence.knowledge", "cipherfx_clean.intelligence.strategies"), ("KNOWLEDGE", "INTELLIGENCE"))
    bind("C076", ("cipherfx_clean.intelligence.patterns", "cipherfx_clean.intelligence.strategies"), ("KNOWLEDGE", "INTELLIGENCE"))
    bind("C077 C078", ("cipherfx_clean.intelligence.outcomes", "cipherfx_clean.intelligence.data_library"), ("MEMORY", "FEEDBACK"))
    bind("C079", ("cipherfx_clean.features", "cipherfx_clean.historical_memory"), ("INTELLIGENCE", "MEMORY"))
    bind("C080", ("cipherfx_clean.intelligence.feature_store", "cipherfx_clean.store"), ("DATA", "MEMORY"))
    bind("C081", ("cipherfx_clean.intelligence.data_library", "cipherfx_clean.historical_memory"), ("DATA", "MEMORY"))
    bind("C082 C083 C084", ("cipherfx_clean.historical_memory",), ("MEMORY", "RESEARCH"))
    bind("C085 C086 C087 C088 C089 C090 C091", ("cipherfx_clean.intelligence.outcomes", "cipherfx_clean.historical_memory", "cipherfx_clean.intelligence.research"), ("MEMORY", "RESEARCH"))
    bind("C092", ("cipherfx_clean.intelligence.research", "cipherfx_clean.cost_evidence"), ("RESEARCH", "COSTS"))
    bind("C093", ("cipherfx_clean.intelligence.edge_miner",), ("RESEARCH",))
    bind("C094", ("cipherfx_clean.intelligence.hypothesis",), ("RESEARCH",))
    bind("C095", ("cipherfx_clean.research_pipeline",), ("RESEARCH", "VALIDATION"))
    bind("C096 C097 C098", ("cipherfx_clean.intelligence.validation_full",), ("VALIDATION",))
    bind("C099", ("cipherfx_clean.intelligence.hypothesis", "cipherfx_clean.intelligence.outcomes"), ("RESEARCH", "MEMORY"))
    bind("C100 C101 C102 C103 C104 C105", ("cipherfx_clean.feedback", "cipherfx_clean.intelligence.data_library", "cipherfx_clean.intelligence.outcomes"), ("MEMORY", "FEEDBACK", "RESEARCH"))
    bind("C106 C107 C108", ("cipherfx_clean.intelligence.attribution",), ("RESEARCH", "VALIDATION"))
    bind("C109 C110 C111 C112", ("cipherfx_clean.intelligence.validation_full", "cipherfx_clean.intelligence.validation"), ("VALIDATION",))
    bind("C113 C114", ("cipherfx_clean.intelligence.research", "cipherfx_clean.research_pipeline"), ("RESEARCH", "GOVERNANCE"))
    bind("C115", ("cipherfx_clean.store", "cipherfx_clean.intelligence.edge_validation"), ("RESEARCH", "GOVERNANCE"))
    bind("C116", ("cipherfx_clean.intelligence.validation_full",), ("VALIDATION",))
    bind("C117", ("cipherfx_clean.cost_evidence", "cipherfx_clean.intelligence.costs"), ("BROKER", "COSTS", "VALIDATION"))
    bind("C118", ("cipherfx_clean.intelligence.execution_model", "cipherfx_clean.backtest"), ("BROKER", "REPLAY", "VALIDATION"))
    bind("C119", ("cipherfx_clean.operations",), ("BROKER", "EXECUTION"))
    bind("C120", ("cipherfx_clean.intelligence.execution_model", "cipherfx_clean.cost_evidence"), ("BROKER", "VALIDATION"))
    bind("C121 C122 C123 C124", ("cipherfx_clean.intelligence.validation_full",), ("VALIDATION",))
    bind("C125", ("cipherfx_clean.intelligence.news",), ("INTELLIGENCE", "RESEARCH"))
    bind("C126 C127", ("cipherfx_clean.intelligence.planning",), ("PLANNING", "FEEDBACK"))
    bind("C128", ("cipherfx_clean.shadow_runtime", "cipherfx_clean.shadow"), ("SHADOW",))
    bind("C129", ("cipherfx_clean.intelligence.tournament",), ("SHADOW", "RESEARCH"))
    bind("C130 C131", ("cipherfx_clean.replay",), ("REPLAY", "VALIDATION"))
    bind("C132 C133 C134", ("cipherfx_clean.intelligence.monitor", "cipherfx_clean.intelligence.governance"), ("MONITORING", "GOVERNANCE"))
    bind("C135 C136 C137 C138 C139 C140 C141", ("cipherfx_clean.intelligence.governance", "cipherfx_clean.intelligence.research", "cipherfx_clean.store"), ("GOVERNANCE", "FEEDBACK"))
    bind("C142", ("cipherfx_clean.intelligence.runtime_contracts", "cipherfx_clean.intelligence.provider"), ("DECISION",))
    bind("C143", ("cipherfx_clean.intelligence.runtime_contracts", "cipherfx_clean.contracts"), ("DECISION",))
    bind("C144", ("cipherfx_clean.validation",), ("VALIDATION", "DECISION"))
    bind("C145", ("cipherfx_clean.execution",), ("DECISION", "EXECUTION"))
    bind("C146", ("cipherfx_clean.runtime", "cipherfx_clean.execution"), ("ARCHITECTURE", "EXECUTION"))
    bind("C147", ("cipherfx_clean.operations",), ("BROKER", "RISK", "EXECUTION"))
    bind("C148", ("cipherfx_clean.execution", "cipherfx_clean.mt5_boundary"), ("BROKER", "EXECUTION"))
    bind("C149", ("cipherfx_clean.management",), ("MANAGEMENT",))
    bind("C150", ("cipherfx_clean.broker_feedback", "cipherfx_clean.feedback"), ("BROKER", "FEEDBACK"))
    bind("C151", ("cipherfx_clean.feedback",), ("FEEDBACK", "RESEARCH"))
    bind("C152", ("cipherfx_clean.replay", "cipherfx_clean.charts"), ("REPLAY", "CHARTS"))
    bind("C153", ("cipherfx_clean.historical_memory",), ("MEMORY", "DECISION"))
    bind("C154", ("cipherfx_clean.intelligence.research", "cipherfx_clean.historical_memory"), ("RESEARCH", "DECISION"))
    bind("C155", ("cipherfx_clean.validation", "cipherfx_clean.contracts"), ("DECISION", "VALIDATION"))
    bind("C156", ("cipherfx_clean.intelligence.lineage", "cipherfx_clean.store"), ("LINEAGE",))
    bind("C157", ("cipherfx_clean.runtime", "cipherfx_clean.store"), ("ARCHITECTURE", "LINEAGE"))
    bind("C158 C159", ("cipherfx_clean.dashboard",), ("DASHBOARD",))
    bind("C160", ("cipherfx_clean.runtime", "cipherfx_clean.execution", "cipherfx_clean.management", "cipherfx_clean.feedback"), ("ARCHITECTURE", "DATA", "INTELLIGENCE", "MEMORY", "RESEARCH", "VALIDATION", "EXECUTION", "MANAGEMENT", "FEEDBACK"))

    # Edge-system principles use the same owned capabilities, never a parallel engine.
    bind("P001 P002", ("cipherfx_clean.intelligence.data_library", "cipherfx_clean.historical_memory", "cipherfx_clean.store"), ("DATA", "MEMORY"))
    bind("P003", ("cipherfx_clean.intelligence.microstructure", "cipherfx_clean.market"), ("DATA", "INTELLIGENCE"))
    bind("P004 P005", ("cipherfx_clean.features", "cipherfx_clean.historical_memory"), ("MEMORY", "RESEARCH"))
    bind("P006 P007", ("cipherfx_clean.intelligence.edge_miner", "cipherfx_clean.intelligence.hypothesis"), ("RESEARCH",))
    bind("P008 P009 P010 P011", ("cipherfx_clean.intelligence.attribution", "cipherfx_clean.intelligence.outcomes", "cipherfx_clean.feedback"), ("RESEARCH", "FEEDBACK"))
    bind("P012 P013 P014 P015", ("cipherfx_clean.intelligence.attribution",), ("RESEARCH", "VALIDATION"))
    bind("P016 P017", ("cipherfx_clean.candidate_universe", "cipherfx_clean.intelligence.edge_miner"), ("RESEARCH",))
    bind("P018 P019", ("cipherfx_clean.intelligence.edge_validation", "cipherfx_clean.intelligence.governance"), ("RESEARCH", "GOVERNANCE"))
    bind("P020 P021", ("cipherfx_clean.research_pipeline", "cipherfx_clean.store"), ("RESEARCH", "GOVERNANCE"))
    bind("P022 P023 P024 P025", ("cipherfx_clean.intelligence.validation_full",), ("VALIDATION",))
    bind("P026 P027 P028", ("cipherfx_clean.intelligence.validation_full",), ("VALIDATION", "RESEARCH"))
    bind("P029", ("cipherfx_clean.cost_evidence",), ("BROKER", "COSTS", "VALIDATION"))
    bind("P030", ("cipherfx_clean.intelligence.execution_model", "cipherfx_clean.backtest"), ("BROKER", "REPLAY", "VALIDATION"))
    bind("P031", ("cipherfx_clean.shadow_runtime", "cipherfx_clean.shadow"), ("SHADOW",))
    bind("P032 P033 P034 P035", ("cipherfx_clean.intelligence.monitor", "cipherfx_clean.intelligence.governance"), ("MONITORING", "GOVERNANCE"))
    bind("P036", ("cipherfx_clean.intelligence.provider", "cipherfx_clean.intelligence.hypothesis"), ("RESEARCH", "DECISION"))
    bind("P037", ("cipherfx_clean.execution", "cipherfx_clean.mt5_boundary"), ("BROKER", "EXECUTION"))
    bind("P038", ("cipherfx_clean.execution", "cipherfx_clean.operations"), ("BROKER", "EXECUTION"))
    bind("P039", ("cipherfx_clean.broker_feedback", "cipherfx_clean.feedback"), ("BROKER", "FEEDBACK"))
    bind("P040", ("cipherfx_clean.intelligence.validation_full", "cipherfx_clean.cost_evidence"), ("RESEARCH", "VALIDATION"))
    bind("P041 P042", ("cipherfx_clean.research_orchestrator", "cipherfx_clean.intelligence.governance"), ("ARCHITECTURE", "RESEARCH", "GOVERNANCE"))

    # Friday obligations are bound to their clean owners and runtime stages.
    bind("F00 F01", ("cipherfx_clean.intelligence.knowledge",), ("KNOWLEDGE", "INTELLIGENCE"))
    bind("F02", ("cipherfx_clean.candles", "cipherfx_clean.intelligence.candles"), ("KNOWLEDGE", "INTELLIGENCE"))
    bind("F03", ("cipherfx_clean.intelligence.evidence",), ("INTELLIGENCE",))
    bind("F04", ("cipherfx_clean.intelligence.strategies",), ("KNOWLEDGE", "RESEARCH"))
    bind("F05", ("cipherfx_clean.calendar", "cipherfx_clean.intelligence.session", "cipherfx_clean.intelligence.regime"), ("CALENDAR", "INTELLIGENCE"))
    bind("F06", ("cipherfx_clean.cost_evidence", "cipherfx_clean.intelligence.execution_model", "cipherfx_clean.operations"), ("BROKER", "COSTS", "VALIDATION"))
    bind("F07", ("cipherfx_clean.store", "cipherfx_clean.intelligence.lineage"), ("DATA", "LINEAGE"))
    bind("F08", ("cipherfx_clean.intelligence.data_library", "cipherfx_clean.historical_memory"), ("DATA", "MEMORY"))
    bind("F09", ("cipherfx_clean.intelligence.strategies",), ("KNOWLEDGE", "RESEARCH"))
    bind("F10", ("cipherfx_clean.intelligence.validation_full",), ("RESEARCH", "VALIDATION"))
    bind("F11", ("cipherfx_clean.candidate_universe", "cipherfx_clean.intelligence.edge_miner"), ("RESEARCH",))
    bind("F12", ("cipherfx_clean.intelligence.validation_full",), ("VALIDATION",))
    bind("F13", ("cipherfx_clean.populate_memory", "cipherfx_clean.historical_memory"), ("DATA", "MEMORY"))
    bind("F14", ("cipherfx_clean.hfm_data", "cipherfx_clean.store"), ("DATA", "LINEAGE"))
    bind("F15", ("cipherfx_clean.research_orchestrator", "cipherfx_clean.runtime"), ("ARCHITECTURE", "RESEARCH"))
    bind("F16", ("cipherfx_clean.release",), ("GOVERNANCE", "RELEASE"))
    bind("F17 F18", ("cipherfx_clean.hfm_data", "cipherfx_clean.data_quality"), ("BROKER", "DATA", "VALIDATION"))
    bind("F19", ("cipherfx_clean.candidate_universe", "cipherfx_clean.research_pipeline"), ("RESEARCH", "VALIDATION"))
    bind("F20 F21 F22", ("cipherfx_clean.calendar", "cipherfx_clean.intelligence.session"), ("BROKER", "CALENDAR", "DATA"))
    bind("F23", ("cipherfx_clean.data_quality",), ("DATA", "VALIDATION"))
    bind("F24 F25 F27 F28", ("cipherfx_clean.time_contract", "cipherfx_clean.hfm_data"), ("BROKER", "DATA", "LINEAGE"))
    bind("F26", ("cipherfx_clean.release", "cipherfx_clean.compliance"), ("VALIDATION", "GOVERNANCE"))
    bind("F29 F30", ("cipherfx_clean.contracts", "cipherfx_clean.intelligence.decision"), ("DECISION",))
    bind("F31", ("cipherfx_clean.feedback", "cipherfx_clean.intelligence.outcomes"), ("BROKER", "FEEDBACK", "MEMORY"))
    bind("F32", ("cipherfx_clean.broker_feedback", "cipherfx_clean.hfm_history"), ("FEEDBACK", "LINEAGE"))
    bind("F33 F34", ("cipherfx_clean.intelligence.validation_full", "cipherfx_clean.release"), ("VALIDATION", "GOVERNANCE"))
    bind("F35 F36", ("cipherfx_clean.release",), ("RELEASE", "GOVERNANCE"))
    bind("F37", ("cipherfx_clean.research_orchestrator",), ("ARCHITECTURE", "RESEARCH", "RELEASE"))
    bind("F38 F39", ("cipherfx_clean.hfm_data", "cipherfx_clean.data_quality"), ("BROKER", "DATA", "VALIDATION"))
    bind("F40", ("cipherfx_clean.snapshot",), ("SNAPSHOT",))
    bind("F41 F42", ("cipherfx_clean.intelligence.engine", "cipherfx_clean.intelligence.evidence"), ("INTELLIGENCE",))
    bind("F43 F44", ("cipherfx_clean.intelligence.decision", "cipherfx_clean.validation"), ("DECISION", "VALIDATION"))
    bind("F45 F46", ("cipherfx_clean.broker_feedback", "cipherfx_clean.feedback"), ("BROKER", "FEEDBACK", "LINEAGE"))
    bind("F47 F48", ("cipherfx_clean.intelligence.validation_full", "cipherfx_clean.release"), ("VALIDATION", "GOVERNANCE"))
    bind("F49 F50 F51", ("cipherfx_clean.release", "cipherfx_clean.research_orchestrator"), ("RELEASE", "ARCHITECTURE"))
    bind("F52", ("cipherfx_clean.hfm_data",), ("DATA", "LINEAGE"))
    bind("F53 F54", ("cipherfx_clean.candidate_universe", "cipherfx_clean.research_pipeline", "cipherfx_clean.sharded_research"), ("RESEARCH", "VALIDATION"))
    bind("F55 F56", ("cipherfx_clean.intelligence.session", "cipherfx_clean.candidate_universe"), ("RESEARCH", "CALENDAR"))
    bind("F57", ("cipherfx_clean.intelligence.governance", "cipherfx_clean.feedback"), ("GOVERNANCE", "FEEDBACK"))
    bind("F58", ("cipherfx_clean.intelligence.renderer", "cipherfx_clean.store"), ("CHARTS", "DATA"))
    bind("F59", ("cipherfx_clean.hfm_audit", "cipherfx_clean.hfm_data"), ("BROKER", "DATA"))
    bind("F60", ("cipherfx_clean.scheduler", "cipherfx_clean.live_runtime"), ("LIVE", "INTELLIGENCE"))
    bind("F61", ("cipherfx_clean.execution", "cipherfx_clean.mt5_boundary"), ("BROKER", "EXECUTION"))
    bind("F62", ("cipherfx_clean.architecture_audit",), ("ARCHITECTURE", "VALIDATION"))
    bind("F63", ("cipherfx_clean.intelligence.validation_full", "cipherfx_clean.release"), ("VALIDATION", "GOVERNANCE"))
    bind("F64", ("cipherfx_clean.runtime", "cipherfx_clean.execution", "cipherfx_clean.feedback"), ("ARCHITECTURE", "DATA", "INTELLIGENCE", "MEMORY", "VALIDATION", "EXECUTION", "FEEDBACK"))

    _validate_complete(rows)
    return rows


def _validate_complete(rows: Mapping[str, RequirementBinding]) -> None:
    expected = {
        *(f"C{number:03d}" for number in range(1, 161)),
        *(f"P{number:03d}" for number in range(1, 43)),
        *(f"F{number:02d}" for number in range(65)),
    }
    observed = set(rows)
    if observed != expected:
        raise ValueError(
            f"traceability must bind all 267 requirements; missing={sorted(expected-observed)} "
            f"unexpected={sorted(observed-expected)}"
        )
