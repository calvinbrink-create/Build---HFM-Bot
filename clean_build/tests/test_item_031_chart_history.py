import json
from datetime import datetime, timedelta, timezone

import pytest

from cipherfx_clean.chart_history import build_persistent_chart, validate_persistent_chart
from cipherfx_clean.contracts import Candle, TradeDecision
from cipherfx_clean.intelligence.model import ChartPack
from cipherfx_clean.intelligence.renderer import render_svg
from cipherfx_clean.store import EvidenceStore


OBSERVED = datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc)


def _chart():
    candles = tuple(
        Candle(
            "XAUUSD",
            "M5",
            OBSERVED - timedelta(minutes=5 * (4 - index)),
            OBSERVED - timedelta(minutes=5 * (3 - index)),
            100.0 + index,
            101.0 + index,
            99.5 + index,
            100.5 + index,
            tick_count=10 + index,
            tick_volume=20 + index,
            source="HFM_MT5_CSV",
            source_id=f"M5:{index}",
        )
        for index in range(3)
    )
    pack = ChartPack(
        symbol="XAUUSD",
        observed_at=OBSERVED.isoformat(),
        timeframes={"M5": candles},
        annotations={"M5": ({"type": "VWAP", "price": 101.0},)},
        pack_id="state-1",
        source_ids=tuple(candle.source_id for candle in candles),
    )
    decision = TradeDecision("decision-1", "XAUUSD", "BUY", OBSERVED, entry=102.0, stop=100.0, target=106.0)
    svg = render_svg(candles, annotations=pack.annotations["M5"])
    return build_persistent_chart(pack, decision, "M5", candles, svg)


def test_chart_and_exact_underlying_candles_survive_rescan_and_restart(tmp_path):
    path = tmp_path / "history.sqlite3"
    chart = _chart()
    store = EvidenceStore(path)
    store.write_chart(chart.chart_id, chart.symbol, chart.observed_at, chart.payload)
    store.write_chart(chart.chart_id, chart.symbol, chart.observed_at, chart.payload)
    assert store._conn.execute("SELECT COUNT(*) FROM chart_evidence").fetchone()[0] == 1
    store.close()

    reopened = EvidenceStore(path)
    loaded = reopened.load_chart(chart.chart_id)
    assert loaded is not None
    assert loaded["payload"] == json.loads(json.dumps(chart.payload))
    assert len(reopened.load_chart_history(symbol="XAUUSD", timeframe="M5")) == 1
    validate_persistent_chart(
        loaded["payload"],
        symbol="XAUUSD",
        observed_at=loaded["observed_at"],
    )
    assert reopened.integrity()
    reopened.close()


def test_persistent_chart_rejects_changed_content_for_same_identity(tmp_path):
    chart = _chart()
    store = EvidenceStore(tmp_path / "history.sqlite3")
    store.write_chart(chart.chart_id, chart.symbol, chart.observed_at, chart.payload)
    changed = dict(chart.payload)
    changed["svg"] = "changed"
    with pytest.raises(ValueError, match="immutable record conflict"):
        store.write_chart(chart.chart_id, chart.symbol, chart.observed_at, changed)
    store.close()
