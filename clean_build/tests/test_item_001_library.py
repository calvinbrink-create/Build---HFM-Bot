from datetime import datetime, timezone

from cipherfx_clean.features import MarketFingerprint
from cipherfx_clean.intelligence.library import MarketStateLibrary, OutcomeRecord, StateRecord
from cipherfx_clean.intelligence.lineage import Lineage, LineageLedger


def test_library_keeps_winners_losers_rejections_misses_and_no_trades():
    now = datetime.now(timezone.utc)
    fingerprint = MarketFingerprint("XAUUSD", 1, 0, 0, 0, 0, 0, 0, 0, 0)
    library = MarketStateLibrary()
    library.add_state(StateRecord("s1", "XAUUSD", now, fingerprint, "TRADE", "entered", {}))
    library.add_state(StateRecord("s2", "XAUUSD", now, fingerprint, "REJECTED", "no_edge", {}))
    library.add_state(StateRecord("s3", "XAUUSD", now, fingerprint, "MISSED", "no_trigger", {}))
    library.add_state(StateRecord("s4", "XAUUSD", now, fingerprint, "NO_TRADE", "ordinary", {}))
    library.add_outcome(OutcomeRecord("s1", "WIN", 1.0, 1.2, -0.2, 60, 30))
    assert len(library.states()) == 4
    assert len(library.states("NO_TRADE")) == 1
    assert library.outcome("s1").result == "WIN"


def test_lineage_ledger_rejects_duplicate_events():
    now = datetime.now(timezone.utc)
    item = Lineage("e1", "s1", "d1", "edge", now, now, "chart", "o1")
    ledger = LineageLedger()
    ledger.record(item)
    assert ledger.get("e1").decision_id == "d1"
