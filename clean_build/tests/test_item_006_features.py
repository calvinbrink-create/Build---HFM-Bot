from datetime import datetime, timedelta, timezone

from cipherfx_clean.analogue import AnalogueLibrary
from cipherfx_clean.features import build_fingerprint
from cipherfx_clean.market import Candle, RawTick


def test_fingerprint_and_analogue_search_use_observed_state():
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ticks = tuple(RawTick("XAUUSD", base + timedelta(seconds=i), 10 + i * 0.1, 10.2 + i * 0.1) for i in range(3))
    candle = Candle("XAUUSD", "M1", base, base + timedelta(minutes=1), 10, 10.4, 10, 10.3, 3)
    fingerprint = build_fingerprint(ticks, (candle,))
    library = AnalogueLibrary()
    library.add(fingerprint, ("outcome-1",))
    results = library.search(fingerprint)
    assert results[0].distance == 0.0
    assert results[0].outcome_ids == ("outcome-1",)
