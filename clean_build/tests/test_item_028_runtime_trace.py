from datetime import datetime, timezone

from cipherfx_clean.runtime_trace import (
    NO_TRADE_STAGES,
    TRADE_STAGES,
    RuntimeTraceEvent,
    verify_completed_trace,
)
from cipherfx_clean.store import EvidenceStore


NOW = datetime(2026, 8, 24, 8, 30, tzinfo=timezone.utc)


def _events(stages):
    return tuple(
        RuntimeTraceEvent("trace-1", index, stage, NOW, f"artifact-{index}", {})
        for index, stage in enumerate(stages, 1)
    )


def test_trace_verifier_requires_the_exact_executed_stage_order():
    assert verify_completed_trace(_events(TRADE_STAGES), execution_required=True).valid
    broken = _events(TRADE_STAGES[:-2] + tuple(reversed(TRADE_STAGES[-2:])))
    result = verify_completed_trace(broken, execution_required=True)
    assert result.valid is False
    assert result.reason == "STAGE_ORDER_MISMATCH"


def test_trace_store_is_immutable_idempotent_and_survives_reopen(tmp_path):
    path = tmp_path / "trace.sqlite3"
    store = EvidenceStore(path)
    for event in _events(NO_TRADE_STAGES):
        store.write_runtime_trace_event(event)
        store.write_runtime_trace_event(event)
    store.close()

    reopened = EvidenceStore(path)
    observed = reopened.runtime_trace("trace-1")
    assert observed == _events(NO_TRADE_STAGES)
    assert verify_completed_trace(observed, execution_required=False).valid
    reopened.close()
