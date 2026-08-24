from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cipherfx_clean.contracts import Candle, RawTick
from cipherfx_clean.calendar import SessionWindow, TradingCalendar
from cipherfx_clean.data_quality import DataQualityPolicy
from cipherfx_clean.execution import BotExecutionHandoff, BotExecutionResponse
from cipherfx_clean.hfm_data import SourceDataset
from cipherfx_clean.historical_memory import (
    FEATURES_PER_TIMEFRAME,
    GLOBAL_FEATURE_COUNT,
    HistoricalAnalogueIndex,
    HistoricalPath,
    HistoricalState,
    HorizonOutcome,
)
from cipherfx_clean.intelligence.provider import StructuredResearchProvider
from cipherfx_clean.runtime import CleanRuntime
from cipherfx_clean.runtime_trace import verify_completed_trace
from cipherfx_clean.slippage import BrokerFill
from cipherfx_clean.snapshot import RESEARCH_TIMEFRAMES
from cipherfx_clean.store import EvidenceStore


OBSERVED = datetime(2026, 8, 21, 12, tzinfo=timezone.utc)


def _dataset() -> SourceDataset:
    intervals = {
        "MN1": timedelta(days=30),
        "W1": timedelta(days=7),
        "D1": timedelta(days=1),
        "H4": timedelta(hours=4),
        "H1": timedelta(hours=1),
        "M30": timedelta(minutes=30),
        "M15": timedelta(minutes=15),
        "M5": timedelta(minutes=5),
        "M3": timedelta(minutes=3),
        "M1": timedelta(minutes=1),
    }
    bars = {}
    for frame, interval in intervals.items():
        rows = []
        if frame == "MN1":
            starts = []
            cursor = datetime(2024, 1, 1, tzinfo=timezone.utc)
            while len(starts) < 30:
                starts.append(cursor)
                cursor = datetime(cursor.year + (cursor.month == 12), 1 if cursor.month == 12 else cursor.month + 1, 1, tzinfo=timezone.utc)
        else:
            first = OBSERVED - interval * 30
            starts = [first + interval * index for index in range(30)]
        for index, start in enumerate(starts):
            price = 100 + index * 0.1
            if frame == "MN1":
                end = datetime(start.year + (start.month == 12), 1 if start.month == 12 else start.month + 1, 1, tzinfo=timezone.utc)
            else:
                end = start + interval
            rows.append(
                Candle(
                    "XAUUSD",
                    frame,
                    start,
                    end,
                    price,
                    price + 0.3,
                    price - 0.2,
                    price + 0.15,
                    tick_count=100 + index,
                    tick_volume=100 + index,
                    source="HFM_MT5_CSV",
                    source_id=f"{frame}:{index}",
                )
            )
        bars[frame] = tuple(rows)
    ticks = tuple(
        RawTick("XAUUSD", OBSERVED - timedelta(seconds=90 - index), 103 + index * 0.001, 103.05 + index * 0.001)
        for index in range(90)
    )
    return SourceDataset(
        "hfm-dataset-1",
        "XAUUSD",
        min(rows[0].start for rows in bars.values()),
        OBSERVED,
        bars,
        ticks,
        {frame: f"hash:{frame}" for frame in bars},
        (),
        {"build": 5836},
        {"login": 57498881},
    )


def _memory() -> HistoricalAnalogueIndex:
    states = tuple(
        HistoricalState(
            f"historical-{index}",
            "history-dataset",
            "XAUUSD",
            OBSERVED - timedelta(days=index + 1),
            tuple(float(index) for _ in range(len(RESEARCH_TIMEFRAMES) * FEATURES_PER_TIMEFRAME + GLOBAL_FEATURE_COUNT)),
            ("LONDON", "H1:STRUCTURE:UP"),
            (f"source:{index}",),
        )
        for index in range(2)
    )
    paths = tuple(
        HistoricalPath(
            state.state_id,
            (HorizonOutcome(300, 0.001 + index * 0.001, 0.002, -0.0005),),
            "BAR_OHLC_NO_EXECUTABLE_TICKS",
        )
        for index, state in enumerate(states)
    )
    return HistoricalAnalogueIndex(states, paths)


class RecordingBot:
    def __init__(self):
        self.calls = []

    def submit(self, instruction):
        self.calls.append(instruction)
        return BotExecutionResponse(
            instruction.decision_id,
            "FILLED",
            "mt5:deal:200",
            fills=(
                BrokerFill(
                    "100",
                    "200",
                    103.0,
                    103.05,
                    1.0,
                    OBSERVED,
                    "hfm-deals:200",
                    requested_at=OBSERVED - timedelta(milliseconds=100),
                ),
            ),
            reason="BROKER_ACCEPTED",
        )


class ResearchResponder:
    def __init__(self, action="BUY", edge_id="edge-live"):
        self.action = action
        self.edge_id = edge_id
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        if self.action == "NO_TRADE":
            return {"decision_id": "decision-none", "action": "NO_TRADE", "evidence": ["state-observed"]}
        return {
            "decision_id": "decision-live",
            "action": self.action,
            "edge_id": self.edge_id,
            "edge_version": 4,
            "evidence": ["chart:XAUUSD:H1", "analogue:historical-0", "validation:edge-live:4"],
            "entry": 103.05,
            "stop": 102.0,
            "target": 105.5,
            "entry_area": [102.9, 103.1],
            "confidence": 0.71,
            "probability": 0.63,
            "expected_value": 0.18,
            "setup_type": "VALIDATED_LONDON_CONTINUATION",
            "stop_concept": "STRUCTURE_INVALIDATION",
            "target_concept": "OPPOSING_LIQUIDITY",
            "reason_codes": ["EDGE_VERSION_ACTIVE", "HISTORICAL_ANALOGUES_PRESENT"],
        }


def _runtime(tmp_path, responder, bot):
    store = EvidenceStore(tmp_path / "runtime.sqlite3")
    provider = StructuredResearchProvider(responder, {"edge-live": 4})
    execution = BotExecutionHandoff(bot, store)
    runtime = CleanRuntime(
        store=store,
        provider=provider,
        execution=execution,
        memory=_memory(),
        calendar=TradingCalendar(
            timezone_name="UTC",
            sessions=(SessionWindow("ALWAYS", tuple(range(7)), datetime.min.time(), datetime.min.time()),),
        ),
        quality_policy=DataQualityPolicy(
            RESEARCH_TIMEFRAMES,
            {frame: 20 for frame in RESEARCH_TIMEFRAMES},
            {
                **{frame: timedelta(minutes=1) for frame in RESEARCH_TIMEFRAMES},
                "MN1": timedelta(days=62),
            },
            timedelta(minutes=2),
        ),
        edge_statistics={"edge-live:4": {"status": "PROMOTED", "oos_samples": 250}},
    )
    return store, runtime


def test_canonical_runtime_wires_dataset_intelligence_memory_decision_bot_and_lineage(tmp_path):
    responder = ResearchResponder()
    bot = RecordingBot()
    store, runtime = _runtime(tmp_path, responder, bot)

    first = runtime.process(_dataset(), observed_at=OBSERVED)
    second = runtime.process(_dataset(), observed_at=OBSERVED)

    assert first == second
    assert first.outcome.status == "FILLED"
    assert first.receipt.broker_reference == "mt5:deal:200"
    assert len(bot.calls) == 1
    instruction = bot.calls[0]
    assert instruction.side == "BUY"
    assert instruction.entry == 103.05
    assert instruction.stop == 102.0
    assert instruction.target == 105.5
    assert instruction.edge_id == "edge-live"
    assert instruction.edge_version == 4
    assert responder.requests[0].analogue_ids == ("historical-0", "historical-1")
    stats = responder.requests[0].statistics["historical_analogues"]
    assert stats["sample_size"] == 2
    assert stats["status"] == "OBSERVED_HISTORICAL_ANALOGUES"
    assert responder.requests[0].statistics["validated_edges"]["edge-live:4"]["status"] == "PROMOTED"
    trace = store.runtime_trace(first.trace_id)
    verification = verify_completed_trace(trace, execution_required=True)
    assert verification.valid, verification
    assert tuple(row.artifact_id for row in trace if row.stage == "BROKER_RECEIPT_RECORDED") == (
        "mt5:deal:200",
    )
    for table, expected in {
        "datasets": 1,
        "dataset_quality_reports": 1,
        "market_states": 1,
        "feature_records": 1,
        "trade_decisions": 1,
        "trade_outcomes": 1,
        "broker_events": 1,
        "execution_latency_observations": 1,
        "lineage_events": 1,
    }.items():
        assert store._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == expected
    assert store._conn.execute("SELECT COUNT(*) FROM chart_evidence").fetchone()[0] == 13
    latency = store.load_execution_latency_observations(first.decision.decision_id)
    assert len(latency) == 1
    assert latency[0].trace_id == first.trace_id
    assert latency[0].durations_seconds["broker_request_to_fill"] == pytest.approx(0.1)
    assert all(value >= 0 for value in latency[0].durations_seconds.values())
    for chart in store.load_chart_history(symbol="XAUUSD"):
        payload = chart["payload"]
        assert payload["candle_count"] == len(payload["candles"])
        assert payload["candles"]
        assert payload["source_ids"]
        assert payload["chart_hash"]
        assert payload["candle_hash"]
    assert store.integrity()
    store.close()


def test_no_trade_is_persisted_and_never_reaches_execution(tmp_path):
    responder = ResearchResponder(action="NO_TRADE")
    bot = RecordingBot()
    store, runtime = _runtime(tmp_path, responder, bot)

    result = runtime.process(_dataset(), observed_at=OBSERVED)

    assert result.outcome.status == "NO_TRADE"
    assert result.receipt is None
    assert bot.calls == []
    trace = store.runtime_trace(result.trace_id)
    verification = verify_completed_trace(trace, execution_required=False)
    assert verification.valid, verification
    assert "EXECUTION_HANDOFF_SUBMITTED" not in verification.observed_stages
    assert store._conn.execute("SELECT status FROM trade_outcomes").fetchone()[0] == "NO_TRADE"
    assert store._conn.execute("SELECT COUNT(*) FROM broker_events").fetchone()[0] == 0
    store.close()


def test_unapproved_edge_is_audited_and_cannot_reach_execution(tmp_path):
    responder = ResearchResponder(edge_id="unapproved-edge")
    bot = RecordingBot()
    store, runtime = _runtime(tmp_path, responder, bot)

    with pytest.raises(ValueError, match="not approved"):
        runtime.process(_dataset(), observed_at=OBSERVED)

    assert bot.calls == []
    assert store._conn.execute("SELECT event_type FROM audit_events").fetchone()[0] == "DECISION_PROVIDER_REJECTED"
    assert store._conn.execute("SELECT COUNT(*) FROM trade_decisions").fetchone()[0] == 0
    store.close()


def test_clean_package_has_no_legacy_imports_or_alternate_runtime():
    root = Path(__file__).parents[1] / "cipherfx_clean"
    forbidden = ("cipherfx_platform", "mt5_xm_gateway", "CipherFxBridge")
    assert not (root / "app.py").exists()
    assert not (root / "pipeline.py").exists()
    for path in root.rglob("*.py"):
        text = path.read_text()
        assert not any(token in text for token in forbidden), path
