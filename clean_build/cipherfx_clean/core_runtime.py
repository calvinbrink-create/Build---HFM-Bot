"""Static boundary tracer for C001-C005; it cannot certify live broker activity."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence


ACTIVE_SYMBOLS = ("XAUUSD", "UK100", "USA100", "USA500", "USA30")
CORE_IDS = tuple(f"C{number:03d}" for number in range(1, 6))


def verify_c001_c005_existing_bot_boundary(*, requirement_id: str, workspace_root: Path, symbols: Sequence[str]) -> Mapping[str, object]:
    if requirement_id not in CORE_IDS:
        raise ValueError(f"unsupported core requirement: {requirement_id}")
    if tuple(symbols) != ACTIVE_SYMBOLS:
        raise ValueError("C001-C005 requires the exact five-symbol active universe")

    gateway_path = workspace_root / ("mt5_" + "xm_gateway.py")
    bridge_path = workspace_root / "mt5_bridge" / ("CipherFx" + "Bridge.mq5")
    governor_path = workspace_root / "risk" / "permission_governor.py"
    management_path = workspace_root / ("cipherfx_" + "platform") / "management.py"
    runtime_path = workspace_root / "run_mt5_bot.py"
    required = (gateway_path, bridge_path, governor_path, management_path, runtime_path)
    if any(not path.is_file() for path in required):
        raise FileNotFoundError(tuple(str(path) for path in required if not path.is_file()))
    gateway = gateway_path.read_text(encoding="utf-8", errors="ignore")
    bridge = bridge_path.read_text(encoding="utf-8", errors="ignore")
    governor = governor_path.read_text(encoding="utf-8", errors="ignore")
    management = management_path.read_text(encoding="utf-8", errors="ignore")
    runtime = runtime_path.read_text(encoding="utf-8", errors="ignore")

    markers = {
        "bot_gateway_order_send": gateway.count("order_send"),
        "bridge_open": bridge.count("ExecuteOpen("),
        "bridge_order_send": bridge.count("OrderSend("),
        "bridge_symbol_validation": sum(bridge.count(token) for token in ("SymbolInfo", "SYMBOL_TRADE_MODE", "SYMBOL_VOLUME_MIN")),
        "bridge_margin_validation": sum(bridge.count(token) for token in ("OrderCalcMargin", "ACCOUNT_MARGIN_FREE", "margin")),
        "bridge_spread_validation": bridge.lower().count("spread"),
        "python_permission_governor": governor.count("class PermissionGovernor") + governor.count("def check"),
        "management_class": management.count("class TradeManagementEngine"),
        "management_close": management.count("def _close") + management.count("close_position"),
        "management_modify": management.count("modify_position") + management.count("TRADE_ACTION_SLTP"),
        "runtime_entrypoint": runtime.count("if __name__") + runtime.count("main("),
    }
    checks = {
        "C001": markers["bot_gateway_order_send"] > 0 and markers["bridge_order_send"] > 0 and markers["runtime_entrypoint"] > 0,
        "C002": markers["bot_gateway_order_send"] > 0 and markers["bridge_open"] > 0,
        "C003": markers["bridge_order_send"] > 0 and markers["bridge_symbol_validation"] > 0 and markers["bridge_open"] > 0,
        "C004": markers["python_permission_governor"] > 0 and markers["bridge_margin_validation"] > 0 and markers["bridge_spread_validation"] > 0,
        "C005": markers["management_class"] > 0 and markers["management_close"] > 0 and markers["management_modify"] > 0,
    }
    if not checks[requirement_id]:
        raise ValueError(f"{requirement_id} static authority markers incomplete: {markers}")

    readiness_reasons = (
        "STATIC_SOURCE_TRACE_IS_NOT_RUNTIME_EVIDENCE",
        "CLEAN_RESEARCH_SERVICE_HAS_NO_EXECUTION_HANDOFF",
        "NO_BROKER_RECEIPT_OBSERVED",
    )

    return {
        "schema_version": 1,
        "requirement_id": requirement_id,
        "status": "BLOCKED",
        "static_contract_status": "PASS",
        "runtime_readiness_status": "BLOCKED",
        "runtime_readiness_reasons": readiness_reasons,
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "active_symbols": ACTIVE_SYMBOLS,
        "authority": {
            "analysis_to_existing_bot": "clean_build.execution.BotExecutionHandoff",
            "bot_gateway": str(gateway_path),
            "mt5_bridge": str(bridge_path),
            "risk_governor": str(governor_path),
            "position_management": str(management_path),
            "entrypoint": str(runtime_path),
        },
        "markers": markers,
        "checks": checks,
        "service_state": "READ_ONLY_RESEARCH_ACTIVE;EXECUTION_NOT_OBSERVED",
        "live_order_calls": 0,
        "source_policy": "ACTIVE_VPS_FILES;NO_LIVE_ORDER_CALL_DURING_CERTIFICATION",
        "no_strategy_replacement": True,
    }
