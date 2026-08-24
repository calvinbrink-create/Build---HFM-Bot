from datetime import datetime, timedelta, timezone

from cipherfx_clean.contracts import Candle
from cipherfx_clean.memory import MemoryCase, MemoryLibrary
from cipherfx_clean.structure import analyse
from cipherfx_clean.zones import discover


def candle(i, open_, high, low, close):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=i)
    return Candle("XAUUSD", "M5", start, start + timedelta(minutes=5), open_, high, low, close)


def test_structure_zones_and_memory_are_real_setup_inputs():
    candles = tuple(candle(i, 10 + i, 11 + i, 9 + i, 10.5 + i) for i in range(8))
    state = analyse(candles)
    assert state.direction in {"UP", "DOWN", "RANGE", "UNKNOWN"}
    assert isinstance(discover(candles), tuple)
    memory = MemoryLibrary()
    memory.add(MemoryCase("c1", "XAUUSD", candles[-1].start, (1.0, 2.0), "WIN", 1.0))
    assert memory.nearest((1.0, 2.0))[0].case_id == "c1"
